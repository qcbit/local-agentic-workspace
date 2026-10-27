"""Sliding window context manager with model-routed compaction.

The context manager no longer accepts a ``llm_config`` dict for token-estimation
parameters.  Instead it interrogates the assigned ``llm_provider`` for
``chars_per_token`` and ``model``, keeping the memory layer decoupled from the
config parser entirely.

Batch Eviction (Chunking) & Async Background Routing
----------------------------------------------------
To maximize LLM prefix-cache retention and eliminate latency, the manager
accumulates dropped messages and triggers a background summarization task
only when the buffer reaches ``compaction_chunk_size``. 

During this async execution, the flagship model continues working without 
blocking. When the background task resolves, the new summary is merged and 
the exact object IDs of the summarized messages are pruned from history.
"""
import asyncio
from dataclasses import dataclass
from typing import List, Dict, Any, Optional

import logging
from opentelemetry import trace

logger = logging.getLogger(__name__)

tracer = trace.get_tracer(__name__)

_MODEL_MISSING_MARKERS = (
    "model not found",
    "not found",
    "no such model",
    "does not exist",
    "model not pulled",
    "manifest for",
    "unknown model",
    "unknown: model",
    "404",
)

@dataclass(frozen=True)
class PruneCandidate:
    message_id: int          # id(msg)
    reason: str              # e.g., "superseded_retry"
    chars_saved: int

@dataclass(frozen=True)
class CompactionSnapshot:
    snapshot_ids: frozenset[int]
    summary_at_time: str
    dropped_text: str
    created_at: float


class SlidingContextManager:
    """Manages the token window with batch-eviction compaction for cache retention.

    Key design change (Batch Eviction / Chunking):
        Instead of calling the compaction model on *every* overflow event, the
        manager accumulates dropped messages in ``_pending_compaction`` and only
        triggers an LLM summarization call once the buffer length reaches
        ``compaction_chunk_size`` (default **7**, configurable via
        ``memory_config["compaction_chunk_size"]``).

        Between compaction events the running summary prefix remains byte-
        identical, which keeps provider-side prompt caches (vLLM, Ollama,
        OpenAI, etc.) warm for that many iterations.
    """

    def __init__(
        self,
        memory_config: Dict[str, Any],
        model_name: str,
        llm_provider: Any,
        llm_config: Optional[Dict[str, Any]] = None,
    ) -> None:
        self.max_tokens = memory_config.get("max_tokens", 4000)
        self.llm_provider = llm_provider

        self.chars_per_token: float = float(getattr(llm_provider, "chars_per_token", 3.5))
        self.model_name: str = getattr(llm_provider, "model", model_name or "")

        # --- Batch Eviction configuration ---
        self.compaction_chunk_size: int = int(
            memory_config.get("compaction_chunk_size", 7)
        )
        self.retain_recent_observations: int = int(
            memory_config.get("retain_recent_observations", 2)
        )

        # Async State
        self._pending_task: Optional[asyncio.Task] = None
        self._pending_snapshot: Optional[CompactionSnapshot] = None

        self.last_compaction_token_overhead: int = 0

        self.compaction_disabled: bool = False
        self.compaction_disabled_reason: str = ""
        self._compaction_missing_warned: bool = False

        logger.info(
            f"\U0001f9e0 Context Manager initialized for '{self.model_name}' "
            f"(Ratio: {self.chars_per_token} chars/token, Max: {self.max_tokens}, "
            f"Chunk: {self.compaction_chunk_size})"
        )

    def _normalize_model_name(self, model_name: str) -> str:
        name = model_name.lower()
        if "llama3" in name:
            return "llama3:8b"
        if "qwen" in name:
            return "qwen2.5-coder"
        return name

    @staticmethod
    def _is_error(content: str) -> bool:
        """Heuristic check for tool errors based on content prefix markers."""
        head = content[:200]
        return ("Error:" in head or "Security violation:" in head or content.strip().startswith("Error:"))

    def _estimate_tokens(self, text: str) -> int:
        return max(1, int(len(text) / self.chars_per_token))

    @staticmethod
    def _is_model_missing_error(exc: BaseException) -> bool:
        haystack = " ".join(
            str(part) for part in (getattr(exc, "args", ()) or ()) + (str(exc),) if part
        ).lower()
        return any(marker in haystack for marker in _MODEL_MISSING_MARKERS)

    def _raise_model_missing_alert(self, exc: BaseException) -> None:
        self.compaction_disabled = True
        model = self.model_name or "<unknown>"
        if self._is_model_missing_error(exc):
            self.compaction_disabled_reason = (
                f"Compaction is DISABLED: the configured compaction model "
                f"'{model}' is not available on the local server. "
                f"Context overflows will NOT be summarized and the working "
                f"context will keep growing. Download it to re-enable "
                f"compaction:  ollama pull {model}"
            )
        else:
            self.compaction_disabled_reason = (
                f"Compaction is DISABLED: the compaction provider could not "
                f"be used (model '{model}'). Context overflows will NOT be "
                f"summarized. Details: {exc}"
            )

        if not self._compaction_missing_warned:
            self._compaction_missing_warned = True
            logger.warning(
                "[Compaction-Disabled] %s",
                self.compaction_disabled_reason,
            )
        else:
            logger.debug(
                "[Compaction-Disabled] Recurring compaction failure suppressed: %s", exc
            )

    def compaction_status(self) -> Dict[str, Any]:
        base: Dict[str, Any] = {
            "disabled": self.compaction_disabled,
            "reason": self.compaction_disabled_reason,
            "model": self.model_name,
            "fix_hint": f"ollama pull {self.model_name}" if self._is_model_missing_for_model() else "",
            "pending_overflow": len(self._pending_snapshot.snapshot_ids) if self._pending_snapshot else 0,
            "chunk_size": self.compaction_chunk_size,
        }
        return base

    def _is_model_missing_for_model(self) -> bool:
        return "not available on the local server" in self.compaction_disabled_reason

    async def _generate_summary(
        self, existing_summary: str, dropped_text: str, available_tokens: int = 0, messages_dropped: int = 0
    ) -> str:
        """Calls the LLM in plain-text mode to summarize the dropped history.

        The ``dropped_text`` parameter is a pre-serialized string built by the
        caller (``build_safe_context``), so this method never touches Message
        objects and holds zero references into ``state.history``.

        Wraps the LLM call in a dedicated ``context_compaction`` OpenTelemetry span.

        OpenInference semantic conventions are applied:
        https://github.com/Arize-ai/openinference

        The ``llm.task`` attribute is set to ``context_compaction`` so that
        Phoenix dashboards can split reasoning vs. compaction spend.
        """
        from services.orchestrator.src.prompts import load_prompt

        template = load_prompt("compaction_v1")
        prompt = template.format(
            existing_summary=existing_summary if existing_summary else "None",
            dropped_text=dropped_text,
        )

        context = [{"role": "system", "content": prompt}]
        prompt_tokens = self._estimate_tokens(prompt)

        with tracer.start_as_current_span("context_compaction") as compaction_span:
            compaction_span.set_attribute("llm.task", "context_compaction")
            compaction_span.set_attribute("compaction.mechanism", "async_background")
            compaction_span.set_attribute("compaction.messages_dropped", messages_dropped)
            compaction_span.set_attribute("compaction.chunk_size", int(self.compaction_chunk_size))
            compaction_span.set_attribute("compaction.available_tokens_before", int(available_tokens))
            compaction_span.set_attribute("compaction.max_window_tokens", int(self.max_tokens))
            compaction_span.set_attribute("llm.provider", type(self.llm_provider).__name__)
            compaction_span.set_attribute(
                "llm.model_family", self._normalize_model_name(self.model_name)
            )

            try:
                # Wrap the blocking synchronous SDK call to yield to the event loop
                response = await asyncio.get_running_loop().run_in_executor(
                    None, 
                    lambda: self.llm_provider.generate(context, require_json=False).strip()
                )
            except Exception as exc:
                compaction_span.set_status(
                    trace.Status(trace.StatusCode.ERROR, f"Compaction LLM call failed: {exc}")
                )
                compaction_span.record_exception(exc)
                compaction_span.set_attribute("llm.token_count.prompt", int(prompt_tokens))
                compaction_span.set_attribute("llm.token_count.completion", 0)
                compaction_span.set_attribute("llm.token_count.total", int(prompt_tokens))
                compaction_span.set_attribute("compaction.token_overhead", int(prompt_tokens))

                self._raise_model_missing_alert(exc)
                self.last_compaction_token_overhead = int(prompt_tokens)
                raise  # Re-raise so the caller can handle task failure gracefully

            if self.compaction_disabled:
                self.compaction_disabled = False
                self.compaction_disabled_reason = ""

            completion_tokens = self._estimate_tokens(response)
            total_tokens = prompt_tokens + completion_tokens

            compaction_span.set_attribute("llm.token_count.prompt", int(prompt_tokens))
            compaction_span.set_attribute("llm.token_count.completion", int(completion_tokens))
            compaction_span.set_attribute("llm.token_count.total", int(total_tokens))
            compaction_span.set_attribute("compaction.summary_length_before", len(existing_summary or ""))
            compaction_span.set_attribute("compaction.summary_length_after", len(response or ""))
            compaction_span.set_attribute("compaction.token_overhead", int(total_tokens))

            logger.info(
                f"[Governance] Async Background Compaction: {prompt_tokens} prompt + "
                f"{completion_tokens} completion = {total_tokens} total tokens "
                f"for {messages_dropped} dropped messages."
            )

            self.last_compaction_token_overhead = int(total_tokens)
            return response

    def build_safe_context(self, state: Any, system_prompt: str) -> List[Dict[str, str]]:
        """Build the LLM-ready context window using batch-eviction compaction.

        Strategy (Batch Eviction / Chunking + Async Background Routing):
        1. If a previous background compaction task has resolved, perform
           differential reintegration: merge the summary and prune only the
           snapshot IDs from history (new messages are preserved).
        2. Walk history newest→oldest, fitting messages into the token budget.
        3. When overflow reaches ``compaction_chunk_size``, dispatch an async
           background summarization task on the secondary model.
        4. While the task is in flight, inject the raw overflow as a non-
           blocking fallback so the flagship never stalls.
        """
        # 1. Differential Reintegration (id-based prune, immune to __eq__)
        if self._pending_task is not None and self._pending_task.done():
            try:
                new_summary = self._pending_task.result()
                if new_summary:
                    state.summary = (
                        f"{state.summary}\\n{new_summary}".strip()
                        if state.summary
                        else new_summary
                    )
                    snap_ids = self._pending_snapshot.snapshot_ids
                    state.history = [m for m in state.history if id(m) not in snap_ids]
                    logger.info(
                        f"[Compaction-Background] Reintegrated summary for "
                        f"{len(snap_ids)} messages."
                    )
            except Exception as exc:
                logger.error(f"Background compaction task failed: {exc}")
            finally:
                self._pending_task = None
                self._pending_snapshot = None

        # 2. Pin the Core Prompts
        core_context = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": state.user_goal},
        ]
        core_tokens = sum(self._estimate_tokens(msg["content"]) for msg in core_context)

        # 3. Pin the Running Summary (if it exists)
        summary_context: List[Dict[str, str]] = []
        summary_tokens = 0
        if state.summary:
            summary_context = [
                {"role": "system", "content": f"Summary of earlier actions:\\n{state.summary}"}
            ]
            summary_tokens = self._estimate_tokens(summary_context[0]["content"])

        available_tokens = self.max_tokens - core_tokens - summary_tokens

        # 4. Process history newest to oldest
        retained_history: List[Dict[str, str]] = []
        overflow_messages: List[Any] = []
        current_tokens = 0
        retained_tool_count = 0

        for msg in reversed(state.history):
            content = msg.content
            role_str = msg.role.value if hasattr(msg.role, "value") else str(msg.role)

            if role_str == "tool":
                retained_tool_count += 1
                if retained_tool_count > self.retain_recent_observations:
                    status = "error" if self._is_error(msg.content) else "ok"
                    content = (
                        f"[Stale Observation Trimmed: {msg.name}] "
                        f"status={status} length={len(msg.content)}"
                    )
                else:
                    content = (
                        f"Tool '{msg.name}' Observation:\\n{msg.content}\\n\\n"
                        "What is your next action?"
                    )
                mapped_role = "user"
            else:
                mapped_role = role_str

            msg_tokens = self._estimate_tokens(content)

            if current_tokens + msg_tokens > available_tokens:
                overflow_messages.insert(0, msg)
            else:
                retained_history.insert(0, {"role": mapped_role, "content": content})
                current_tokens += msg_tokens

        # 5. Trigger Async Compaction (only if no task already in flight)
        if overflow_messages and self._pending_task is None:
            
            # --- NEW: Prune redundant retries before serialization ---
            surviving_overflow, pruned = self._classify_for_pruning(overflow_messages)
            
            if pruned:
                total_chars = sum(p.chars_saved for p in pruned)
                logger.info(
                    f"[Prune] Dropped {len(pruned)} of {len(overflow_messages)} overflow messages "
                    f"({total_chars} chars) before compaction. Reasons: {[p.reason for p in pruned]}"
                )
                if (span := trace.get_current_span()).is_recording():
                    span.set_attribute("compaction.pruned_messages", len(pruned))
                    span.set_attribute("compaction.pruned_chars", total_chars)
            
            # If everything was somehow pruned, skip compaction
            if not surviving_overflow:
                return core_context + summary_context + retained_history

            # --- Pre-Compaction Heuristic Noise Filtering ---
            non_noise_overflow: List[Any] = []
            noise_filtered_count = 0
            for m in surviving_overflow:
                if getattr(m, "is_noise", False):
                    noise_filtered_count += 1
                else:
                    non_noise_overflow.append(m)

            if noise_filtered_count:
                logger.info(
                    f"[Noise-Filter] Stripped {noise_filtered_count} of "
                    f"{len(surviving_overflow)} overflow messages tagged as noise "
                    f"before compaction."
                )
            surviving_overflow = non_noise_overflow
            # ---------------------------------------------------------------

            if not surviving_overflow:
                return core_context + summary_context + retained_history
            # ---------------------------------------------------------

            if len(surviving_overflow) >= self.compaction_chunk_size:
                logger.info(
                    f"Compaction threshold reached ({len(surviving_overflow)} >= "
                    f"{self.compaction_chunk_size}). Dispatching async background task..."
                )

                # Use surviving_overflow for all downstream operations
                snapshot_ids = frozenset(id(m) for m in surviving_overflow)
                dropped_text = "\n".join(
                    f"{m.role.value if hasattr(m.role, 'value') else str(m.role)}: {m.content}"
                    for m in surviving_overflow
                )

                self._pending_snapshot = CompactionSnapshot(
                    snapshot_ids=snapshot_ids,
                    summary_at_time=state.summary or "",
                    dropped_text=dropped_text,
                    created_at=asyncio.get_running_loop().time(),
                )

                self._pending_task = asyncio.create_task(
                    self._generate_summary(
                        existing_summary=state.summary or "",
                        dropped_text=dropped_text,
                        available_tokens=int(available_tokens),
                        messages_dropped=len(surviving_overflow),
                    )
                )

        # 6. Non-Blocking Fallback or Normal Return
        if self._pending_task is not None and overflow_messages:
            # Fallback path: Inject the raw overflow so the flagship never stalls.
            # CRITICAL: We only inject the surviving messages, excluding the pruned ones.
            raw_fallback: List[Dict[str, str]] = []
            for m in surviving_overflow: # <-- Note the change to surviving_overflow
                role_str = m.role.value if hasattr(m.role, "value") else str(m.role)
                content = m.content
                if role_str == "tool":
                    content = f"Tool '{m.name}' Observation:\n{m.content}\n\nWhat is your next action?"
                    role_str = "user"
                raw_fallback.append({"role": role_str, "content": content})

            if raw_fallback:
                raw_fallback[0]["content"] = (
                    "[Earlier context summarized asynchronously — summary pending]\n"
                    + raw_fallback[0]["content"]
                )

            return core_context + summary_context + raw_fallback + retained_history
        return core_context + summary_context + retained_history

    def _classify_for_pruning(
        self, messages: List[Any]
    ) -> tuple[List[Any], List[PruneCandidate]]:
        """Split overflow into (surviving, pruned) using deterministic heuristics.

        BREADCRUMB TRADE-OFF:
        We DO NOT prune general tool errors or dead-end searches here. The 
        Stale Observation Trimmer already reduces these to ~15-token breadcrumbs 
        (e.g., "[Stale Observation Trimmed...] status=error"). 
        Deleting those breadcrumbs would save negligible tokens but cause the 
        LLM summarizer to permanently forget the failure, leading to infinite 
        retry loops.

        We ONLY prune exact-match superseded retries (Rule 3), as a later 
        identical attempt makes the earlier one entirely redundant.
        """
        if not messages:
            return messages, []

        pruned: List[PruneCandidate] = []
        surviving: List[Any] = []

        # Rule 3: Identify superseded retries (exact content match + same tool name)
        # O(n^2) pre-pass, but n is small (only overflow messages) and string 
        # equality check is a fast C-level memcmp.
        superseded_ids: set[int] = set()
        
        for i in range(len(messages) - 1):
            for j in range(i + 1, len(messages)):
                a, b = messages[i], messages[j]
                
                # Check for Role.TOOL dynamically based on your object structure
                a_role = a.role.value if hasattr(a.role, "value") else str(a.role)
                b_role = b.role.value if hasattr(b.role, "value") else str(b.role)
                
                if (
                    a_role == "tool"
                    and b_role == "tool"
                    and getattr(a, "name", None) == getattr(b, "name", None)
                    and a.content == b.content
                ):
                    superseded_ids.add(id(a))
                    break  # Found a later duplicate, move to next 'a'

        for msg in messages:
            msg_id = id(msg)
            if msg_id in superseded_ids:
                pruned.append(PruneCandidate(msg_id, "superseded_retry", len(msg.content or "")))
            else:
                surviving.append(msg)

        return surviving, pruned
