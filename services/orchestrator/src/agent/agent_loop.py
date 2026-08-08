import asyncio
from dataclasses import dataclass, field
from enum import Enum
import json
import logging
import os
from pathlib import Path
from rag.vector_store import LocalVectorStore
from services.orchestrator.src.memory.context_manager import SlidingContextManager
import shlex
import subprocess
import time
from typing import List, Dict, Any, Optional
import urllib.request
import urllib.error

logger = logging.getLogger(__name__)

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
    iterations: int = 0
    max_iterations: int = 10
    summary: str = ""  # to track the running summary 

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
    
    def __init__(self, uds_server=None, workspace_root: str = None):
        self.uds_server = uds_server
        # Default to current directory if not provided
        self.workspace_root = workspace_root or os.getcwd() 
        
        # Strict deny-list for highly destructive or interactive commands
        self.shell_deny_list = [
            "rm", "sudo", "mkfs", "fdisk", "dd", "chown", "chmod", 
            "shutdown", "reboot", "ufw", "iptables", "firewall-cmd", 
            "nano", "vim", "top", "history"
        ]

    async def execute_async(self, tool_name: str, arguments: Dict[str, Any]) -> str:
        print(f"🔧 [Tool Call] Dispatching '{tool_name}' with args: {arguments}")
        
        try:
            if tool_name == "file_system":
                return await self._handle_file_system_async(arguments)
            elif tool_name == "terminal_proxy":
                return await self._handle_terminal_proxy_async(arguments)
            elif tool_name == "finish_task":
                return "Task marked as complete by the agent."
            else:
                return f"Error: Tool '{tool_name}' not recognized."
        except Exception as e:
            return f"Error executing {tool_name}: {str(e)}"

    async def _handle_file_system_async(self, args: Dict[str, Any]) -> str:
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
                return f"File content of '{path}':\n{content}"
            else:
                return f"Error: Path '{path}' does not exist."
                
        # TIER 2: File writes (Requires VS Code Staged Diff Approval)
        elif action == "write":
            content = args.get("content", "")
            
            if not self.uds_server:
                return "Error: Cannot request write permission. IPC Server not attached."
                
            print(f"⏸️  [Proxy] Requesting write permission for '{path}'...")
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

    async def _handle_terminal_proxy_async(self, args: Dict[str, Any]) -> str:
        command = args.get("command")
        if not command:
            return "Error: No command provided."
            
        # Sandbox Check
        is_safe, error_msg = validate_terminal_command(self.workspace_root, command)
        if not is_safe:
            logger.warning(f"🔒 [Sandbox Blocked] Command: '{command}'")
            return error_msg  # Fed back to LLM as observation

        # Pre-execution Deny-List Check
        command_lower = command.lower()
        if any(forbidden in command_lower.split() for forbidden in self.shell_deny_list):
            # Return a strict security violation observation payload
             return f"SECURITY VIOLATION: Command execution blocked. '{command}' contains forbidden keywords."
            
        # TIER 3: Shell commands (Requires Explicit Modal Confirmation)
        if not self.uds_server:
            return "Error: Cannot request shell permission. IPC Server not attached."
            
        print(f"⏸️  [Proxy] Requesting shell execution permission for '{command}'...")
        response = await self.uds_server.request_client_context("request_shell_permission", {
            "command": command
        })
        
        # -> NEW: Explicitly check if the UI timed out
        if "content" in response and "timed out" in response["content"]:
            return response["content"]
        
        if response.get("status") == "approved":
            result = subprocess.run(
                command, 
                shell=True, 
                capture_output=True, 
                text=True, 
                cwd=os.getcwd()
            )
            output = result.stdout if result.returncode == 0 else result.stderr
            return f"Command exit code {result.returncode}.\nOutput:\n{output}"
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
            # ... keep your existing math tool ...
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

    async def execute_tool_async(self, tool_name: str, arguments: dict) -> Optional[str]:
        try:
            if tool_name == "calculate_math":
                # ... existing logic ...
                pass
                
            elif tool_name == "search_codebase":
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

            elif tool_name in ["get_active_file_content", "get_selected_text"]:
                if not self.uds_server:
                    return "Error: IPC Server not attached to ToolRegistry."
                
                # This is where the magic happens! We pause the agent and 
                # ask the UDS server to request data FROM Node.js
                response = await self.uds_server.request_client_context(tool_name)
                return response.get("content", "Error: No content received from VS Code.")

            else:
                return f"Tool {tool_name} not found."
                
        except Exception as e:
            return f"Error executing {tool_name}: {str(e)}"

# --- Agent Core ---

class Agent:
    """The central state machine managing the ReAct loop."""

    def __init__(self, llm_provider, config: Dict[str, Any], uds_server=None, workspace_root: str = None):
        self.llm_provider = llm_provider
        self.uds_server = uds_server
        self.workspace_root = workspace_root or os.getcwd()
        
        # Pass the workspace_root down to the dispatcher
        self.dispatcher = ToolDispatcher(uds_server=uds_server, workspace_root=self.workspace_root)        
        self.tool_registry = ToolRegistry(uds_server=uds_server)
        self.max_iterations = 10
        
        llm_config = config.get("llm", {})
        memory_config = config.get("memory", {})
        
        self.memory = SlidingContextManager(
            memory_config=memory_config,
            model_name=llm_config.get("model_name", "llama3:8b"),
            llm_provider=self.llm_provider,
        )

    def reason(self, context: List[Dict[str, str]]) -> Dict[str, Any]:
        """Invokes the LLM and parses the structured JSON response."""
        raw_response = self.llm_provider.generate(context)
        print(f"🧠 [Reasoning] LLM Output:\n{raw_response}")
        
        try:
            return json.loads(raw_response)
        except json.JSONDecodeError:
            return {
                "reasoning": "Failed to parse JSON from LLM output.", 
                "tool": "error", 
                "tool_args": {"raw": raw_response}
            }

    # 2. Make the run method ASYNC so we can await the IPC socket tools
    async def run(self, user_goal: str) -> AgentState:
        """The Main Agent Loop: Context -> Reason -> Tool -> Observation -> State Update."""
        state = AgentState(user_goal=user_goal)
        print(f"🚀 --- Starting Agent Loop ---\nGoal: {user_goal}")

        while not state.is_complete and state.iterations < state.max_iterations:
            print(f"\n🔄 --- Iteration {state.iterations + 1} ---")
            
            # Fetch dynamically from our properly named tool_registry
            tool_descriptions = "\n".join([f"- {name}: {info['description']}" for name, info in self.tool_registry.tools.items()])

            # 3. Use an f-string so {tool_descriptions} actually gets injected!
            # Note the double brackets {{ }} to escape JSON schema syntax inside an f-string.
            system_prompt = f"""You are an autonomous agent. You must respond ONLY with valid JSON. 

            Do not include any conversational text or markdown formatting. 
            You have access to the following tools:
            1. 'terminal_proxy' - args: {{"command": "<bash command>"}}
            2. 'file_system' - args: {{"action": "<read/write>", "path": "<file path>", "content": "<string to write>"}}
            3. 'finish_task' - args: {{"summary": "<The comprehensive final answer, data, or requested information to show the user>"}}

            AVAILABLE CONTEXT TOOLS:
            {tool_descriptions}

            Your output must be a single JSON object with EXACTLY these keys: "reasoning" (string), "tool" (string), and "tool_args" (dictionary). 
            
            CRITICAL RULES:
            - If a tool requires no arguments, you MUST pass an empty dictionary: {{"tool_args": {{}}}}
            - Never use Python-style 'None'. Use strict JSON only.
            - NEVER invent, hallucinate, or call tools that are not explicitly listed above. 
            - For basic factual questions, general knowledge, or simple arithmetic (e.g., "what is 1+1?"), DO NOT call any tools. Use your internal knowledge to solve it and immediately call 'finish_task' with the answer.
            - Once you have achieved the user's goal based on the observations, you MUST IMMEDIATELY call 'finish_task'. Do not explore further.
            - The 'summary' argument in 'finish_task' is the ONLY information the user will see. You MUST include the actual results, lists, code, or data requested by the user in this summary. Never just say "task complete".

            CRITICAL INSTRUCTIONS FOR VS CODE CONTEXT:
            - You are running inside VS Code. You DO NOT know what file the user is looking at by default.
            - NEVER guess or hallucinate file paths.
            - If the user asks to modify "this file" or "my code", you MUST call `get_active_file_content` FIRST to discover the absolute file path.
            - When asked to fix, refactor, or resolve an issue, the goal is NOT just to find the answer. The goal is to physically apply the code change.
            - To apply a fix, you must ALWAYS call the `file_system` tool with `"action": "write"`. Never use `finish_task` to simply describe the solution.
            - You are STRICTLY FORBIDDEN from using the `file_system` tool to write to a file until you have called `get_active_file_content` to get its exact path.
            - WHEN WRITING FILES: The "content" string MUST contain the completely updated, fully functioning, and syntactically correct code for the ENTIRE file.
            """
            
            context = self.memory.build_safe_context(state, system_prompt)
            llm_response = self.reason(context)

            if self.uds_server:
                reasoning_text = llm_response.get("reasoning", "...")
                await self.uds_server.send_notification(
                    "agent_status", 
                    {"message": f"🧠 Thinking: {reasoning_text}"}
                )
            
            state.history.append(Message(role=Role.ASSISTANT, content=json.dumps(llm_response)))

            tool_name = llm_response.get("tool", "unknown")
            tool_args = llm_response.get("tool_args", {})

            if tool_name == "finish_task":
                state.is_complete = True
                summary = tool_args.get("summary", "Task completed successfully.")
                print(f"✅ [Observation] {summary}")
                
                # Actually save the summary to the state so the server can return it
                state.summary = summary 
                
                state.history.append(Message(role=Role.TOOL, content=summary, name=tool_name))
                break

            # 4. Route the tool call to the correct handler
            if tool_name in self.tool_registry.tools:
                # Execute new async tools (search_codebase, get_active_file_content)
                observation = await self.tool_registry.execute_tool_async(tool_name, tool_args)
            else:
                # Execute original synchronous tools (terminal_proxy, file_system)
                observation = await self.dispatcher.execute_async(tool_name, tool_args)                

            print(f"👀 [Observation] {observation}")
            
            if tool_name == "error" and "Connection Error" in str(llm_response.get("reasoning", "")):
                print("🛑 [Circuit Breaker] LLM provider is unreachable. Aborting loop.")
                break

            state.history.append(Message(role=Role.TOOL, content=str(observation), name=tool_name))
            state.iterations += 1

        if not state.is_complete:
            print("\n⚠️ --- Agent Loop Terminated (Max Iterations Reached) ---")
        else:
            print("\n🏁 --- Agent Loop Completed ---")
            
        return state

# --- Agent Core ---

class OllamaProxyProvider:
    """Connects the Python agent loop to the local Warp proxy on port 11435."""
    
    def __init__(self, endpoint_url: str = "http://127.0.0.1:11435/v1/chat/completions", model: str = "llama3:8b"):
        self.endpoint_url = endpoint_url
        self.model = model

    def generate(self, context: List[Dict[str, str]], require_json: bool = True) -> Optional[str]:
        payload = {
            "model": self.model,
            "messages": context
        }
        if require_json:
            payload["format"] = "json"
            
        data = json.dumps(payload).encode('utf-8')
        
        # Retry loop for slow startup or transient drops
        max_retries = 3
        backoff_seconds = 1.5
        
        for attempt in range(max_retries):
            try:
                req = urllib.request.Request(
                    self.endpoint_url, 
                    data=data,
                    headers={'Content-Type': 'application/json'},
                    method='POST'
                )
                with urllib.request.urlopen(req, timeout=30) as response:
                    res_body = response.read().decode('utf-8')
                    res_json = json.loads(res_body)
                    return res_json["choices"][0]["message"]["content"]
                    
            except urllib.error.URLError as e:
                if attempt == max_retries - 1:
                    logger.error(f"❌ [Connection Error] Failed to reach proxy after {max_retries} attempts: {e.reason}")
                    raise RuntimeError(f"LLM Provider unreachable: {e.reason}")
                
                logger.warning(f"⚠️ Proxy not ready (attempt {attempt + 1}/{max_retries}), retrying in {backoff_seconds}s...")
                time.sleep(backoff_seconds)
                backoff_seconds *= 2  # Exponential backoff

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
