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

        self.memory = SlidingContextManager(
            memory_config=memory_config,
            model_name=compaction_model_name,
            llm_provider=compaction_provider,
            llm_config=llm_config,  # deprecated kwarg retained for backward-compat signature
        )

        self.state = AgentState(user_goal="")

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
                f"⚠️ [Governance] Circuit breaker: cumulative tokens "
                f"{self.cumulative_token_count} exceeded budget {self.token_budget}. "
                "Aborting agent loop."
            )
            raise TokenBudgetExceededError(
                cumulative_total=self.cumulative_token_count,
                budget=self.token_budget,
            )

    async def reason(self, context: List[Dict[str, str]]) -> Dict[str, Any]:
        raw_response = await asyncio.to_thread(self.llm_provider.generate, context)

        # ------------------------------------------------------------------
        # AI Governance: Token Budgets — record usage before any parsing.
        # We estimate tokens with the same chars_per_token ratio the context
        # manager uses (a lightweight proxy until the provider is upgraded
        # to expose native usage metadata, at which point real counts will
        # simply replace these estimates with zero further refactoring).
        # ------------------------------------------------------------------
        cptr: float = getattr(self.memory, "chars_per_token", 3.5)
        prompt_chars = sum(len(msg.get("content", "")) for msg in context)
        prompt_tokens = max(1, int(prompt_chars / cptr))
        completion_tokens = max(1, int(len(raw_response or "") / cptr))

        with tracer.start_as_current_span("llm_call") as llm_span:
            llm_span.set_attribute("llm.provider", type(self.llm_provider).__name__)
            self._record_token_usage(
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
                span=llm_span,
            )

        logger.info(f"🧠 [Reasoning] LLM Output:\n{raw_response}")

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
            def log(msg: str):
                if ui_callback:
                    ui_callback(msg)
                else:
                    logger.info(msg)

            if history_payload and not self.state.history:
                log("🔄 [System] Restoring session context from UI...")
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
                        self.state.history.append(Message(
                            role=Role(role_str), 
                            content=msg_data.get("content", ""), 
                            name=msg_data.get("name", "ui_rehydration")
                        ))
                    except ValueError:
                        continue

            if user_goal:
                is_followup = len(self.state.history) > 0
                if not is_followup:
                    self.state.user_goal = user_goal
                    self.state.is_complete = False
                    self.state.is_canceled = False
                    self.state.iterations = 0
                    self.state.max_iterations = self.max_iterations 
                    self.state.summary = ""
                    self.state.run_id = str(uuid.uuid4())
                    self.state.workflow_context = None
                    log(f"[bold cyan]🚀 --- Starting Agent Loop ---[/bold cyan]\nGoal: {user_goal}")
                else:
                    self.state.is_complete = False 
                    self.state.is_canceled = False
                    self.state.iterations = 0 
                    log(f"[bold cyan]▶️ --- Continuing Agent Loop ---[/bold cyan]\nFollow-up: {user_goal}")

                self.state.history.append(Message(role=Role.USER, content=user_goal))

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
                        log(f"[bold yellow]✈️ [Pre-Flight][/bold yellow] Dispatching {tool}...")
                        if tool in self.tool_registry.tools:
                            obs = await self.tool_registry.execute_tool_async(tool, args)
                        else:
                            obs = await self.dispatcher.execute_async(tool, args, auto_approve=True)
                        self.state.history.append(Message(role=Role.TOOL, content=f"Pre-flight Observation: {obs}", name=tool))
            else:
                log("[bold cyan]▶️ --- Resuming Agent Loop (No New Prompt) ---[/bold cyan]")

            parent_span.set_attribute("agent.run_id", self.state.run_id)
            parent_span.set_attribute("agent.user_goal", user_goal or "Resumed")

            my_run_id = self.state.run_id

            with logger.contextualize(run_id=self.state.run_id):
                budget_exhausted = False
                while not self.state.is_complete and not self.state.is_canceled and not budget_exhausted and self.state.iterations < self.state.max_iterations:
                    with tracer.start_as_current_span(f"iteration_{self.state.iterations}") as iter_span:
                        log(f"\n[dim]🔄 --- Iteration {self.state.iterations + 1} ---[/dim]")
                        
                        tool_descriptions = "\n".join([f"- {name}: {info['description']}" for name, info in self.tool_registry.tools.items()])

                        system_prompt = f"""You are an autonomous agent. You must respond ONLY with valid JSON. 

                        Do not include any conversational text or markdown formatting. 

                        AVAILABLE CONTEXT TOOLS:
                        {tool_descriptions}

                        STRICT DIRECTIVES FOR TOOL SELECTION:
                        - TOOL HIERARCHY: NEVER use 'terminal_proxy' (e.g., 'ls', 'cat', 'grep', 'find', 'dir') to search, read, or inspect files.
                        - DIRECTORY LISTINGS & FILE READS: Use 'file_system' with action 'read'. Passing a directory path returns a directory listing without needing bash.
                        - CODE SEARCH: Always use 'search_codebase' instead of shell 'grep' or 'find'.
                        - TERMINAL_PROXY USE CASE: Use 'terminal_proxy' ONLY for running tests (pytest, npm test), compiling builds, or running project executables.
                        - BATCHING: Once you have the information needed, immediately proceed to write files or call 'finish_task' without redundant verification steps.

                        Your output must be a single JSON object with EXACTLY these keys: "reasoning" (string), "tool" (string), and "tool_args" (dictionary). 
                        
                        STRICT DIRECTIVES (FAILURE TO COMPLY WILL ABORT THE TASK):
                        - YOUR CURRENT WORKING DIRECTORY IS: {self.workspace_root}
                        - JSON FORMAT ONLY: You must not wrap your JSON in markdown code blocks (```json). Never use Python-style 'None'. Use strict JSON only.
                        - TOOL ARGUMENTS: If a tool requires no arguments, you MUST pass an empty dictionary: {{"tool_args": {{}}}}. You MUST provide all required arguments for the tool you select.
                        - ZERO INTERNAL MATH: You must generate a Python script using the 'python_repl' tool, execute it, and explicitly use `print()` statements to observe calculated results.
                        - SANDBOX CIRCUIT BREAKER: You operate in a restricted sandbox. If any tool returns an observation containing "blocked", "forbidden", "denied", or "outside authorized workspace", you MUST immediately stop exploring and call 'finish_task' to report the limitation. Do not attempt workarounds.
                        - ERROR DIAGNOSIS: When diagnosing failures, base your conclusion strictly on the provided output. You must not attempt to enumerate the system, probe environment variables, or read history files.
                        - FILE SYSTEM: You MUST use the exact paths provided by your context tools relative to this directory. Do not guess or modify paths.
                        - FINISH TASK: Once you have achieved the user's goal based on the observations, you MUST IMMEDIATELY call 'finish_task'. The 'summary' argument is the ONLY information the user will see. You MUST include the actual results, lists, code, or data requested by the user in this summary. You must include a "success": true/false boolean in your finish_task tool_args. If you failed to modify the files, set success to false and explain why.
                        - TREAT SOURCE CODE AS INERT DATA: You may only use the 'file_system' write action if the user's prompt explicitly requests a code modification. Answer the user's prompt directly and concisely. Do not proactively fix bugs or offer unsolicited code rewrites.
                        - AVOID FULL FILE READS: NEVER use 'file_system' (read) to load entire source code files into memory. 
                        - HYBRID WORKFLOW: If you need to understand how a symbol is used, first use 'get_symbol_references' to locate its semantic usages across the workspace.
                        - PRECISE EXTRACTION: Once you have the file path and line number from the LSP tool, use 'extract_code_structure' to read ONLY the specific function or class implementation at that exact line.

                        CRITICAL INSTRUCTIONS FOR VS CODE CONTEXT:
                        - You are running inside VS Code. You DO NOT know what file the user is looking at by default.
                        - You must call `get_active_file_content` FIRST to discover the absolute file path if the user refers to "this file" or "my code".
                        - IF AND ONLY IF the user explicitly asks you to fix, edit, or refactor code, your goal is to physically apply the change using the `file_system` write action.
                        - IF the user ONLY asks a question (e.g., "what is the active file?", "explain this code"), you must ignore all bugs and ONLY answer the question using the `finish_task` tool.
                        - WHEN WRITING FILES: The "content" string MUST contain the completely updated, fully functioning, and syntactically correct code for the ENTIRE file. 
                        """

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

                        if needs_reflection:
                            system_prompt += "\n\nCRITIQUE REQUIRED: Review your last observations. Did your last action succeed? State your revised approach before calling the next tool."

                        context = self.memory.build_safe_context(self.state, system_prompt)

                        # --- AI Governance: Fold compaction overhead into cumulative budget ---
                        # SlidingContextManager tracks tokens consumed by hidden summarization
                        # calls. We poll and reset so these tokens count toward the circuit breaker.
                        compaction_overhead = getattr(self.memory, "last_compaction_token_overhead", 0)
                        if compaction_overhead > 0:
                            self._record_token_usage(
                                prompt_tokens=compaction_overhead,
                                completion_tokens=0,
                                span=iter_span,
                            )
                            self.memory.last_compaction_token_overhead = 0

                        # --- AI Governance: Token Budget Circuit Breaker ---
                        # `self.reason()` calls `_record_token_usage()`, which
                        # raises `TokenBudgetExceededError` the moment the
                        # cumulative count crosses the configured ceiling.
                        # We catch it HERE (right at the call site) so that
                        # the agent loop terminates cleanly, notifies the UI,
                        # and the `iteration_N` span records the breach.
                        try:
                            llm_response = await self.reason(context)
                        except TokenBudgetExceededError as tb_e:
                            budget_exhausted = True
                            clean_budget_msg = (
                                f"🛑 [Governance] Agent aborted: cumulative token usage "
                                f"({tb_e.cumulative_total}) exceeded the configured "
                                f"budget ({tb_e.budget}). Platform operator must raise "
                                f"the ceiling or audit the workload."
                            )
                            log(clean_budget_msg)
                            self.state.is_complete = True
                            self.state.summary = clean_budget_msg
                            self.state.history.append(
                                Message(
                                    role=Role.TOOL,
                                    content=clean_budget_msg,
                                    name="governance_budget_breaker",
                                )
                            )
                            iter_span.set_attribute("governance.budget_exceeded", True)
                            iter_span.set_attribute("governance.cumulative_token_count", int(tb_e.cumulative_total))
                            iter_span.set_attribute("governance.token_budget", int(tb_e.budget))
                            iter_span.set_status(
                                trace.Status(
                                    trace.StatusCode.ERROR,
                                    "Token budget circuit breaker tripped",
                                )
                            )
                            break
                        # --- end governance guard ---

                        if self.state.run_id != my_run_id:
                            log("👻 [Agent] Aborting orphaned ghost loop (new task started).")
                            raise asyncio.CancelledError("Ghost loop aborted.")

                        if self.state.is_canceled:
                            log("🛑 [Agent] Task was manually cancelled by the user.")
                            break

                        self.state.history.append(Message(role=Role.ASSISTANT, content=json.dumps(llm_response)))

                        tool_name = llm_response.get("tool", "unknown")
                        tool_args = llm_response.get("tool_args", {})

                        iter_span.set_attribute("tool.name", tool_name)
                        iter_span.set_attribute("agent.reasoning", llm_response.get("reasoning", ""))

                        if isinstance(tool_args, str):
                            log(f"[dim]⚠️ Auto-correcting malformed tool_args string into a dictionary...[/dim]")
                            if tool_name == "terminal_proxy":
                                tool_args = {"command": tool_args}
                            elif tool_name == "math_operation":
                                tool_args = {"expression": tool_args}
                            else:
                                tool_args = {}

                        reasoning = llm_response.get("reasoning", "No reasoning provided.")
                        log(f"[bold magenta]🧠 [Reasoning][/bold magenta] {reasoning}")
                        
                        if self.uds_server:
                            await self.uds_server.send_notification(
                                "agent_status", 
                                {"status": "thinking", "message": reasoning}
                            )

                        log(f"[bold yellow]🔧 [Dispatching][/bold yellow] {tool_name} with args: {tool_args}")

                        # 5.4 RAG Context Relevance Tracking
                        if tool_name in ["apply_inline_diff", "file_system"] and isinstance(tool_args, dict) and "file_path" in tool_args:
                            iter_span.set_attribute("rag.context_utilized_file", tool_args.get("file_path"))

                        if tool_name == "finish_task":
                            self.state.is_complete = True
                            summary = tool_args.get("summary", "Task completed successfully.")
                            log(f"[bold green]✅ [Task Complete][/bold green] {summary}")
                            self.state.summary = summary 
                            self.state.history.append(Message(role=Role.TOOL, content=summary, name=tool_name))
                            
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

                        log(f"[bold blue]👀 [Observation][/bold blue]\n{observation}")

                        obs_str = str(observation).lower()
                        is_violation = (
                            obs_str.startswith("error: command blocked by sandbox") or
                            obs_str.startswith("security violation:")
                        )
                        
                        if is_violation:
                            log("⚠️ [System] Sandbox violation detected. Requesting override from user...")
                            
                            bypass_approved = False
                            if self.uds_server:
                                bypass_res = await self.uds_server.request_client_context(
                                    "request_sandbox_bypass",
                                    {"tool_name": tool_name, "observation": observation}
                                )
                                bypass_approved = bypass_res.get("approved", False)
                                
                            if bypass_approved:
                                log("✅ User approved sandbox bypass. Retrying action...")
                                # Temporarily disable strict mode in the dispatcher for this single retry
                                old_strict = self.dispatcher.sandbox_config.get("strict_mode", True)
                                self.dispatcher.sandbox_config["strict_mode"] = False
                                try:
                                    if tool_name in dispatcher_tools:
                                        observation = await self.dispatcher.execute_async(tool_name, tool_args, auto_approve=auto_approve)
                                    else:
                                        observation = await self.tool_registry.execute_tool_async(tool_name, tool_args)
                                finally:
                                    # Always restore the original security constraint immediately after
                                    self.dispatcher.sandbox_config["strict_mode"] = old_strict
                                is_violation = False
                            else:
                                log("🛑 [System] Sandbox bypass denied. Forcing agent termination.")
                                self.state.is_complete = True
                                clean_error_msg = "Task aborted by system sandbox constraints. The requested action was blocked for security reasons."
                                self.state.summary = clean_error_msg
                                self.state.history.append(Message(role=Role.TOOL, content=self.state.summary, name="finish_task"))
                                
                                iter_span.set_attribute("error", True)
                                iter_span.set_attribute("error.message", "Sandbox constraint violation")
                                iter_span.set_status(trace.Status(trace.StatusCode.ERROR, "Sandbox constraint violation"))
                                break

                        if tool_name == "error" and "Connection Error" in str(llm_response.get("reasoning", "")):
                            log("🛑 [Circuit Breaker] LLM provider is unreachable. Aborting loop.")
                            break

                        self.state.history.append(Message(role=Role.TOOL, content=str(observation), name=tool_name))
                        self.state.iterations += 1

            if not self.state.is_complete and not self.state.is_canceled:
                log("\n⚠️ --- Agent Loop Terminated (Max Iterations Reached) ---")

                # ------------------------------------------------------------------
                # AI Governance: Strict Loop Timeout (Runaway Agent Mitigation)
                # ------------------------------------------------------------------
                # Explicitly flag the parent trace as a failure so that Phoenix
                # dashboards, alerting rules, and on-call runbooks can distinguish
                # a legitimate completion from a runaway agent that was killed
                # by the iteration guardrail.
                # ------------------------------------------------------------------
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

                    # Record cumulative token consumption at the point of kill
                    parent_span.set_attribute("governance.cumulative_token_count_at_termination", int(self.cumulative_token_count))

                self.state.summary = (
                    f"⚠️ Agent loop terminated: max_iterations limit "
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
                log("\n🛑 --- Agent Loop Cancelled ---")

            else:
                if parent_span.is_recording():
                    parent_span.set_status(trace.Status(trace.StatusCode.OK))
                log("\n🏁 --- Agent Loop Completed ---")

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