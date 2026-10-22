"""Sliding window context manager with model-routed compaction.

The context manager no longer accepts a ``llm_config`` dict for token-estimation
parameters.  Instead it interrogates the assigned ``llm_provider`` for
``chars_per_token`` and ``model``, keeping the memory layer decoupled from the
config parser entirely.

Compaction health
-----------------
When the dedicated compaction model is unavailable (e.g. an Ollama model that
has not been ``pull``-ed yet), the manager used to fail *silently*, returning
the prior summary and quietly leaking context past the token window on every
overflow.  This is now a first-class, observable condition:

* ``compaction_disabled`` / ``compaction_disabled_reason`` flag the failure
* a single, prominent WARNING (deduplicated) is emitted for the UI/log pipeline
* ``compaction_status()`` lets the socket-owning orchestrator push the alert

The memory layer only *reports* the disabled state; relaying it to the VS Code
UI remains the orchestrator's job, preserving this module's decoupling.
"""

from typing import List, Dict, Any, Optional

import logging
from opentelemetry import trace

logger = logging.getLogger(__name__)

tracer = trace.get_tracer(__name__)

# Substrings that indicate the requested model is not present on the server
# (i.e. a "model not downloaded / not pulled" situation) rather than a transient
# network or generic API failure.  Matching is done case-insensitively.
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


class SlidingContextManager:
    """Manages the token window with a rolling summarization buffer."""

    def __init__(
        self,
        memory_config: Dict[str, Any],
        model_name: str,
        llm_provider: Any,
        llm_config: Optional[Dict[str, Any]] = None,  # deprecated; kept for call-site compat
    ) -> None:
        self.max_tokens = memory_config.get("max_tokens", 4000)
        self.llm_provider = llm_provider

        # --- Token-estimation ownership moved to the provider ---
        # The provider exposes .chars_per_token and .model so the memory layer
        # never needs to parse the llm config dictionary.
        self.chars_per_token: float = float(getattr(llm_provider, "chars_per_token", 3.5))
        self.model_name: str = getattr(llm_provider, "model", model_name or "")

        # Tracks the token cost of the most recent compaction event so the
        # Agent loop can fold it into its cumulative budget counter.
        # Reset to 0 by the Agent after each poll.
        self.last_compaction_token_overhead: int = 0

        # --- Compaction health (new) ---
        # Set True the moment a model-not-found condition is detected so the
        # orchestrator can proactively warn the user that compaction is disabled.
        self.compaction_disabled: bool = False
        self.compaction_disabled_reason: str = ""
        # Once a model-not-found warning has been surfaced for this session we
        # do not re-spam the UI on every subsequent context overflow.
        self._compaction_missing_warned: bool = False

        logger.info(
            f"\U0001f9e0 Context Manager initialized for '{self.model_name}' "
            f"(Ratio: {self.chars_per_token} chars/token, Max: {self.max_tokens})"
        )

    def _normalize_model_name(self, model_name: str) -> str:
        name = model_name.lower()
        if "llama3" in name:
            return "llama3:8b"
        if "qwen" in name:
            return "qwen2.5-coder"
        return name

    def _estimate_tokens(self, text: str) -> int:
        return max(1, int(len(text) / self.chars_per_token))

    @staticmethod
    def _is_model_missing_error(exc: BaseException) -> bool:
        """Return True if ``exc`` looks like a 'model not downloaded' failure.

        This is deliberately a heuristic over the exception's text; the goal is
to distinguish a recoverable "pull the model" situation from an opaque
network/SDK error so the UI can show an actionable, non-alarming message.
        """
        haystack = " ".join(
            str(part) for part in (getattr(exc, "args", ()) or ()) + (str(exc),) if part
        ).lower()
        return any(marker in haystack for marker in _MODEL_MISSING_MARKERS)

    def _raise_model_missing_alert(self, exc: BaseException) -> None:
        """Record the disabled state and surface a single, prominent warning.

        The alert is emitted exactly ONCE per manager instance so that repeated
        context overflows do not flood the UI.  The orchestrator may read
        :meth:`compaction_status` to relay the alert to the editor over the
        ``agent_status`` channel.
        """
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
            # Distinctive, machine-grippable prefix so the orchestrator / log
            # shipper can recognize this as a proactive compaction alert.
            logger.warning(
                "[Compaction-Disabled] %s",
                self.compaction_disabled_reason,
            )
        else:
            logger.debug(
                "[Compaction-Disabled] Recurring compaction failure suppressed: %s", exc
            )

    def compaction_status(self) -> Dict[str, Any]:
        """Snapshot of compaction health for the orchestrator / UI.

        Returns a plain dict the socket-owning layer can forward unchanged to a
        ``client.showErrorMessage`` / ``agent_status`` payload, e.g.::

            {
                "disabled": True,
                "reason": "Compaction is DISABLED: ...",
                "model": "qwen2.5:1.5b",
                "fix_hint": "ollama pull qwen2.5:1.5b"
            }
        """
        if not self.compaction_disabled:
            return {"disabled": False, "reason": "", "model": self.model_name, "fix_hint": ""}

        fix_hint = f"ollama pull {self.model_name}" if self._is_model_missing_for_model() else ""
        return {
            "disabled": True,
            "reason": self.compaction_disabled_reason,
            "model": self.model_name,
            "fix_hint": fix_hint,
        }

    def _is_model_missing_for_model(self) -> bool:
        """Helper used only for fix_hint computation (no exception available)."""
        # We only know the failure kind from the last recorded reason; reuse the
        # wording we stored rather than re-inspecting an exception that may be
        # long gone.
        return "not available on the local server" in self.compaction_disabled_reason

    def _generate_summary(
        self, existing_summary: str, dropped_messages: List[Any], available_tokens: int = 0
    ) -> str:
        """Calls the LLM in plain-text mode to summarize the dropped history.

        Wraps the LLM call in a dedicated ``context_compaction`` OpenTelemetry span
        to display compaction frequency, cost, and latency.

        OpenInference semantic conventions are applied:
        https://github.com/Arize-ai/openinference

        The ``llm.task`` attribute is set to ``context_compaction`` so that
        Phoenix dashboards can split reasoning vs. compaction spend.
        """
        dropped_text = "\n".join(
            f"{msg.role}: {msg.content}" for msg in dropped_messages
        )

        from services.orchestrator.src.prompts import load_prompt

        template = load_prompt("compaction_v1")
        prompt = template.format(
            existing_summary=existing_summary if existing_summary else "None",
            dropped_text=dropped_text,
        )

        context = [{"role": "system", "content": prompt}]

        # Token estimation uses the EXACT prompt string (no duplication/drift risk)
        prompt_tokens = self._estimate_tokens(prompt)

        with tracer.start_as_current_span("context_compaction") as compaction_span:
            # --- Task routing label (Phase B decision #4) ---
            compaction_span.set_attribute("llm.task", "context_compaction")

            # --- Identity & mechanism ---
            compaction_span.set_attribute("compaction.mechanism", "sliding_window")
            compaction_span.set_attribute("compaction.messages_dropped", len(dropped_messages))
            compaction_span.set_attribute("compaction.available_tokens_before", int(available_tokens))
            compaction_span.set_attribute("compaction.max_window_tokens", int(self.max_tokens))
            compaction_span.set_attribute("llm.provider", type(self.llm_provider).__name__)
            compaction_span.set_attribute(
                "llm.model_family", self._normalize_model_name(self.model_name)
            )

            try:
                response = self.llm_provider.generate(context, require_json=False).strip()
            except Exception as exc:
                # Explicitly mark the span as failed so Phoenix surfaces it
                # in error dashboards; prompt tokens were still consumed.
                compaction_span.set_status(
                    trace.Status(trace.StatusCode.ERROR, f"Compaction LLM call failed: {exc}")
                )
                compaction_span.record_exception(exc)
                compaction_span.set_attribute("llm.token_count.prompt", int(prompt_tokens))
                compaction_span.set_attribute("llm.token_count.completion", 0)
                compaction_span.set_attribute("llm.token_count.total", int(prompt_tokens))
                compaction_span.set_attribute("compaction.token_overhead", int(prompt_tokens))

                # NEW: detect-and-raise instead of failing silently. A dedicated
                # one-time warning is surfaced and the disabled state recorded so
                # the orchestrator can proactively alert the user in the UI.
                self._raise_model_missing_alert(exc)

                logger.error(f"Summarization failed: {exc}")
                self.last_compaction_token_overhead = int(prompt_tokens)
                return existing_summary

            # A successful summarization re-enables compaction; clear any prior
            # disabled flag (e.g. the model was pulled mid-session).
            if self.compaction_disabled:
                self.compaction_disabled = False
                self.compaction_disabled_reason = ""

            # --- Token accounting (from the actual response) ---
            completion_tokens = self._estimate_tokens(response)
            total_tokens = prompt_tokens + completion_tokens

            # OpenInference token count attributes (Phoenix reads these for $-cost)
            compaction_span.set_attribute("llm.token_count.prompt", int(prompt_tokens))
            compaction_span.set_attribute("llm.token_count.completion", int(completion_tokens))
            compaction_span.set_attribute("llm.token_count.total", int(total_tokens))

            # --- Governance overhead metadata ---
            compaction_span.set_attribute("compaction.summary_length_before", len(existing_summary or ""))
            compaction_span.set_attribute("compaction.summary_length_after", len(response or ""))
            compaction_span.set_attribute("compaction.token_overhead", int(total_tokens))

            logger.info(
                f"[Governance] Compaction: {prompt_tokens} prompt + "
                f"{completion_tokens} completion = {total_tokens} total tokens "
                f"for {len(dropped_messages)} dropped messages."
            )

            # Expose to the Agent loop's cumulative budget counter
            self.last_compaction_token_overhead = int(total_tokens)

            return response

    def build_safe_context(self, state: Any, system_prompt: str) -> List[Dict[str, str]]:
        # 1. Pin the Core Prompts
        core_context = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": state.user_goal},
        ]
        core_tokens = sum(self._estimate_tokens(msg["content"]) for msg in core_context)

        # 2. Pin the Running Summary (if it exists)
        summary_context = []
        summary_tokens = 0
        if state.summary:
            summary_context = [
                {"role": "system", "content": f"Summary of earlier actions:\n{state.summary}"}
            ]
            summary_tokens = self._estimate_tokens(summary_context[0]["content"])

        available_tokens = self.max_tokens - core_tokens - summary_tokens

        # 3. Process history newest to oldest
        retained_history = []
        dropped_history = []
        current_tokens = 0

        for msg in reversed(state.history):
            content = msg.content
            role_str = msg.role.value if hasattr(msg.role, "value") else str(msg.role)

            if role_str == "tool":
                content = (
                    f"Tool '{msg.name}' Observation:\n{msg.content}\n\n"
                    "What is your next action?"
                )
                mapped_role = "user"
            else:
                mapped_role = role_str

            msg_tokens = self._estimate_tokens(content)

            if current_tokens + msg_tokens > available_tokens:
                # Add to the front so they stay in chronological order when summarized
                dropped_history.insert(0, msg)
            else:
                retained_history.insert(0, {"role": mapped_role, "content": content})
                current_tokens += msg_tokens

        # 4. Handle dropped messages by generating a new summary
        if dropped_history:
            logger.info(
                f"Context limit reached. Summarizing and dropping {len(dropped_history)} old messages..."
            )

            new_summary = self._generate_summary(
                state.summary, dropped_history, available_tokens=int(available_tokens)
            )

            # Persist the new summary to the state
            state.summary = new_summary

            # Prune the state history so these messages aren't summarized again next time
            state.history = [msg for msg in state.history if msg not in dropped_history]

            # Rebuild the summary block for this current injection
            summary_context = [
                {"role": "system", "content": f"Summary of earlier actions:\n{state.summary}"}
            ]

        return core_context + summary_context + retained_history
