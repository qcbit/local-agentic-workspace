import asyncio
import os
import platform
import psutil
import time
from typing import Any, Dict, List, Optional, Union
from loguru import logger
from opentelemetry import trace

tracer = trace.get_tracer(__name__)


def detect_optimal_candidate_k(configured_val: Union[int, str] = "auto") -> int:
    """
    Resolves candidate_pool_k dynamically based on host architecture,
    CPU core count, and physical memory capacity.
    """
    if isinstance(configured_val, int):
        return configured_val

    if str(configured_val).lower() != "auto":
        try:
            return int(configured_val)
        except ValueError:
            pass

    system = platform.system()
    machine = platform.machine().lower()
    cpu_cores = os.cpu_count() or 4
    total_ram_gb = psutil.virtual_memory().total / (1024 ** 3)

    # Tier 1: Apple Silicon (unified memory + high-bandwidth NEON SIMD)
    if system == "Darwin" and machine in ("arm64", "aarch64"):
        resolved_k = 25

    # Tier 2: High-spec x86_64 Workstations / Desktops
    elif cpu_cores >= 8 and total_ram_gb >= 16:
        resolved_k = 25

    # Tier 3: Standard 4 to 6-core Laptops
    elif cpu_cores >= 4 and total_ram_gb >= 8:
        resolved_k = 15

    # Tier 4: Constrained / Low-power devices
    else:
        resolved_k = 8

    logger.info(
        f"🖥️ [Reranker Hardware Autodetect] Arch: {machine} | Cores: {cpu_cores} | "
        f"RAM: {total_ram_gb:.1f}GB -> candidate_pool_k resolved to {resolved_k}"
    )
    return resolved_k


class Reranker:
    """
    Lazy-loaded singleton wrapper around FastEmbed's TextCrossEncoder.
    Maintains zero-egress, local ONNX cross-encoder execution.
    """

    def __init__(
        self,
        model_name: str = "BAAI/bge-reranker-base",
        max_latency_ms: float = 250.0,
    ):
        self.model_name = model_name
        self.max_latency_ms = max_latency_ms
        self._model = None
        self._load_error = False

    def _get_model(self):
        if self._load_error:
            return None
        if self._model is None:
            try:
                from fastembed.rerank.cross_encoder import TextCrossEncoder

                logger.info(f"Loading FastEmbed CrossEncoder: {self.model_name}...")
                self._model = TextCrossEncoder(model_name=self.model_name)
            except Exception as e:
                logger.error(
                    f"Failed to load cross-encoder model '{self.model_name}': {e}"
                )
                self._load_error = True
        return self._model

    def rerank(
        self, query: str, candidates: List[Dict[str, Any]]
    ) -> List[Dict[str, Any]]:
        """
        Scores candidates against query and returns them sorted by descending _rerank_score.
        Preserves all original dictionary keys and falls back gracefully to
        distance order on failure.
        """
        if not candidates:
            return []

        with tracer.start_as_current_span("rag_rerank") as span:
            span.set_attribute("rerank.model", self.model_name)
            span.set_attribute("rerank.n_in", len(candidates))

            model = self._get_model()
            if model is None:
                span.set_attribute("rerank.fallback_used", True)
                return candidates

            t0 = time.time()
            texts = [c.get("content", "") for c in candidates]

            try:
                scores_generator = model.rerank(query, texts)
                scores = [float(s) for s in scores_generator]

                for i, c in enumerate(candidates):
                    c["_rerank_score"] = scores[i]

                reranked = sorted(
                    candidates,
                    key=lambda x: x.get("_rerank_score", -999.0),
                    reverse=True,
                )

                t1 = time.time()
                latency_ms = (t1 - t0) * 1000
                span.set_attribute("rerank.latency_ms", latency_ms)
                span.set_attribute("rerank.n_out", len(reranked))
                span.set_attribute("rerank.fallback_used", False)

                if scores:
                    span.set_attribute("rerank.score.min", min(scores))
                    span.set_attribute("rerank.score.max", max(scores))
                    span.set_attribute("rerank.score.mean", sum(scores) / len(scores))

                logger.info(
                    f"🎯 Rerank: model={self.model_name} in={len(candidates)} "
                    f"out={len(reranked)} {latency_ms:.1f}ms"
                )
                return reranked

            except Exception as e:
                logger.error(f"Reranking computation failed: {e}")
                span.set_attribute("rerank.fallback_used", True)
                span.record_exception(e)
                return candidates

    async def rerank_async(
        self,
        query: str,
        candidates: List[Dict[str, Any]],
        timeout: Optional[float] = None,
    ) -> List[Dict[str, Any]]:
        """
        Executes reranking on a background thread to prevent blocking
        the async event loop, bounded by a strict latency timeout.
        """
        if timeout is None:
            timeout = self.max_latency_ms / 1000.0

        try:
            return await asyncio.wait_for(
                asyncio.to_thread(self.rerank, query, candidates),
                timeout=timeout,
            )
        except asyncio.TimeoutError:
            logger.warning(
                f"Reranking timed out after {timeout}s. Falling back to distance order."
            )
            return candidates
        except Exception as e:
            logger.error(f"Async reranking failed: {e}")
            return candidates


_RERANKER_SINGLETON: Optional[Reranker] = None


def get_reranker(config: Optional[Dict[str, Any]] = None) -> Optional[Reranker]:
    """
    Factory function returning the process-lifetime Reranker singleton,
    or None if disabled via configuration.
    """
    global _RERANKER_SINGLETON

    rag_config = config.get("rag", {}).get("rerank", {}) if config else {}
    if not rag_config.get("enabled", True):
        return None

    if _RERANKER_SINGLETON is None:
        model_name = rag_config.get("model", "BAAI/bge-reranker-base")
        max_latency_ms = float(rag_config.get("max_latency_ms", 250.0))
        _RERANKER_SINGLETON = Reranker(
            model_name=model_name, max_latency_ms=max_latency_ms
        )

    return _RERANKER_SINGLETON