import asyncio
import json

import os
from services.orchestrator.src.llm.provider import UniversalLLMProvider
import re
from typing import Any, Dict, List, Optional
import uuid

from loguru import logger
from opentelemetry import trace

# Import from refactored modules
from services.orchestrator.src.agent.state import AgentState, Message, Role
from services.orchestrator.src.tools.dispatcher import ToolDispatcher
from services.orchestrator.src.tools.registry import ToolRegistry
from services.orchestrator.src.memory.context_manager import SlidingContextManager

tracer = trace.get_tracer(__name__)


class TokenBudgetExceededError(RuntimeError):
    """Raised when the cumulative token counter trips the governance circuit breaker
    so that the agent loop can be cleanly aborted and reported."""

    def __init__(self, cumulative_total: int, budget: int) -> None:
        self.cumulative_total = cumulative_total
        self.budget = budget
        super().__init__(
            f"Token budget circuit breaker tripped: cumulative {cumulative_total} "
            f"tokens exceeded the configured budget of {budget}."
        )


class Agent:
    def __init__(self, llm_provider, config: Dict[str, Any], uds_server=None, workspace_root: Optional[str] = None, permission_callback=None):
        self.llm_provider = llm_provider
        self.uds_server = uds_server
        self.workspace_root = workspace_root or os.getcwd()
        self.sandbox_config = config.get("sandbox", {})
        self.search_config = config.get("search", {})
        self.auto_approve = False
        self.user_inbox = asyncio.Queue()
        self.state = AgentState(user_goal="")
        
        llm_config = config.get("llm", {})
        memory_config = config.get("memory", {})
        max_tokens = memory_config.get("max_tokens", 6000)
        dynamic_char_limit = int(max_tokens * 3.5 * 0.8)
        
        self.dispatcher = ToolDispatcher(
            uds_server=uds_server, 
            workspace_root=self.workspace_root,
            permission_callback=permission_callback,
            sandbox_config=self.sandbox_config,
            max_file_read_chars=dynamic_char_limit
        )      
        self.tool_registry = ToolRegistry(uds_server=uds_server, config=config)
        self.max_iterations = config.get("max_iterations", 25)
        
        # ------------------------------------------------------------------
        # AI Governance: Model Routing (Phase C)
        # ------------------------------------------------------------------
        # The reasoning loop keeps `self.llm_provider` (flagship model).
        # Context compaction is a low-stakes task and is routed to a smaller,
        # faster, cheaper model selected via the optional nested
        # `llm.compaction` block:
        #
        #   "llm": {
        #     "model_name": "qwen3.8",
        #     "endpoint_url": "...",
        #     "api_key": "...",
        #     "compaction": { "model_name": "qwen2.5:1.5b" }
        #   }
        #
        # Inheritance rules (locked in the design session):
        #   - `endpoint_url` and `api_key` inherit from the parent llm block
        #     when omitted in the compaction block, so operators only change
        #     `model_name`.
        #   - `chars_per_token` inherits from the parent llm block when
        #     omitted.
        #   - If no compaction block (or no `model_name`) is present, the
        #     SlidingContextManager keeps using the flagship provider and
        #     behavior is identical to the pre-routing build.
        # ------------------------------------------------------------------
        compaction_cfg: Dict[str, Any] = llm_config.get("compaction") or {}
        compaction_provider: Any = self.llm_provider  # default: flagship fallback
        compaction_model_name: str = llm_config.get("model_name", "llama3:8b")

        if compaction_cfg.get("model_name"):
            compaction_endpoint: Optional[str] = (
                compaction_cfg.get("endpoint_url") or llm_config.get("endpoint_url")
            )
            compaction_api_key: Optional[str] = compaction_cfg.get(
                "api_key", llm_config.get("api_key")
            )
            if not compaction_endpoint:
                logger.warning(
                    "\U0001f4a1 [Model Routing] compaction.model_name is set but "
                    "endpoint_url is missing in both the compaction block and the "
                    "parent llm block. Falling back to the flagship provider for "
                    "context compaction."
                )
            else:
                compaction_provider = UniversalLLMProvider(
                    endpoint_url=compaction_endpoint,
                    model=compaction_cfg["model_name"],
                    api_key=compaction_api_key,
                    chars_per_token=float(
                        compaction_cfg.get(
                            "chars_per_token",
                            llm_config.get("chars_per_token", 3.5),
                        )
                    ),
                )
                compaction_model_name = compaction_cfg["model_name"]
                logger.info(
                    f"\U0001f504 [Model Routing] Context compaction routed to "
                    f"'{compaction_model_name}' "
                    f"(chars/token={compaction_provider.chars_per_token}), "
                    f"reasoning stays on flagship."
                )

        # Extract the store from the uds_server instance
        message_store = uds_server.message_store if uds_server else None

        self.memory = SlidingContextManager(
            memory_config=memory_config,
            model_name=compaction_model_name,
            llm_provider=compaction_provider,
            llm_config=llm_config,
            state=self.state,  # Assuming state injection is updated in ContextManager
            max_tokens=memory_config.get("max_tokens", 6000),
            summary_budget=4096,
            message_store=message_store  # Inject here
        )

        # ------------------------------------------------------------------
        # AI Governance: Token Budgets (Economic Governance)
        # ------------------------------------------------------------------
        # A running total of tokens consumed by THIS AGENT INSTANCE across
        # every LLM call. When the cumulative total crosses the configured
        # budget, `_record_token_usage` trips a circuit breaker so platform
        # operators can see exactly when (and by which span) the limit was
        # crossed.
        # ------------------------------------------------------------------
        governance_config = config.get("governance", {})
        self.token_budget: int = int(governance_config.get("token_budget", 0))  # 0 == unlimited
        self.cumulative_token_count: int = 0
        self._token_call_count: int = 0

    async def _save_message(self, msg: Message, token_count: int = 0):
        if self.uds_server and getattr(self.uds_server, 'message_store', None):
            try:
                # Serialize the content for SQLite so JSON.parse() in React works cleanly
                content_json = json.dumps(msg.content)
                role_str = msg.role.value if hasattr(msg.role, 'value') else str(msg.role)
                await self.uds_server.message_store.append(
                    session_id=self.state.run_id,
                    seq=len(self.state.history) - 1,
                    role=role_str,
                    content_json=content_json,
                    token_count=token_count
                )
            except Exception as e:
                logger.error(f"SQLite DB Write Error: {e}")

    def _record_token_usage(
        self,
        prompt_tokens: int,
        completion_tokens: int,
        span: Optional[trace.Span] = None,
    ) -> None:
        """Governance hook: enrich the current span with OpenInference token
        count attributes, update the cumulative counter, and trip the circuit
        breaker if the corporate token budget has been exceeded.

        OpenInference semantic conventions: https://github.com/Arize-ai/openinference
        - llm.token_count.prompt
        - llm.token_count.completion
        - llm.token_count.total

        These exact span attributes are read to compute per-call cost
        against the model pricing table configured in the workspace settings.
        """
        total = int(prompt_tokens) + int(completion_tokens)

        # 1. Enrich the current OTLP span (Arize Phoenix shows these in the
        #    "LLM Call" panel and uses them to compute $-cost per call).
        active_span: trace.Span = (
            span if span is not None
            else trace.get_current_span()
        )
        if active_span.is_recording():
            active_span.set_attribute("llm.token_count.prompt", int(prompt_tokens))
            active_span.set_attribute("llm.token_count.completion", int(completion_tokens))
            active_span.set_attribute("llm.token_count.total", total)

        # 2. Accumulate across the agent's lifecycle (the corporate view).
        self._token_call_count += 1
        self.cumulative_token_count += total

        # 3. Emit the running total on the same span so Phoenix can show a
        #    trend line of cumulative consumption independent of per-call cost.
        if active_span.is_recording():
            active_span.set_attribute("governance.cumulative_token_count", int(self.cumulative_token_count))
            active_span.set_attribute("governance.token_call_index", int(self._token_call_count))

        logger.debug(
            f"[Governance] LLM call #{self._token_call_count}: "
            f"prompt={prompt_tokens}, completion={completion_tokens}, "
            f"cumulative={self.cumulative_token_count}"
            + (f" / budget={self.token_budget}" if self.token_budget else " / unlimited")
        )

        # 4. Circuit breaker: abort further work before we waste more budget.
        if self.token_budget > 0 and self.cumulative_token_count > self.token_budget:
            if active_span.is_recording():
                active_span.set_attribute("governance.budget_exceeded", True)
                active_span.set_attribute("governance.token_budget", int(self.token_budget))
                active_span.set_status(
                    trace.Status(
                        trace.StatusCode.ERROR,
                        f"Token budget exceeded ({self.cumulative_token_count} > {self.token_budget})",
                    )
                )
            logger.warning(
                f"\u26a0\ufe0f [Governance] Circuit breaker: cumulative tokens "
                f"{self.cumulative_token_count} exceeded budget {self.token_budget}. "
                "Aborting agent loop."
            )
            raise TokenBudgetExceededError(
                cumulative_total=self.cumulative_token_count,
                budget=self.token_budget,
            )

    async def reason(self, context: List[Dict[str, str]]) -> Dict[str, Any]:
        cptr: float = getattr(self.memory, "chars_per_token", 3.5)
        prompt_chars = sum(len(msg.get("content", "")) for msg in context)
        prompt_tokens = max(1, int(prompt_chars / cptr))

        with tracer.start_as_current_span("llm_call") as llm_span:
            # --- Input-side attributes (known before the call) ---
            llm_span.set_attribute("llm.provider", type(self.llm_provider).__name__)
            llm_span.set_attribute("llm.token_count.prompt", int(prompt_tokens))

            # --- Perform the LLM call INSIDE the active span so the
            #     OpenAI auto-instrumentor nests its span beneath llm_call ---
            try:
                raw_response = await asyncio.to_thread(self.llm_provider.generate, context)
            except Exception as exc:
                llm_span.set_status(
                    trace.Status(trace.StatusCode.ERROR, f"LLM call failed: {exc}")
                )
                llm_span.record_exception(exc)
                llm_span.set_attribute("llm.token_count.completion", 0)
                llm_span.set_attribute("llm.token_count.total", int(prompt_tokens))
                raise

            # --- Output-side attributes (known after the call) ---
            completion_tokens = max(1, int(len(raw_response or "") / cptr))
            self._record_token_usage(
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
                span=llm_span,
            )

        logger.info(f"\U0001f9e0 [Reasoning] LLM Output:\n{raw_response}")

        clean_response = raw_response.strip()

        markdown_match = re.search(r'```(?:json)?\s*(\{.*?\})\s*```', clean_response, re.DOTALL)
        if markdown_match:
            clean_response = markdown_match.group(1)

        try:
            return json.loads(clean_response)
        except json.JSONDecodeError:
            pass

        match = re.search(r'\{.*\}', raw_response, re.DOTALL)
        if match:
            try:
                return json.loads(match.group(0))
            except json.JSONDecodeError:
                pass
           
        return {
            "reasoning": "Failed to parse JSON. Remember to output ONLY a single valid JSON object.", 
            "tool": "error", 
            "tool_args": {"raw": raw_response}
        }

    async def run(
        self,
        user_goal: str,
        ui_callback=None,
        auto_approve: bool = False,
        workflow_config: Optional[Dict[str, Any]] = None,
        history_payload: list = None
    ) -> AgentState:
        with tracer.start_as_current_span("agent_execution_loop") as parent_span:
            self.auto_approve = auto_approve

            # --- Trigger Async DB Rehydration Before Generating Context ---
            if getattr(self.memory, 'rehydrate', None):
                 await self.memory.rehydrate()

            def log(msg: str):
                if ui_callback:
                    ui_callback(msg)
                else:
                    logger.info(msg)

            if history_payload and not self.state.history:
                log("\U0001f504 [System] Restoring session context from UI...")
                self.state.user_goal = history_payload[0].get("content", "") if history_payload else user_goal
                
                for msg_data in history_payload:
                    ui_role = msg_data.get("role", "user").lower()
                    if ui_role == "thought":
                        continue
                    role_str = "user"
                    if ui_role == "agent":
                        role_str = "assistant"
                    elif ui_role == "error":
                        role_str = "tool"  
                    elif ui_role == "user":
                        role_str = "user"
                    try:
                        msg = Message(role=Role(role_str), content=msg_data.get("content", ""), name=msg_data.get("name", "ui_rehydration"))
                        self.state.history.append(msg)
                        await self._save_message(msg)
                    except ValueError:
                        continue

            self.state.is_complete = False
            self.state.is_canceled = False
            if user_goal:
                is_followup = len(self.state.history) > 0
                if not is_followup:
                    # Preserve self.state.run_id during session restoration.
                    current_run_id = self.state.run_id
                    self.state = AgentState(user_goal=user_goal) 
                    self.state.run_id = current_run_id

                    self.state.user_goal = user_goal
                    self.state.iterations = 0
                    self.state.max_iterations = self.max_iterations 
                    self.state.summary = ""
                    # Generate a new UUID only if one wasn't injected from the UI
                    if not self.state.run_id:
                        self.state.run_id = str(uuid.uuid4())

                    self.state.workflow_context = None
                    log(f"[bold cyan]\U0001f680 --- Starting Agent Loop ---[/bold cyan]\nGoal: {user_goal}")
                else:
                    self.state.iterations = 0
                    self.state.max_iterations = self.max_iterations
                    log(f"[bold cyan]\u25b6\ufe0f --- Continuing Agent Loop ---[/bold cyan]\nFollow-up: {user_goal}")

                msg = Message(role=Role.USER, content=user_goal)
                self.state.history.append(msg)
                await self._save_message(msg)

                if workflow_config:
                    orch_cfg = workflow_config.get("orchestrator_config", {})
                    if "max_iterations" in orch_cfg:
                        self.state.max_iterations = orch_cfg["max_iterations"]
                    if "sandbox" in orch_cfg:
                        self.sandbox_config.update(orch_cfg["sandbox"])
                        self.dispatcher.sandbox_config = self.sandbox_config
                    if "search" in orch_cfg:
                        self.search_config.update(orch_cfg["search"])
                    self.state.workflow_context = workflow_config.get("context_engineering", {})
                    for action in workflow_config.get("pre_flight_actions", []):
                        tool = action.get("tool")
                        args = action.get("args", {})
                        log(f"[bold yellow]\u2708\ufe0f [Pre-Flight][/bold yellow] Dispatching {tool}...")
                        if tool in self.tool_registry.tools:
                            obs = await self.tool_registry.execute_tool_async(tool, args)
                        else:
                            obs = await self.dispatcher.execute_async(tool, args, auto_approve=True)
                        msg = Message(role=Role.TOOL, content=f"Pre-flight Observation: {obs}", name=tool)
                        self.state.history.append(msg)
                        await self._save_message(msg)
            else:
                log("[bold cyan]\u25b6\ufe0f --- Resuming Agent Loop (No New Prompt) ---[/bold cyan]")

            parent_span.set_attribute("agent.run_id", self.state.run_id)
            parent_span.set_attribute("agent.user_goal", user_goal or "Resumed")

            my_run_id = self.state.run_id

            with logger.contextualize(run_id=self.state.run_id):
                budget_exhausted = False
                tool_descriptions = "\n".join([f"- {name}: {info['description']}" for name, info in self.tool_registry.tools.items()])

                # --- Load system prompt from external template file ---
                from services.orchestrator.src.prompts import load_prompt
                _system_tpl = load_prompt("agent_system_v1")
                system_prompt = _system_tpl.format(
                    tool_descriptions=tool_descriptions,
                    workspace_root=self.workspace_root,
                )

                if getattr(self.state, "workflow_context", None):
                    ctx = self.state.workflow_context
                    if ctx:
                        system_prompt += f"""
                        
                        WORKFLOW CONSTRAINTS & ROLE:
                        - ROLE: {ctx.get('role', '')}
                        - TASK: {ctx.get('task', '')}
                        - CONSTRAINTS: {json.dumps(ctx.get('constraints', []))}
                        - FAILURE BEHAVIOR: {ctx.get('failure_behavior', '')}
                        - OUTPUT CONTRACT: {ctx.get('output_contract', '')}
                        """
                while not self.state.is_complete and not self.state.is_canceled and not budget_exhausted and self.state.iterations < self.state.max_iterations:
                    with tracer.start_as_current_span(f"iteration_{self.state.iterations}") as iter_span:
                        log(f"\n[dim]\U0001f504 --- Iteration {self.state.iterations + 1} ---[/dim]")

                        # --- HITL: Drain Mid-Task User Input ---
                        while not self.user_inbox.empty():
                            injected_msg = self.user_inbox.get_nowait()
                            formatted_input = f"\u26a0\ufe0f [User Input Interjected]: {injected_msg}"
                            msg = Message(role=Role.USER, content=formatted_input)
                            self.state.history.append(msg)
                            await self._save_message(msg)
                            log(f"\U0001f4e5 [System] Injected user input into context window.")
                        
                        # --- 1. Evaluate Reflection (No System Prompt Mutation) ---
                        needs_reflection = False
                        if self.state.iterations > 0 and self.state.iterations % 3 == 0:
                            needs_reflection = True
                        elif self.state.history and self.state.history[-1].role == Role.TOOL:
                            last_obs = self.state.history[-1].content.lower()
                            if "error" in last_obs or ("exit code" in last_obs and "exit code 0" not in last_obs):
                                needs_reflection = True

                        if self.uds_server and needs_reflection:
                            await self.uds_server.send_notification(
                                "agent_status", 
                                {"status": "reflecting", "message": "Critique Required: Evaluating recent actions..."}
                            )

                        # --- 2. Build Base Context (Layer 1 Static + Layer 2 History) ---
                        # Pass the perfectly static system_prompt to the memory manager
                        context = self.memory.build_safe_context(self.state, system_prompt)

                        # --- 3. Append Volatile State (Layer 3 Dynamic) ---
                        # Inject transient environment variables and reflections as a temporary user block
                        volatile_state = f"[System State]\nAuto-Approve: {self.auto_approve}\n"
                        
                        if needs_reflection:
                            volatile_state += "\nCRITIQUE REQUIRED: Review your last observations. Did your last action succeed? State your revised approach before calling the next tool."

                        # 🎯 INVERT PROMPT STRUCTURE: Place the JSON contract at the absolute end
                        volatile_state += "\n\nFINAL DIRECTIVE: Your output MUST be a single valid JSON object with EXACTLY these keys: \"reasoning\" (string), \"tool\" (string), and \"tool_args\" (dictionary)."

                        context.append({"role": "user", "content": volatile_state.strip()})

                        # --- AI Governance: Fold compaction overhead into cumulative budget ---
                        compaction_overhead = getattr(self.memory, "last_compaction_token_overhead", 0)
                        if compaction_overhead > 0:
                            self._record_token_usage(
                                prompt_tokens=compaction_overhead,
                                completion_tokens=0,
                                span=iter_span,
                            )
                            self.memory.last_compaction_token_overhead = 0

                        # --- AI Governance: Token Budget Circuit Breaker ---
                        try:
                            llm_response = await self.reason(context)
                        except TokenBudgetExceededError as tb_e:
                            budget_exhausted = True
                            clean_budget_msg = (
                                f"\U0001f6d1 [Governance] Agent aborted: cumulative token usage "
                                f"({tb_e.cumulative_total}) exceeded the configured "
                                f"budget ({tb_e.budget}). Platform operator must raise "
                                f"the ceiling or audit the workload."
                            )
                            log(clean_budget_msg)
                            self.state.is_complete = True
                            self.state.summary = clean_budget_msg
                            msg = Message(role=Role.TOOL, content=clean_budget_msg, name="governance_budget_breaker")
                            self.state.history.append(msg)
                            await self._save_message(msg)
                            iter_span.set_attribute("governance.budget_exceeded", True)
                            iter_span.set_attribute("governance.cumulative_token_count", int(self.cumulative_token_count))
                            iter_span.set_attribute("governance.token_budget", int(self.token_budget))
                            iter_span.set_status(
                                trace.Status(
                                    trace.StatusCode.ERROR,
                                    "Token budget circuit breaker tripped",
                                )
                            )
                            break
                        # --- end governance guard ---

                        if self.state.run_id != my_run_id:
                            log("\U0001f47b [Agent] Aborting orphaned ghost loop (new task started).")
                            raise asyncio.CancelledError("Ghost loop aborted.")

                        if self.state.is_canceled:
                            log("\U0001f6d1 [Agent] Task was manually cancelled by the user.")
                            break

                        msg = Message(role=Role.ASSISTANT, content=json.dumps(llm_response))
                        self.state.history.append(msg)
                        await self._save_message(msg)

                        tool_name = llm_response.get("tool", "unknown")
                        tool_args = llm_response.get("tool_args", {})

                        iter_span.set_attribute("tool.name", tool_name)
                        iter_span.set_attribute("agent.reasoning", llm_response.get("reasoning", ""))

                        if isinstance(tool_args, str):
                            log(f"[dim]\u26a0\ufe0f Auto-correcting malformed tool_args string into a dictionary...[/dim]")
                            if tool_name == "terminal_proxy":
                                tool_args = {"command": tool_args}
                            elif tool_name == "math_operation":
                                tool_args = {"expression": tool_args}
                            else:
                                tool_args = {}

                        reasoning = llm_response.get("reasoning", "No reasoning provided.")
                        log(f"[bold magenta]\U0001f9e0 [Reasoning][/bold magenta] {reasoning}")
                        
                        if self.uds_server:
                            # [Compaction Status Relay] Proactively surface a compaction-disabled alert to the UI.
                            try:
                                if hasattr(self.memory, 'compaction_status'):
                                    _comp_status = self.memory.compaction_status()
                                    if _comp_status and _comp_status.get('disabled') and not getattr(self, '_compaction_alert_sent', False):
                                        self._compaction_alert_sent = True
                                        logger.warning("[Compaction-Disabled] Relaying alert to UI: %s", _comp_status.get('reason'))
                                        await self.uds_server.send_notification(
                                            "agent_status",
                                            {"status": "compaction_disabled", "message": _comp_status.get("reason", "Compaction disabled"),
                                             "fix_hint": _comp_status.get("fix_hint", ""), "model": _comp_status.get("model", "")}
                                        )
                            except Exception as _comp_alert_exc:
                                logger.debug("Compaction status relay failed: %s", _comp_alert_exc)

                            await self.uds_server.send_notification(
                                "agent_status", 
                                {"status": "thinking", "message": reasoning}
                            )

                        log(f"[bold yellow]\U0001f527 [Dispatching][/bold yellow] {tool_name} with args: {tool_args}")

                        # 5.4 RAG Context Relevance Tracking
                        if tool_name in ["apply_inline_diff", "file_system"] and isinstance(tool_args, dict) and "file_path" in tool_args:
                            iter_span.set_attribute("rag.context_utilized_file", tool_args.get("file_path"))

                        if tool_name == "finish_task":
                            self.state.is_complete = True
                            summary = tool_args.get("summary", "Task completed successfully.")
                            log(f"[bold green]\u2705 [Task Complete][/bold green] {summary}")
                            self.state.summary = summary 
                            msg = Message(role=Role.TOOL, content=summary, name=tool_name)
                            self.state.history.append(msg)
                            await self._save_message(msg)
                            
                            success_flag = tool_args.get("success", True)
                            iter_span.set_attribute("task.success", success_flag)
                            if not success_flag:
                                iter_span.set_status(trace.Status(trace.StatusCode.ERROR, "Task reported failure"))
                            break

                        dispatcher_tools = ["file_system", "terminal_proxy", "python_repl", "apply_inline_diff"]
                        
                        if tool_name not in self.tool_registry.tools and tool_name != "finish_task":
                            observation = f"Error: Tool '{tool_name}' is disabled or not recognized in this workflow."
                        elif tool_name in dispatcher_tools:
                            observation = await self.dispatcher.execute_async(tool_name, tool_args, auto_approve=self.auto_approve)
                        else:
                            if tool_name == "web_search":
                                tool_args["run_id"] = self.state.run_id
                                tool_args["search_config"] = self.search_config
                                tool_args["max_chars"] = getattr(self.dispatcher, "max_file_read_chars", 4000)
                            observation = await self.tool_registry.execute_tool_async(tool_name, tool_args)      

                        log(f"[bold blue]\U0001f440 [Observation][/bold blue]\n{observation}")

                        obs_str = str(observation).lower()
                        is_violation = (
                            obs_str.startswith("error: command blocked by sandbox") or
                            obs_str.startswith("security violation:")
                        )
                        
                        if is_violation:
                            log("\u26a0\ufe0f [System] Sandbox violation detected. Requesting override from user...")
                            
                            bypass_approved = False
                            if self.uds_server:
                                bypass_res = await self.uds_server.request_client_context(
                                    "request_sandbox_bypass",
                                    {"tool_name": tool_name, "observation": observation}
                                )
                                bypass_approved = bypass_res.get("approved", False)
                                
                            if bypass_approved:
                                log("\u2705 User approved sandbox bypass. Retrying action...")
                                old_strict = self.dispatcher.sandbox_config.get("strict_mode", True)
                                self.dispatcher.sandbox_config["strict_mode"] = False
                                try:
                                    if tool_name in dispatcher_tools:
                                        observation = await self.dispatcher.execute_async(tool_name, tool_args, auto_approve=auto_approve)
                                    else:
                                        observation = await self.tool_registry.execute_tool_async(tool_name, tool_args)
                                finally:
                                    self.dispatcher.sandbox_config["strict_mode"] = old_strict
                                is_violation = False
                            else:
                                log("\U0001f6d1 [System] Sandbox bypass denied. Forcing agent termination.")
                                self.state.is_complete = True
                                clean_error_msg = "Task aborted by system sandbox constraints. The requested action was blocked for security reasons."
                                self.state.summary = clean_error_msg
                                msg = Message(role=Role.TOOL, content=self.state.summary, name="finish_task")
                                self.state.history.append(msg)
                                await self._save_message(msg)
                                
                                iter_span.set_attribute("error", True)
                                iter_span.set_attribute("error.message", "Sandbox constraint violation")
                                iter_span.set_status(trace.Status(trace.StatusCode.ERROR, "Sandbox constraint violation"))
                                break

                        if tool_name == "error":
                            reasoning_text = str(llm_response.get("reasoning", ""))
                            if "Connection Error" in reasoning_text:
                                log("\U0001f6d1 [Circuit Breaker] LLM provider is unreachable. Aborting loop.")
                                break
                            elif "Failed to parse JSON" in reasoning_text:
                                log("\U0001f6d1 [Circuit Breaker] LLM produced invalid JSON (likely context overflow). Aborting loop to prevent runaway errors.")
                                self.state.is_complete = True
                                self.state.summary = "Task aborted: The local engine failed to produce valid JSON, likely due to context window exhaustion."
                                break

                        # --- Pre-Compaction Heuristic Noise Tagging ---
                        obs_lower = str(observation).lower()
                        is_noise = any(
                            pattern in obs_lower
                            for pattern in (
                                "blocked by sandbox",
                                "security violation",
                                "blocked",
                                "forbidden",
                                "denied",
                                "outside authorized workspace",
                                "json parse error",
                                "failed to parse json",
                                "json.decoder",
                                "jsondecodeerror",
                                "file not found",
                                "no such file",
                                "not found: file",
                                "no such directory",
                            )
                        )
                        msg = Message(role=Role.TOOL, content=str(observation), name=tool_name, is_noise=is_noise)
                        self.state.history.append(msg)
                        await self._save_message(msg)
                        if is_noise:
                            logger.debug(f"[Noise-Tag] Observation from '{tool_name}' flagged as noise for pre-compaction filtering.")
                        self.state.iterations += 1

            if not self.state.is_complete and not self.state.is_canceled:
                log("\n\u26a0\ufe0f --- Agent Loop Terminated (Max Iterations Reached) ---")

                if parent_span.is_recording():
                    parent_span.set_status(
                        trace.Status(
                            trace.StatusCode.ERROR,
                            f"Agent loop terminated: max_iterations ({self.state.max_iterations}) "
                            f"reached after {self.state.iterations} iterations without completion.",
                        )
                    )
                    parent_span.set_attribute("governance.termination_reason", "max_iterations_reached")
                    parent_span.set_attribute("governance.max_iterations", int(self.state.max_iterations))
                    parent_span.set_attribute("governance.iterations_executed", int(self.state.iterations))
                    parent_span.set_attribute("governance.cumulative_token_count_at_termination", int(self.cumulative_token_count))

                self.state.summary = (
                    f"\u26a0\ufe0f Agent loop terminated: max_iterations limit "
                    f"({self.state.max_iterations}) reached. The agent was unable "
                    f"to complete the task within the allocated iteration budget."
                )

            elif self.state.is_canceled:
                if parent_span.is_recording():
                    parent_span.set_status(
                        trace.Status(
                            trace.StatusCode.ERROR,
                            "Agent loop was manually cancelled by the user."
                        )
                    )
                    parent_span.set_attribute("governance.termination_reason", "user_cancelled")
                log("\n\U0001f6d1 --- Agent Loop Cancelled ---")

            else:
                if parent_span.is_recording():
                    parent_span.set_status(trace.Status(trace.StatusCode.OK))
                log("\n\U0001f3c1 --- Agent Loop Completed ---")

            return self.state

    def is_path_authorized(self, target_path: str) -> bool:
        sandbox_config = self.sandbox_config
        if not sandbox_config.get("strict_mode", True):
            return True

        abs_target = os.path.abspath(os.path.expanduser(target_path))
        abs_workspace = os.path.abspath(self.workspace_root)

        if abs_target.startswith(abs_workspace):
            return True

        allowed_paths = sandbox_config.get("allowed_external_paths", [])
        for allowed_dir in allowed_paths:
            abs_allowed = os.path.abspath(os.path.expanduser(allowed_dir))
            if abs_target.startswith(abs_allowed):
                return True

        return False
