"""Arize Phoenix tracing for LLM token-usage baselining.

Usage (before importing OpenAI in other modules):
    from services.orchestrator.src.llm.tracing import setup_phoenix_tracing
    setup_phoenix_tracing()   # call once at process start

Prerequisites:
    1. Run Phoenix locally:  python -m app.phoenix serve   (or px serve)
       It listens on http://localhost:6006 by default.
    2. All required packages are already in requirements.txt:
       - arize-phoenix
       - arize-phoenix-otel
       - opentelemetry-sdk
       - opentelemetry-exporter-otlp
       - openinference-instrumentation-openai
"""

import logging
import os
from typing import Optional

logger = logging.getLogger(__name__)

_phoenix_tracer_provider = None


def setup_phoenix_tracing(
    endpoint: Optional[str] = None,
    project_name: Optional[str] = None,
) -> None:
    """Initialize OpenTelemetry + Phoenix OTLP exporter and instrument the OpenAI SDK.

    Safe to call multiple times (idempotent). On the second+ call it is a no-op.

    Parameters
    ----------
    endpoint :
        OTLP HTTP endpoint for the Phoenix collector.
        Defaults to $PHOENIX_COLLECTOR_ENDPOINT or http://localhost:6006/v1/traces.
    project_name :
        The project/dataset name shown in the Phoenix UI.
    """
    global _phoenix_tracer_provider

    if _phoenix_tracer_provider is not None:
        logger.debug("Phoenix tracing already initialised - skipping.")
        return

    active_project_name = (
        project_name 
        or os.getenv("PHOENIX_PROJECT_NAME") 
        or "orchestrator-token-baseline"
    )

    collector_endpoint = (
        endpoint
        or os.getenv("PHOENIX_COLLECTOR_ENDPOINT")
        or "http://localhost:6006/v1/traces"
    )

    from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
    from opentelemetry.sdk.resources import Resource
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import BatchSpanProcessor
    import opentelemetry.trace as trace
    from opentelemetry.sdk.trace import SpanLimits

    resource = Resource.create({
        "service.name": "orchestrator",
        "service.version": os.getenv("BUILD_VERSION", "local"),
        "openinference.project.name": active_project_name,
    })

    limits = SpanLimits(max_attributes=1024)
    provider = TracerProvider(resource=resource)
    otlp_exporter = OTLPSpanExporter(endpoint=collector_endpoint)
    provider.add_span_processor(BatchSpanProcessor(otlp_exporter))

    trace.set_tracer_provider(provider)
    _phoenix_tracer_provider = provider
    logger.info(
        "\U0001f4ca Phoenix tracing -> %s (project: %s)",
        collector_endpoint,
        active_project_name,
    )

    try:
        from openinference.instrumentation.openai import OpenAIInstrumentor
        OpenAIInstrumentor().instrument()
        logger.info("OpenAI SDK instrumented via openinference-instrumentation-openai.")
    except ImportError:
        logger.warning(
            "openinference-instrumentation-openai not installed. "
            "pip install openinference-instrumentation-openai"
        )


def shutdown_tracing() -> None:
    """Flush and shut down the tracer provider (call on clean exit)."""
    global _phoenix_tracer_provider
    if _phoenix_tracer_provider is not None:
        _phoenix_tracer_provider.shutdown()
        _phoenix_tracer_provider = None
        logger.info("Phoenix tracing shut down.")
