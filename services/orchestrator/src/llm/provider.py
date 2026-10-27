import logging
import os
from typing import Dict, List, Optional

import httpx
from openai import OpenAI
from opentelemetry import trace

logger = logging.getLogger(__name__)


def _ensure_phoenix_tracing() -> None:
    """Auto-initialise Phoenix tracing if PHOENIX_TRACING env var is set.

    Set ``PHOENIX_TRACING=1`` (optionally ``PHOENIX_COLLECTOR_ENDPOINT`` and
    ``PHOENIX_PROJECT_NAME``) in the environment to enable tracing without
    any code changes.
    """
    if os.getenv("PHOENIX_TRACING") not in ("1", "true", "yes"):
        return
    try:
        from services.orchestrator.src.llm.tracing import setup_phoenix_tracing
        project_name = os.getenv("PHOENIX_PROJECT_NAME", "orchestrator-token-baseline")
        endpoint = os.getenv("PHOENIX_COLLECTOR_ENDPOINT", "http://localhost:6006/v1/traces")
        setup_phoenix_tracing(endpoint=endpoint, project_name=project_name)
    except Exception as exc:
        logger.warning("Could not enable Phoenix tracing: %s", exc)


class UniversalLLMProvider:
    """Connects the Python agent loop using the OpenAI-compatible SDK interface.

    Exposes ``chars_per_token`` as a first-class property so downstream consumers
    (e.g. the context compaction manager) can interrogate the provider instead of
    re-parsing the config dictionary. This keeps the memory system decoupled from
    the config parser.
    """

    def __init__(
        self,
        endpoint_url: str,
        model: str,
        api_key: Optional[str] = None,
        chars_per_token: float = 3.5,
    ) -> None:
        _ensure_phoenix_tracing()

        self.endpoint_url = endpoint_url
        self.model = model
        self.chars_per_token: float = float(chars_per_token)

        base_url = endpoint_url.split("/chat/completions")[0].split("/responses")[0]
        proxy_url = os.getenv("HTTPS_PROXY") or os.getenv("HTTP_PROXY")
        http_client = httpx.Client(proxy=proxy_url) if proxy_url else None

        if "127.0.0.1" in endpoint_url or "localhost" in endpoint_url or "11434" in endpoint_url:
            self.client = OpenAI(base_url=base_url, api_key="ollama", http_client=http_client)
        else:
            if "azure.com" in endpoint_url and (not api_key or api_key.lower() in ["none", ""]):
                from azure.identity import DefaultAzureCredential, get_bearer_token_provider

                token_provider = get_bearer_token_provider(
                    DefaultAzureCredential(), "https://ai.azure.com/.default"
                )
                logger.info("\ud83d\udd10 Azure Entra ID authentication enabled via OpenAI SDK.")
                self.client = OpenAI(base_url=base_url, api_key=token_provider, http_client=http_client)
            else:
                self.client = OpenAI(base_url=base_url, api_key=api_key or "sk-dummy", http_client=http_client)

    def generate(self, context: list, require_json: bool = True) -> Optional[str]:
        try:
            if "azure.com" in self.endpoint_url and "/responses" in self.endpoint_url:
                response = self.client.responses.create(model=self.model, input=context)
                self._log_token_usage(response, context)
                try:
                    return response.output[0].content[0].text
                except (KeyError, IndexError, AttributeError):
                    return str(getattr(response, "output", response))
            else:
                kwargs = {"model": self.model, "messages": context}
                if require_json:
                    kwargs["response_format"] = {"type": "json_object"}
                response = self.client.chat.completions.create(**kwargs)
                self._log_token_usage(response, context)
                return response.choices[0].message.content
        except Exception as e:
            logger.error(f"\u274c [SDK Connection Error] Failed to generate: {e}")
            raise RuntimeError(f"LLM Provider unreachable: {e}")

    def _log_token_usage(self, response: object, context: list) -> None:
        """Extract and log token counts from the API response for baseline tracking.

        This records the *current* pre-prefix-caching token usage so we can
        compare against post-caching numbers in Phoenix.
        """
        try:
            usage = getattr(response, "usage", None)
            if usage is None:
                return
            prompt_tokens = getattr(usage, "prompt_tokens", 0) or 0
            completion_tokens = getattr(usage, "completion_tokens", 0) or 0
            total_tokens = getattr(usage, "total_tokens", 0) or 0

            prompt_chars = sum(
                len(msg.get("content") or "") if isinstance(msg, dict) else len(str(getattr(msg, "content", "")))
                for msg in context
            )

            # Extract cached tokens (vLLM/OpenAI compatible)
            cached_tokens = 0
            if hasattr(usage, "prompt_tokens_details") and usage.prompt_tokens_details:
                cached_tokens = getattr(usage.prompt_tokens_details, "cached_tokens", 0)
            
            # Attach custom metrics to the active OTel span so Phoenix can index them
            current_span = trace.get_current_span()
            if current_span.is_recording():
                current_span.set_attribute("llm.token_count.prompt_cache_hit", cached_tokens)
                current_span.set_attribute("llm.token_count.prompt_cache_miss", prompt_tokens - cached_tokens)

            logger.info(
                "\U0001f50d [Token Baseline] model=%s | "
                "prompt_tokens=%d completion_tokens=%d total_tokens=%d | "
                "prompt_chars=%d (est chars/token=%.1f) | "
                "chars_per_token_config=%.1f",
                self.model,
                prompt_tokens,
                completion_tokens,
                total_tokens,
                prompt_chars,
                (prompt_chars / prompt_tokens) if prompt_tokens else 0.0,
                self.chars_per_token,
            )
        except Exception as exc:  # noqa: BLE001
            logger.debug("Could not extract token usage: %s", exc)


class MockLLMProvider:
    """Mocks structured JSON generation for unit tests.

    Mirrors the ``UniversalLLMProvider`` introspection surface (``model`` and
    ``chars_per_token``) so that consumers which interrogate the provider do not
    need to distinguish real from mock providers.
    """

    def __init__(
        self,
        mock_responses: List[str],
        model: str = "mock",
        chars_per_token: float = 3.5,
    ) -> None:
        self.mock_responses = mock_responses
        self.index = 0
        self.model = model
        self.chars_per_token: float = float(chars_per_token)

    def generate(self, context: List[Dict[str, str]], require_json: bool = True) -> str:
        if self.index < len(self.mock_responses):
            response = self.mock_responses[self.index]
            self.index += 1
            return response
        return '{"reasoning": "No more instructions.", "tool": "finish_task", "tool_args": {}}'
