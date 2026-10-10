import logging
from typing import List, Dict, Any, Optional

from opentelemetry import trace

logger = logging.getLogger(__name__)
tracer = trace.get_tracer(__name__)


class SlidingContextManager:
    """Manages the token window with a rolling summarization buffer."""

    def __init__(self, memory_config: Dict[str, Any], model_name: str, llm_provider: Any, llm_config: Optional[Dict[str, Any]] = None) -> None:
        self.max_tokens = memory_config.get("max_tokens", 4000)
        self.model_name = model_name
        self.llm_provider = llm_provider

        # Tracks the token cost of the most recent compaction event so the
        # Agent loop can fold it into its cumulative budget counter.
        # Reset to 0 by the Agent after each poll.
        self.last_compaction_token_overhead: int = 0

        if llm_config and "chars_per_token" in llm_config:
            self.chars_per_token: float = float(llm_config["chars_per_token"])
        else:
            # Set reasonable defaults based on common models
            if "llama3" in model_name.lower():
                self.chars_per_token: float = 4.0  # Llama3 typically around 4 chars/token
            elif "qwen" in model_name.lower():
                self.chars_per_token: float = 3.0  # Qwen models typically around 3 chars/token
            else:
                self.chars_per_token: float = 3.5  # Default fallback

        logger.info(f"🧠 Context Manager initialized for '{model_name}' (Ratio: {self.chars_per_token} chars/token, Max: {self.max_tokens})")

    def _normalize_model_name(self, model_name: str) -> str:
        name = model_name.lower()
        if "llama3" in name: return "llama3:8b"
        if "qwen" in name: return "qwen2.5-coder"
        return name

    def _estimate_tokens(self, text: str) -> int:
        return max(1, int(len(text) / self.chars_per_token))

    def _generate_summary(self, existing_summary: str, dropped_messages: List[Any], available_tokens: int = 0) -> str:
        """Calls the LLM in plain-text mode to summarize the dropped history.

        Wraps the LLM call in a dedicated ``context_compaction`` OpenTelemetry span
        to display compaction frequency, cost, and latency.

        OpenInference semantic conventions are applied:
        https://github.com/Arize-ai/openinference
        """
        dropped_text = "\n".join([f"{msg.role}: {msg.content}" for msg in dropped_messages])

        prompt = (
            "You are a system memory compressor. Your task is to update a running summary "
            "of an autonomous agent's actions.\n"
            f"Current Summary: {existing_summary if existing_summary else 'None'}\n\n"
            f"New Events to Incorporate:\n{dropped_text}\n\n"
            "Write a concise, factual, and updated paragraph summarizing the agent's progress. "
            "Do not use conversational filler."
        )

        context = [{"role": "system", "content": prompt}]

        # Token estimation uses the EXACT prompt string (no duplication/drift risk)
        prompt_tokens = self._estimate_tokens(prompt)

        with tracer.start_as_current_span("context_compaction") as compaction_span:
            # --- Identity & mechanism ---
            compaction_span.set_attribute("compaction.mechanism", "sliding_window")
            compaction_span.set_attribute("compaction.messages_dropped", len(dropped_messages))
            compaction_span.set_attribute("compaction.available_tokens_before", int(available_tokens))
            compaction_span.set_attribute("compaction.max_window_tokens", int(self.max_tokens))
            compaction_span.set_attribute("llm.provider", type(self.llm_provider).__name__)
            compaction_span.set_attribute("llm.model_family", self._normalize_model_name(self.model_name))

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

                logger.error(f"Summarization failed: {exc}")
                self.last_compaction_token_overhead = int(prompt_tokens)
                return existing_summary

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
            {"role": "user", "content": state.user_goal}
        ]
        core_tokens = sum(self._estimate_tokens(msg["content"]) for msg in core_context)

        # 2. Pin the Running Summary (if it exists)
        summary_context = []
        summary_tokens = 0
        if state.summary:
            summary_context = [{"role": "system", "content": f"Summary of earlier actions:\n{state.summary}"}]
            summary_tokens = self._estimate_tokens(summary_context[0]["content"])

        available_tokens = self.max_tokens - core_tokens - summary_tokens

        # 3. Process history newest to oldest
        retained_history = []
        dropped_history = []
        current_tokens = 0

        for msg in reversed(state.history):
            content = msg.content
            role_str = msg.role.value if hasattr(msg.role, 'value') else str(msg.role)

            if role_str == "tool":
                content = f"Tool '{msg.name}' Observation:\n{msg.content}\n\nWhat is your next action?"
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
            logger.info(f"Context limit reached. Summarizing and dropping {len(dropped_history)} old messages...")

            # The span, token accounting, and governance attributes are all
            # handled internally by _generate_summary() using the EXACT prompt
            # string, eliminating duplication/drift risk.
            new_summary = self._generate_summary(
                state.summary, dropped_history, available_tokens=int(available_tokens)
            )

            # Persist the new summary to the state
            state.summary = new_summary

            # Prune the state history so these messages aren't summarized again next time
            state.history = [msg for msg in state.history if msg not in dropped_history]

            # Rebuild the summary block for this current injection
            summary_context = [{"role": "system", "content": f"Summary of earlier actions:\n{state.summary}"}]

        return core_context + summary_context + retained_history
