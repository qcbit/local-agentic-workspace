import asyncio
import json
import logging
import os
import re
from typing import Any, Dict, List, Optional
import uuid

from services.orchestrator.src.agent.state import AgentState, Message, Role
from services.orchestrator.src.llm.provider import MockLLMProvider, UniversalLLMProvider
from services.orchestrator.src.memory.context_manager import SlidingContextManager
from services.orchestrator.src.tools.dispatcher import ToolDispatcher
from services.orchestrator.src.tools.registry import ToolRegistry

logger = logging.getLogger(__name__)

__all__ = [
    "Role",
    "Message",
    "AgentState",
    "UniversalLLMProvider",
    "MockLLMProvider",
    "Agent",
]


class Agent:
    """The central state machine managing the ReAct loop."""

    def __init__(
        self,
        llm_provider,
        config: Dict[str, Any],
        uds_server=None,
        workspace_root: Optional[str] = None,
        permission_callback=None,
    ):
        self.llm_provider = llm_provider
        self.uds_server = uds_server
        self.workspace_root = workspace_root or os.getcwd()
        self.sandbox_config = config.get("sandbox", {})
        self.search_config = config.get("search", {})

        llm_config = config.get("llm", {})
        memory_config = config.get("memory", {})
        max_tokens = memory_config.get("max_tokens", 6000)
        dynamic_char_limit = int(max_tokens * 3.5 * 0.8)

        self.dispatcher = ToolDispatcher(
            uds_server=uds_server,
            workspace_root=self.workspace_root,
            permission_callback=permission_callback,
            sandbox_config=self.sandbox_config,
            max_file_read_chars=dynamic_char_limit,
        )
        self.tool_registry = ToolRegistry(uds_server=uds_server)
        self.max_iterations = config.get("max_iterations", 25)

        self.memory = SlidingContextManager(
            memory_config=memory_config,
            model_name=llm_config.get("model_name", "llama3:8b"),
            llm_provider=self.llm_provider,
            llm_config=llm_config,
        )

        self.state = AgentState(user_goal="")

    async def reason(self, context: List[Dict[str, str]]) -> Dict[str, Any]:
        raw_response = await asyncio.to_thread(self.llm_provider.generate, context)
        logger.info(f"🧠 [Reasoning] LLM Output:\n{raw_response}")

        clean_response = raw_response.strip()

        markdown_match = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", clean_response, re.DOTALL)
        if markdown_match:
            clean_response = markdown_match.group(1)

        try:
            return json.loads(clean_response)
        except json.JSONDecodeError:
            pass

        match = re.search(r"\{.*\}", raw_response, re.DOTALL)
        if match:
            try:
                return json.loads(match.group(0))
            except json.JSONDecodeError:
                pass

        return {
            "reasoning": "Failed to parse JSON. Remember to output ONLY a single valid JSON object.",
            "tool": "error",
            "tool_args": {"raw": raw_response},
        }

    async def run(
        self,
        user_goal: str,
        ui_callback=None,
        auto_approve: bool = False,
        workflow_config: Optional[Dict[str, Any]] = None,
        history_payload: list = None,
    ) -> AgentState:
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

                try:
                    self.state.history.append(
                        Message(
                            role=Role(role_str),
                            content=msg_data.get("content", ""),
                            name=msg_data.get("name", "ui_rehydration"),
                        )
                    )
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
                logger.info(f"[bold cyan]🚀 --- Starting Agent Loop ---[/bold cyan]\nGoal: {user_goal}")
            else:
                self.state.is_complete = False
                self.state.is_canceled = False
                self.state.iterations = 0
                logger.info(f"[bold cyan]▶️ --- Continuing Agent Loop ---[/bold cyan]\nFollow-up: {user_goal}")

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
            logger.info("[bold cyan]▶️ --- Resuming Agent Loop (No New Prompt) ---[/bold cyan]")

        my_run_id = self.state.run_id

        while (
            not self.state.is_complete
            and not self.state.is_canceled
            and self.state.iterations < self.state.max_iterations
        ):
            log(f"\n[dim]🔄 --- Iteration {self.state.iterations + 1} ---[/dim]")

            tool_descriptions = "\n".join(
                [f"- {name}: {info['description']}" for name, info in self.tool_registry.tools.items()]
            )

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
- FINISH TASK: Once you have achieved the user's goal based on the observations, you MUST IMMEDIATELY call 'finish_task'. The 'summary' argument is the ONLY information the user will see. You MUST include the actual results, lists, code, or data requested by the user in this summary.
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
                    {"status": "reflecting", "message": "Critique Required: Evaluating recent actions..."},
                )

            if needs_reflection:
                system_prompt += "\n\nCRITIQUE REQUIRED: Review your last observations. Did your last action succeed? State your revised approach before calling the next tool."

            context = self.memory.build_safe_context(self.state, system_prompt)
            llm_response = await self.reason(context)

            if self.state.run_id != my_run_id:
                log("👻 [Agent] Aborting orphaned ghost loop (new task started).")
                raise asyncio.CancelledError("Ghost loop aborted.")

            if self.state.is_canceled:
                log("🛑 [Agent] Task was manually cancelled by the user.")
                break

            self.state.history.append(Message(role=Role.ASSISTANT, content=json.dumps(llm_response)))

            tool_name = llm_response.get("tool", "unknown")
            tool_args = llm_response.get("tool_args", {})

            if isinstance(tool_args, str):
                log("[dim]⚠️ Auto-correcting malformed tool_args string into a dictionary...[/dim]")
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
                    "agent_status", {"status": "thinking", "message": reasoning}
                )

            log(f"[bold yellow]🔧 [Dispatching][/bold yellow] {tool_name} with args: {tool_args}")

            if tool_name == "finish_task":
                self.state.is_complete = True
                summary = tool_args.get("summary", "Task completed successfully.")
                log(f"[bold green]✅ [Task Complete][/bold green] {summary}")
                self.state.summary = summary
                self.state.history.append(Message(role=Role.TOOL, content=summary, name=tool_name))
                break

            dispatcher_tools = ["file_system", "terminal_proxy", "python_repl", "apply_inline_diff"]

            if tool_name not in self.tool_registry.tools and tool_name != "finish_task":
                observation = f"Error: Tool '{tool_name}' is disabled or not recognized in this workflow."
            elif tool_name in dispatcher_tools:
                observation = await self.dispatcher.execute_async(tool_name, tool_args, auto_approve=auto_approve)
            else:
                if tool_name == "web_search":
                    tool_args["run_id"] = self.state.run_id
                    tool_args["search_config"] = self.search_config
                    tool_args["max_chars"] = getattr(self.dispatcher, "max_file_read_chars", 4000)
                observation = await self.tool_registry.execute_tool_async(tool_name, tool_args)

            log(f"[bold blue]👀 [Observation][/bold blue]\n{observation}")

            obs_str = str(observation).lower()
            is_violation = (
                obs_str.startswith("error: command blocked by sandbox")
                or obs_str.startswith("security violation:")
            )

            if is_violation:
                log("🛑 [System] Sandbox violation detected. Forcing agent termination.")
                self.state.is_complete = True
                clean_error_msg = "Task aborted by system sandbox constraints. The requested action was blocked for security reasons."
                self.state.summary = clean_error_msg
                self.state.history.append(Message(role=Role.TOOL, content=self.state.summary, name="finish_task"))
                break

            if tool_name == "error" and "Connection Error" in str(llm_response.get("reasoning", "")):
                log("🛑 [Circuit Breaker] LLM provider is unreachable. Aborting loop.")
                break

            self.state.history.append(Message(role=Role.TOOL, content=str(observation), name=tool_name))
            self.state.iterations += 1

        if not self.state.is_complete:
            log("\n⚠️ --- Agent Loop Terminated (Max Iterations Reached) ---")
        else:
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