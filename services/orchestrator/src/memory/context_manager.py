import logging
from typing import List, Dict, Any

logger = logging.getLogger(__name__)

class SlidingContextManager:
    """Manages the token window with a rolling summarization buffer."""
    
    def __init__(self, memory_config: Dict[str, Any], model_name: str, llm_provider: Any):
        self.max_tokens = memory_config.get("max_tokens", 4000)
        self.llm_provider = llm_provider
        
        heuristics = memory_config.get("token_heuristics", {})
        base_model = self._normalize_model_name(model_name)
        self.chars_per_token = heuristics.get(base_model, heuristics.get("default", 3.5))
        
        logger.info(f"🧠 Context Manager initialized for '{model_name}' (Ratio: {self.chars_per_token} chars/token, Max: {self.max_tokens})")

    def _normalize_model_name(self, model_name: str) -> str:
        name = model_name.lower()
        if "llama3" in name: return "llama3:8b"
        if "qwen" in name: return "qwen2.5-coder"
        return name

    def _estimate_tokens(self, text: str) -> int:
        return max(1, int(len(text) / self.chars_per_token))

    def _generate_summary(self, existing_summary: str, dropped_messages: List[Any]) -> str:
        """Calls the LLM in plain-text mode to summarize the dropped history."""
        # Format the dropped messages for the LLM to read
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
        
        try:
            # Call the provider, explicitly turning OFF strict JSON mode
            return self.llm_provider.generate(context, require_json=False).strip()
        except Exception as e:
            logger.error(f"Summarization failed: {e}")
            return existing_summary

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
            new_summary = self._generate_summary(state.summary, dropped_history)
            
            # Persist the new summary to the state
            state.summary = new_summary
            
            # Prune the state history so these messages aren't summarized again next turn
            state.history = [msg for msg in state.history if msg not in dropped_history]
            
            # Rebuild the summary block for this current injection
            summary_context = [{"role": "system", "content": f"Summary of earlier actions:\n{state.summary}"}]

        return core_context + summary_context + retained_history
