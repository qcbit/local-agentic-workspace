import ast
import asyncio
from dataclasses import dataclass, field
from enum import Enum
import json
import logging
import operator
import os
from pathlib import Path
import re
from services.orchestrator.src.rag.vector_store import LocalVectorStore
from services.orchestrator.src.memory.context_manager import SlidingContextManager
import shlex
import subprocess
import sys
import time
from typing import Any, AsyncGenerator, Dict, List, Optional
import urllib.request
import urllib.error
import uuid

logger = logging.getLogger(__name__)

def get_python_interpreter() -> str:
    """Finds a valid Python interpreter, avoiding PyInstaller binary wrappers."""
    # 1. If running standard python, sys.executable is safe
    if not getattr(sys, 'frozen', False):
        return sys.executable
        
    # 2. If running as a PyInstaller bundle, sys.executable points to the binary.
    # We must search the host system for a real python interpreter.
    possible_interpreters = []
    
    if os.name == 'nt': # Windows
        possible_interpreters = ['python', 'python3', 'py']
    else: # macOS / Linux
        possible_interpreters = ['python3', '/usr/bin/python3', '/usr/local/bin/python3']
        
    for interp in possible_interpreters:
        try:
            # Verify the interpreter actually works
            result = subprocess.run([interp, "--version"], capture_output=True, text=True, timeout=2)
            if result.returncode == 0:
                return interp
        except Exception:
            continue
            
    # Fallback default
    return 'python3'

def execute_python_repl(code: str, timeout: int = 5) -> str:
    """Executes Python code in a sandboxed child process with a strict timeout."""
    if not code:
        return "Error: No code provided."

    forbidden_modules = {"os", "sys", "subprocess", "shutil", "pty", "socket", "pathlib"}
    
    # 1. AST Sandbox Security Check
    try:
        tree = ast.parse(code)
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name.split('.')[0] in forbidden_modules:
                        return f"Error: Import of forbidden module '{alias.name}' is blocked by sandbox."
            elif isinstance(node, ast.ImportFrom):
                if node.module and node.module.split('.')[0] in forbidden_modules:
                    return f"Error: Import from forbidden module '{node.module}' is blocked by sandbox."
    except SyntaxError as e:
        return f"SyntaxError in provided code: {e}"

    # 2. Execution via Isolated Child Process using a real interpreter
    python_bin = get_python_interpreter()

    try:
        result = subprocess.run(
            [python_bin, "-c", code],
            capture_output=True,
            text=True,
            timeout=timeout
        )
        
        output = result.stdout
        if result.stderr:
            output += f"\n--- STDERR ---\n{result.stderr}"
            
        return output.strip() if output.strip() else "Execution successful (no standard output). Did you forget to print()?"
        
    except subprocess.TimeoutExpired:
        return f"Error: Execution timed out after {timeout} seconds. Infinite loop prevented."
    except Exception as e:
        return f"Error executing python code: {str(e)}"

def validate_terminal_command(workspace_root: str, command_str: str) -> tuple[bool, str]:
    """
    Scans a shell command for path arguments that escape the workspace root.
    """
    try:
        tokens = shlex.split(command_str)
    except Exception:
        return False, "Error: Invalid or unparseable shell command syntax."

    safe_root = Path(workspace_root).resolve(strict=True)

    for token in tokens:
        if token.startswith("/") or token.startswith("~") or ".." in token:
            expanded_token = os.path.expanduser(token)
            resolved_path = Path(expanded_token).resolve()
            
            # Remove the exists() check! Block it if it points outside, period.
            try:
                resolved_path.relative_to(safe_root)
            except ValueError:
                return False, (
                    f"Error: Command blocked by sandbox. "
                    f"Target path '{token}' is outside authorized workspace root."
                )

    return True, ""

# --- State Definitions ---

class Role(str, Enum):
    USER = "user"
    ASSISTANT = "assistant"
    SYSTEM = "system"
    TOOL = "tool"

@dataclass
class Message:
    role: Role
    content: str
    name: Optional[str] = None

@dataclass
class AgentState:
    user_goal: str
    history: List[Message] = field(default_factory=list)
    is_complete: bool = False
    is_canceled: bool = False
    iterations: int = 0
    max_iterations: int = 10
    summary: str = ""  # to track the running summary 
    run_id: str = "" # Unique execution token

# --- Tool Dispatcher ---

def is_path_safe(workspace_root: str, target_path: str) -> bool:
    """Strictly sandboxes file paths to the workspace root."""
    try:
        safe_root = Path(workspace_root).resolve(strict=True)
        target = Path(target_path).resolve()
        
        # Throws ValueError if target is outside safe_root
        target.relative_to(safe_root)
        return True
    except (ValueError, RuntimeError):
        return False

class ToolDispatcher:
    """Handles structured JSON tool requests with a Tiered Operational Rights Proxy."""
    
    def __init__(self, uds_server=None, workspace_root: Optional[str] = None, permission_callback=None):
        self.uds_server = uds_server
        # Default to current directory if not provided
        self.workspace_root = workspace_root or os.getcwd() 
        self.permission_callback = permission_callback # Store the UI callback
        # Strict deny-list for highly destructive or interactive commands
        self.shell_deny_list = [
            "rm", "sudo", "mkfs", "fdisk", "dd", "chown", "chmod", 
            "shutdown", "reboot", "ufw", "iptables", "firewall-cmd", 
            "nano", "vim", "top", "history"
        ]

    async def execute_async(self, tool_name: str, arguments: Dict[str, Any], auto_approve: bool = False) -> str:
        logger.info(f"🔧 [Tool Call] Dispatching '{tool_name}' with args: {arguments} (Auto-Approve: {auto_approve})")
        
        try:
            if tool_name == "file_system":
                return await self._handle_file_system_async(arguments, auto_approve=auto_approve)
            elif tool_name == "terminal_proxy":
                return await self._handle_terminal_proxy_async(arguments, auto_approve=auto_approve)
            elif tool_name == "python_repl":
                return execute_python_repl(arguments.get("code", ""))
            elif tool_name == "finish_task":
                return "Task marked as complete by the agent."
            else:
                return f"Error: Tool '{tool_name}' not recognized."
        except Exception as e:
            return f"Error executing {tool_name}: {str(e)}"

    async def _handle_file_system_async(self, args: Dict[str, Any], auto_approve: bool = False) -> str:
        action = args.get("action")
        path = args.get("path", ".")
        
        # 🛡️ SANDBOX ENFORCEMENT 
        if not is_path_safe(self.workspace_root, path):
            return f"Error: Access denied. Path '{path}' is outside the authorized workspace sandbox."

        # TIER 1: Read-only actions (Auto-Approve)
        if action == "read":
            if os.path.isdir(path):
                files = os.listdir(path)
                return f"Directory listing for '{path}': {json.dumps(files)}"
            elif os.path.isfile(path):
                with open(path, "r", encoding="utf-8") as f:
                    content = f.read()
                # --- Protect the Context Window ---
                max_chars = 4000 
                if len(content) > max_chars:
                    return (
                        f"File content of '{path}' (TRUNCATED - File is too large):\n"
                        f"{content[:max_chars]}\n\n"
                        f"...[TRUNCATED]... The file is too large to read entirely. "
                        f"You MUST use the 'search_codebase' tool to query specific parts of this file."
                    )    
                return f"File content of '{path}':\n{content}"
            else:
                return f"Error: Path '{path}' does not exist."
                
        # TIER 2: File writes (Requires VS Code Staged Diff Approval)
        elif action == "write":
            content = args.get("content", "")

            # 🚀 AUTO-APPROVE BYPASS
            if auto_approve:
                logger.info(f"⚡ [Auto-Approve] Silently writing to '{path}'...")
                with open(path, "w", encoding="utf-8") as f:
                    f.write(content)
                return f"Successfully wrote to file '{path}'."
            
            if not self.uds_server:
                return "Error: Cannot request write permission. IPC Server not attached."
                
            logger.info(f"⏸️  [Proxy] Requesting write permission for '{path}'...")
            # Suspend and ask VS Code for permission
            response = await self.uds_server.request_client_context("request_write_permission", {
                "path": path,
                "content": content
            })
            
            # -> NEW: Explicitly check if the UI timed out
            if "content" in response and "timed out" in response["content"]:
                return response["content"]
            
            if response.get("status") == "approved":
                with open(path, "w", encoding="utf-8") as f:
                    f.write(content)
                return f"Successfully wrote to file '{path}'."
            else:
                return f"Action Blocked: The user denied the file write request for '{path}'."
        
        return f"Error: Unsupported file system action '{action}'."

    async def _handle_terminal_proxy_async(self, args: Dict[str, Any], auto_approve: bool = False) -> str:
        command = args.get("command")
        if not command:
            return "Error: No command provided."
            
        # 🛡️ 1. SANDBOX CHECK (Always runs)
        is_safe, error_msg = validate_terminal_command(self.workspace_root, command)
        if not is_safe:
            logger.warning(f"🔒 [Sandbox Blocked] Command: '{command}'")
            return error_msg

        # 🛡️ 2. DENY-LIST CHECK (Always runs)
        command_lower = command.lower()
        if any(forbidden in command_lower.split() for forbidden in self.shell_deny_list):
             return f"SECURITY VIOLATION: Command execution blocked. '{command}' contains forbidden keywords."
            
        # 🚀 AUTO-APPROVE BYPASS
        if auto_approve:
            logger.info(f"⚡ [Auto-Approve] Silently executing '{command}'...")
            user_timeout = 30 # Default to 30 seconds when silently auto-approving
            
            try:
                process = await asyncio.create_subprocess_shell(
                    command,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    cwd=self.workspace_root
                )
                stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=user_timeout)
                stdout_str = stdout.decode('utf-8')
                stderr_str = stderr.decode('utf-8')
                output = stdout_str if process.returncode == 0 else stderr_str
                return f"Command exit code {process.returncode}.\nOutput:\n{output}"
            except asyncio.TimeoutError:
                try:
                    process.kill()
                except ProcessLookupError:
                    pass
                return f"Error: Command execution timed out after {user_timeout} seconds."

        # TIER 3: Shell commands (Requires Explicit Modal Confirmation)
        if self.permission_callback:
            logger.info(f"⏸️  [Proxy] Requesting TUI permission for '{command}'...")
            is_approved = await self.permission_callback(f"Allow shell execution:\n\n{command}")
            
            if is_approved:
                # TUI fallback does not support dynamic timeouts yet, fallback to standard subprocess
                result = subprocess.run(command, shell=True, capture_output=True, text=True, cwd=self.workspace_root)
                output = result.stdout if result.returncode == 0 else result.stderr
                return f"Command exit code {result.returncode}.\nOutput:\n{output}"
            else:
                return "Action Blocked: The user denied the shell execution request."

        if not self.uds_server:
            return "Error: Cannot request shell permission. IPC Server not attached."
            
        logger.info(f"⏸️  [Proxy] Requesting shell execution permission for '{command}'...")
        response = await self.uds_server.request_client_context("request_shell_permission", {
            "command": command
        })
        
        if "content" in response and "timed out" in response["content"]:
            return response["content"]
        
        if response.get("status") == "approved":
            # 🎯 Extract custom timeout, fallback to 30s
            try:
                user_timeout = int(response.get("timeout", 30))
            except (ValueError, TypeError):
                user_timeout = 30
                
            try:
                process = await asyncio.create_subprocess_shell(
                    command,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    cwd=self.workspace_root
                )
                stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=user_timeout)
                stdout_str = stdout.decode('utf-8')
                stderr_str = stderr.decode('utf-8')
                output = stdout_str if process.returncode == 0 else stderr_str
                return f"Command exit code {process.returncode}.\nOutput:\n{output}"
            except asyncio.TimeoutError:
                try:
                    process.kill()
                except ProcessLookupError:
                    pass
                return f"Error: Command execution timed out after {user_timeout} seconds."
        else:
            return "Action Blocked: The user denied the shell execution request."

# --- Tool Registry ---

class ToolRegistry:
    def __init__(self, uds_server=None):
        # We pass in a reference to the UDS Server so the ToolRegistry can
        # trigger the reverse-request over the socket to VS Code.
        self.uds_server = uds_server
        self.vector_store = LocalVectorStore() 
        
        self.tools = {
            "vscode_command": {
                "name": "vscode_command",
                "description": "Executes a native VS Code command. Use 'vscode.openFolder' to open a directory workspace, or 'vscode.open' to open a specific file in the editor.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "command": {
                            "type": "string",
                            "description": "The VS Code command ID (e.g., 'vscode.openFolder' or 'vscode.open')"
                        },
                        "target_path": {
                            "type": "string",
                            "description": "The absolute path to the file or folder"
                        }
                    },
                    "required": ["command", "target_path"]
                }
            },
            "python_repl": {
                "name": "python_repl",
                "description": "A sandboxed Python environment. Use this to execute Python code for mathematical calculations, data formatting, and complex logic. You MUST use print() to output results so they can be read.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "code": {
                            "type": "string",
                            "description": "The Python script to execute."
                        }
                    },
                    "required": ["code"]
                }
            },
            "search_codebase": {
                "name": "search_codebase",
                "description": "Searches the local codebase using semantic vector embeddings. Use this when you need to find where a function, class, or variable is defined, or to understand how a specific part of the local project works.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "query": {
                            "type": "string",
                            "description": "The semantic search query"
                        }
                    },
                    "required": ["query"]
                }
            },
            "get_active_file_content": {
                "name": "get_active_file_content",
                "description": "Retrieves the full source code text currently visible in the user's active VS Code editor window.",
                "parameters": {
                    "type": "object",
                    "properties": {}
                }
            },
            "get_selected_text": {
                "name": "get_selected_text",
                "description": "Retrieves the specific string of text the user currently has highlighted/selected in VS Code.",
                "parameters": {
                    "type": "object",
                    "properties": {}
                }
            }
        }

        if self.uds_server is None:
            # Strip out VS Code tools if running in the standalone TUI
            for tool_name in ["get_active_file_content", "apply_inline_diff"]:
                self.tools.pop(tool_name, None)

            logger.info("🖥️ [TUI Mode] VS Code specific tools disabled.")

    async def execute_tool_async(self, tool_name: str, arguments: dict) -> Optional[str]:
        try:
            if tool_name == "search_codebase":
                query = arguments.get("query")
                if not isinstance(query, str) or not query.strip():
                    return "Error: search query must be a non-empty string."

                results = self.vector_store.semantic_search(query, limit=3)
                if not results:
                    return "No relevant codebase results found."
                
                formatted_response = "Codebase Search Results:\n\n"
                for i, res in enumerate(results):
                    formatted_response += f"--- Result {i+1} (File: {res.get('file_path')}) ---\n"
                    formatted_response += f"{res.get('content')}\n\n"
                return formatted_response

            elif tool_name == "vscode_command":
                if not self.uds_server:
                    return "Error: IPC Server not attached to ToolRegistry."
                
                # Forward the command request to VS Code
                response = await self.uds_server.request_client_context(tool_name, arguments)
                return response.get("content", "Error: No confirmation received from VS Code.")

            elif tool_name in ["get_active_file_content", "get_selected_text"]:
                if not self.uds_server:
                    return "Error: IPC Server not attached to ToolRegistry."
                
                # This is where the magic happens! We pause the agent and 
                # ask the UDS server to request data FROM Node.js
                response = await self.uds_server.request_client_context(tool_name)
                return response.get("content", "Error: No content received from VS Code.")

            elif tool_name == "python_repl":
                return execute_python_repl(arguments.get("code", ""))

            else:
                return f"Tool {tool_name} not found."
                
        except Exception as e:
            return f"Error executing {tool_name}: {str(e)}"

# --- Agent Core ---

class Agent:
    """The central state machine managing the ReAct loop."""

    def __init__(self, llm_provider, config: Dict[str, Any], uds_server=None, workspace_root: Optional[str] = None, permission_callback=None):
        self.llm_provider = llm_provider
        self.uds_server = uds_server
        self.workspace_root = workspace_root or os.getcwd()
        
        # Pass the workspace_root down to the dispatcher
        self.dispatcher = ToolDispatcher(
            uds_server=uds_server, 
            workspace_root=self.workspace_root,
            permission_callback=permission_callback
        )      
        self.tool_registry = ToolRegistry(uds_server=uds_server)
        self.max_iterations = 10
        
        llm_config = config.get("llm", {})
        memory_config = config.get("memory", {})
        
        self.memory = SlidingContextManager(
            memory_config=memory_config,
            model_name=llm_config.get("model_name", "llama3:8b"),
            llm_provider=self.llm_provider,
        )

        # 🎯 Initialize the state ONCE so history survives multiple turns
        self.state = AgentState(user_goal="")

    async def reason(self, context: List[Dict[str, str]]) -> Dict[str, Any]:
        """Invokes the LLM and parses the structured JSON response."""

        # OFF-LOAD TO THREAD: Prevents freezing the Textual UI
        raw_response = await asyncio.to_thread(self.llm_provider.generate,context)
        logger.info(f"🧠 [Reasoning] LLM Output:\n{raw_response}")

        clean_response = raw_response.strip()

        # 1. Strip markdown code block formatting if the LLM hallucinated it
        markdown_match = re.search(r'```(?:json)?\s*(\{.*?\})\s*```', clean_response, re.DOTALL)
        if markdown_match:
            clean_response = markdown_match.group(1)

        # 2. Try parsing the cleaned response directly
        try:
            return json.loads(clean_response)
        except json.JSONDecodeError:
            pass

        # Try to find the first JSON object in the string using regex
        match = re.search(r'\{.*\}', raw_response, re.DOTALL)
        if match:
            try:
                return json.loads(match.group(0))
            except json.JSONDecodeError:
                pass # Fall through to the error handler below
           
        return {
            "reasoning": "Failed to parse JSON. Remember to output ONLY a single valid JSON object.", 
            "tool": "error", 
            "tool_args": {"raw": raw_response}
        }

    # 2. Make the run method ASYNC so we can await the IPC socket tools
    async def run(self, user_goal: str, ui_callback=None, auto_approve: bool = False) -> AgentState:
        """The Main Agent Loop: Context -> Reason -> Tool -> Observation -> State Update."""

        # Helper function to print to terminal AND the Textual UI
        def log(msg: str):
            if ui_callback:
                ui_callback(msg)
            else:
                logger.info(msg)

        # 🎯 Reset iteration counters for the new turn, but KEEP the history
        self.state.user_goal = user_goal
        self.state.is_complete = False
        self.state.is_canceled = False
        self.state.iterations = 0
        self.state.summary = ""
        self.state.run_id = str(uuid.uuid4())
        my_run_id = self.state.run_id

        # 🎯 Append the new prompt as a USER message to the history
        self.state.history.append(Message(role=Role.USER, content=user_goal))

        log(f"[bold cyan]🚀 --- Starting Agent Loop ---[/bold cyan]\nGoal: {user_goal}")

        while not self.state.is_complete and not self.state.is_canceled and self.state.iterations < self.state.max_iterations:
            log(f"\n[dim]🔄 --- Iteration {self.state.iterations + 1} ---[/dim]")
            
            # Fetch dynamically from our properly named tool_registry
            tool_descriptions = "\n".join([f"- {name}: {info['description']}" for name, info in self.tool_registry.tools.items()])

            # 3. Use an f-string so {tool_descriptions} actually gets injected!
            # Note the double brackets {{ }} to escape JSON schema syntax inside an f-string.
            system_prompt = f"""You are an autonomous agent. You must respond ONLY with valid JSON. 

            Do not include any conversational text or markdown formatting. 
            You have access to the following tools:
            1. 'terminal_proxy' - args: {{"command": "<bash command>"}}
            2. 'file_system' - args: {{"action": "<read/write>", "path": "<file path>", "content": "<string to write>"}} 
            3. 'python_repl' - args: {{"code": "<valid python code>"}}
            4. 'finish_task' - args: {{"summary": "<The comprehensive final answer, data, or requested information to show the user>"}}

            AVAILABLE CONTEXT TOOLS:
            {tool_descriptions}

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

            CRITICAL INSTRUCTIONS FOR VS CODE CONTEXT:
            - You are running inside VS Code. You DO NOT know what file the user is looking at by default.
            - You must call `get_active_file_content` FIRST to discover the absolute file path if the user refers to "this file" or "my code".
            - IF AND ONLY IF the user explicitly asks you to fix, edit, or refactor code, your goal is to physically apply the change using the `file_system` write action.
            - IF the user ONLY asks a question (e.g., "what is the active file?", "explain this code"), you must ignore all bugs and ONLY answer the question using the `finish_task` tool.
            - WHEN WRITING FILES: The "content" string MUST contain the completely updated, fully functioning, and syntactically correct code for the ENTIRE file. 
            """

            # 1. Determine if a critique is required
            needs_reflection = False
            
            # Condition A: Mandatory Checkpoint (Every 3 iterations)
            if self.state.iterations > 0 and self.state.iterations % 3 == 0:
                needs_reflection = True
                
            # Condition B: Mid-Stream Correction (Check if the last tool failed)
            elif self.state.history and self.state.history[-1].role == Role.TOOL:
                last_obs = self.state.history[-1].content.lower()
                if "error" in last_obs or ("exit code" in last_obs and "exit code 0" not in last_obs):
                    needs_reflection = True

            # 2. Push the UI state BEFORE the LLM starts generating
            if self.uds_server:
                status_type = "reflecting" if needs_reflection else "thinking"
                status_msg = "Critique Required: Evaluating recent actions..." if needs_reflection else "🧠 Analyzing context and planning next step..."
                
                await self.uds_server.send_notification(
                    "agent_status", 
                    {"status": status_type, "message": status_msg}
                )

            # 3. Inject the mandatory critique directive into the system prompt
            if needs_reflection:
                system_prompt += "\n\nCRITIQUE REQUIRED: Review your last observations. Did your last action succeed? State your revised approach before calling the next tool."

            # 4. Build context and execute reasoning
            context = self.memory.build_safe_context(self.state, system_prompt)
            llm_response = await self.reason(context)

            # 🎯 GHOST LOOP PREVENTION: Check if a new task hijacked the state while we waited
            if self.state.run_id != my_run_id:
                log("👻 [Agent] Aborting orphaned ghost loop (new task started).")
                raise asyncio.CancelledError("Ghost loop aborted.")

            # 🎯 Emergency abort check after heavy LLM processing
            if self.state.is_canceled:
                log("🛑 [Agent] Task was manually cancelled by the user.")
                break

            self.state.history.append(Message(role=Role.ASSISTANT, content=json.dumps(llm_response)))

            tool_name = llm_response.get("tool", "unknown")
            tool_args = llm_response.get("tool_args", {})

            # If the LLM got lazy and returned a string instead of a dict
            if isinstance(tool_args, str):
                log(f"[dim]⚠️ Auto-correcting malformed tool_args string into a dictionary...[/dim]")
                if tool_name == "terminal_proxy":
                    tool_args = {"command": tool_args}
                elif tool_name == "math_operation":
                    tool_args = {"expression": tool_args}
                else:
                    tool_args = {} # Fallback

            reasoning = llm_response.get("reasoning", "No reasoning provided.")

            log(f"[bold magenta]🧠 [Reasoning][/bold magenta] {reasoning}")
            log(f"[bold yellow]🔧 [Dispatching][/bold yellow] {tool_name} with args: {tool_args}")

            if tool_name == "finish_task":
                self.state.is_complete = True
                summary = tool_args.get("summary", "Task completed successfully.")
                log(f"[bold green]✅ [Task Complete][/bold green] {summary}")

                # Actually save the summary to the state so the server can return it
                self.state.summary = summary 
                self.state.history.append(Message(role=Role.TOOL, content=summary, name=tool_name))
                break

            # 4. Route the tool call to the correct handler
            if tool_name in self.tool_registry.tools:
                # Execute new async tools (search_codebase, get_active_file_content)
                observation = await self.tool_registry.execute_tool_async(tool_name, tool_args)
            else:
                # Execute original synchronous tools (terminal_proxy, file_system)
                observation = await self.dispatcher.execute_async(tool_name, tool_args, auto_approve=auto_approve)                

            log(f"[bold blue]👀 [Observation][/bold blue]\n{observation}")

            # Hard-code the Circuit Breaker to sever the loop
            obs_str = str(observation).lower()
            if any(keyword in obs_str for keyword in ["blocked", "forbidden", "denied", "outside authorized workspace"]):
                log("🛑 [System] Sandbox violation detected. Forcing agent termination.")
                self.state.is_complete = True
                self.state.summary = f"Task aborted by system sandbox constraints:\n{observation}"
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

# --- Agent Core ---

class UniversalLLMProvider:
    """Connects the Python agent loop using the official OpenAI SDK."""
    
    def __init__(self, endpoint_url: str, model: str, api_key: Optional[str] = None):
        self.endpoint_url = endpoint_url
        self.model = model
        
        import httpx
        from openai import OpenAI
        import os

        # 🎯 FIX: Strip explicit routes universally so the SDK never double-appends them
        base_url = endpoint_url.split("/chat/completions")[0].split("/responses")[0]

        proxy_url = os.getenv("HTTPS_PROXY") or os.getenv("HTTP_PROXY")
        http_client = httpx.Client(proxy=proxy_url) if proxy_url else None
        
        # Local Ollama Routing
        if "127.0.0.1" in endpoint_url or "localhost" in endpoint_url or "11434" in endpoint_url:
            self.client = OpenAI(base_url=base_url, api_key="ollama", http_client=http_client)
            
        # Azure Foundry & Standard OpenAI Routing
        else:
            # Use Entra ID if Azure and no explicit key is provided (or if user typed 'entra')
            if "azure.com" in endpoint_url and (not api_key or api_key.lower() in ["none", "", "entra"]):
                from azure.identity import DefaultAzureCredential, get_bearer_token_provider
                
                # Fetch the Microsoft Entra token provider
                token_provider = get_bearer_token_provider(
                    DefaultAzureCredential(), "https://ai.azure.com/.default"
                )
                
                import logging
                logger = logging.getLogger(__name__)
                logger.info("🔐 Azure Entra ID authentication enabled via OpenAI SDK.")
                
                # The OpenAI SDK natively accepts the token callable instead of a string!
                self.client = OpenAI(base_url=base_url, api_key=token_provider, http_client=http_client)
            else:
                # Standard static API Key (OpenAI or Azure)
                self.client = OpenAI(base_url=base_url, api_key=api_key or "sk-dummy", http_client=http_client)

    def generate(self, context: list, require_json: bool = True) -> Optional[str]:
        import logging
        logger = logging.getLogger(__name__)
        
        try:
            # 🎯 Dynamic Routing: Use the new v1 Responses API if the user configured it
            if "azure.com" in self.endpoint_url and "/responses" in self.endpoint_url:
                # Note: The Responses API accepts 'input' instead of 'messages'
                response = self.client.responses.create(
                    model=self.model,
                    input=context
                )
                # Parse the response based on Azure's v1 Responses API structure
                try:
                    return response.output[0].content[0].text
                except (KeyError, IndexError, AttributeError):
                    return str(getattr(response, 'output', response))
                    
            # 🎯 Default: Standard Chat Completions (OpenAI & Legacy Azure)
            else:
                kwargs = {
                    "model": self.model,
                    "messages": context,
                }
                if require_json:
                    kwargs["response_format"] = { "type": "json_object" }
                    
                response = self.client.chat.completions.create(**kwargs)
                return response.choices[0].message.content
                
        except Exception as e:
            logger.error(f"❌ [SDK Connection Error] Failed to generate: {e}")
            raise RuntimeError(f"LLM Provider unreachable: {e}")

# --- Mock Implementation for Testing ---

class MockLLMProvider:
    """Mocks the LLM generating structured JSON responses for local testing."""
    def __init__(self, mock_responses: List[str]):
        self.mock_responses = mock_responses
        self.index = 0

    def generate(self, context: List[Dict[str, str]]) -> str:
        if self.index < len(self.mock_responses):
            response = self.mock_responses[self.index]
            self.index += 1
            return response
        return '{"reasoning": "No more instructions.", "tool": "finish_task", "tool_args": {}}'

# if __name__ == "__main__":
#     # Simulate the LLM deciding what to do over multiple turns
#     mocked_responses = [
#         '{"reasoning": "I need to check the current directory contents to find the project root.", "tool": "terminal_proxy", "tool_args": {"command": "ls -la"}}',
#         '{"reasoning": "I see the config file. I will read its contents via the file system.", "tool": "file_system", "tool_args": {"action": "read", "path": "./config.json"}}',
#         '{"reasoning": "I have the necessary information from the config file.", "tool": "finish_task", "tool_args": {}}'
#     ]
    
#     llm = MockLLMProvider(mocked_responses)
#     agent = Agent(llm_provider=llm)
    
#     final_state = agent.run("Locate the project root and read the configuration file.")

if __name__ == "__main__":
    llm = OllamaProxyProvider()
    
    # We pass an empty config for the test run
    agent = Agent(llm_provider=llm, config={})
    
    # Use asyncio.run() to execute the new async loop
    final_state = asyncio.run(agent.run("What is the name of the function defined in my current file?"))
