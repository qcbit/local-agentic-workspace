import json
from enum import Enum
from typing import List, Dict, Any, Optional
from dataclasses import dataclass, field
import urllib.request
import urllib.error
import os
import subprocess

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

# --- Tool Dispatcher ---

class ToolDispatcher:
    """Handles structured JSON tool requests for the environment."""
    
    def execute(self, tool_name: str, arguments: Dict[str, Any]) -> str:
        print(f"🔧 [Tool Call] Dispatching '{tool_name}' with args: {arguments}")
        
        try:
            if tool_name == "file_system":
                return self._handle_file_system(arguments)
            elif tool_name == "terminal_proxy":
                return self._handle_terminal_proxy(arguments)
            elif tool_name == "finish_task":
                return "Task marked as complete by the agent."
            else:
                return f"Error: Tool '{tool_name}' not recognized."
        except Exception as e:
            return f"Error executing {tool_name}: {str(e)}"

    def _handle_file_system(self, args: Dict[str, Any]) -> str:
        action = args.get("action")
        path = args.get("path", ".")
        
        if action == "read":
            if os.path.isdir(path):
                # Return actual directory listing so the LLM has data to work with
                files = os.listdir(path)
                return f"Directory listing for '{path}': {json.dumps(files)}"
            elif os.path.isfile(path):
                with open(path, "r", encoding="utf-8") as f:
                    content = f.read()
                return f"File content of '{path}':\n{content}"
            else:
                return f"Error: Path '{path}' does not exist."
        elif action == "write":
            content = args.get("content", "")
            with open(path, "w", encoding="utf-8") as f:
                f.write(content)
            return f"Successfully wrote to file '{path}'."
        
        return f"Error: Unsupported file system action '{action}'."

    def _handle_terminal_proxy(self, args: Dict[str, Any]) -> str:
        command = args.get("command")
        if not command:
            return "Error: No command provided."
            
        # Execute real terminal command safely
        result = subprocess.run(
            command, 
            shell=True, 
            capture_output=True, 
            text=True, 
            cwd=os.getcwd()
        )
        
        output = result.stdout if result.returncode == 0 else result.stderr
        return f"Command exit code {result.returncode}.\nOutput:\n{output}"

# --- Agent Core ---

class Agent:
    def __init__(self, llm_provider):
        self.dispatcher = ToolDispatcher()
        self.llm_provider = llm_provider

    def context_assembly(self, state: AgentState) -> List[Dict[str, str]]:
        """Assembles the user goal and historical state into the LLM context window."""
        system_prompt = (
            "You are an autonomous agent. You must respond ONLY with valid JSON. "
            "Do not include any conversational text or markdown formatting. "
            "You have access to the following tools:\n"
            "1. 'terminal_proxy' - args: {\"command\": \"<bash command>\"}\n"
            "2. 'file_system' - args: {\"action\": \"<read/write>\", \"path\": \"<file path>\"}\n"
            "3. 'finish_task' - args: {\"summary\": \"<summary of results>\"}\n\n"
            "Your output must be a single JSON object with EXACTLY these keys: "
            "\"reasoning\" (string), \"tool\" (string), and \"tool_args\" (dictionary). "
            "When you have achieved the user's goal based on the observations, you MUST call 'finish_task'."
        )
        
        context = [
            {"role": Role.SYSTEM.value, "content": system_prompt},
            {"role": Role.USER.value, "content": state.user_goal}
        ]
        
        for msg in state.history:
            if msg.role == Role.TOOL:
                # Map tool observations to the USER role to maintain strict conversation flow
                context.append({
                    "role": Role.USER.value, 
                    "content": f"Tool '{msg.name}' Observation:\n{msg.content}\n\nWhat is your next action?"
                })
            else:
                context.append({"role": msg.role.value, "content": msg.content})
                
        return context

    def reason(self, context: List[Dict[str, str]]) -> Dict[str, Any]:
        """Invokes the LLM and parses the structured JSON response."""
        # Delegate to the LLM (Ollama, OpenAI, Claude, etc.)
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

    def run(self, user_goal: str) -> AgentState:
        """The Main Agent Loop: Context -> Reason -> Tool -> Observation -> State Update."""
        state = AgentState(user_goal=user_goal)
        print(f"🚀 --- Starting Agent Loop ---\nGoal: {user_goal}")

        while not state.is_complete and state.iterations < state.max_iterations:
            print(f"\n🔄 --- Iteration {state.iterations + 1} ---")
            
            # 1. Context Assembly
            context = self.context_assembly(state)
            
            # 2. Reasoning
            llm_response = self.reason(context)
            
            # 3. State Update (Assistant Thought)
            state.history.append(Message(role=Role.ASSISTANT, content=json.dumps(llm_response)))

            # Provide a fallback string if the key is missing
            tool_name = llm_response.get("tool", "unknown")
            tool_args = llm_response.get("tool_args", {})

            # Handle task completion
            if tool_name == "finish_task":
                state.is_complete = True
                # Extract the LLM's custom summary, fallback to a default string if missing
                summary = tool_args.get("summary", "Task completed successfully.")
                print(f"✅ [Observation] {summary}")
                
                # Append the final tool observation to the history BEFORE breaking
                state.history.append(Message(role=Role.TOOL, content=summary, name=tool_name))
                break

            # 4. Tool Call & Observation
            observation = self.dispatcher.execute(tool_name, tool_args)
            print(f"👀 [Observation] {observation}")
            
            # --- Add Circuit Breaker Here ---
            if tool_name == "error" and "Connection Error" in str(llm_response.get("reasoning", "")):
                print("🛑 [Circuit Breaker] LLM provider is unreachable. Aborting loop.")
                break

            # 5. State Update (Tool Result)
            state.history.append(Message(role=Role.TOOL, content=observation, name=tool_name))
            state.iterations += 1

        if not state.is_complete:
            print("\n⚠️ --- Agent Loop Terminated (Max Iterations Reached) ---")
        else:
            print("\n🏁 --- Agent Loop Completed ---")
            
        return state

class OllamaProxyProvider:
    """Connects the Python agent loop to the local Warp proxy on port 11435."""
    
    def __init__(self, endpoint_url: str = "http://127.0.0.1:11435/v1/chat/completions", model: str = "llama3:8b"):
        self.endpoint_url = endpoint_url
        self.model = model

    def generate(self, context: List[Dict[str, str]]) -> str:
        payload = {
            "model": self.model,
            "messages": context,
            "format": "json"
        }
        
        req = urllib.request.Request(
            self.endpoint_url, 
            data=json.dumps(payload).encode('utf-8'),
            headers={'Content-Type': 'application/json'},
            method='POST'
        )

        try:
            with urllib.request.urlopen(req) as response:
                result = json.loads(response.read().decode('utf-8'))
                return result['choices'][0]['message']['content']
                
        except urllib.error.HTTPError as e:
            # Catches API errors (e.g., 404 Not Found, 500 Server Error)
            error_msg = e.read().decode('utf-8')
            print(f"❌ [HTTP Error {e.code}] Proxy response: {error_msg}")
            return json.dumps({
                "reasoning": f"HTTP Error from proxy: {error_msg}",
                "tool": "error",
                "tool_args": {}
            })
            
        except urllib.error.URLError as e:
            # Catches connection failures (e.g., proxy not running)
            print(f"❌ [Connection Error] Failed to reach proxy: {e.reason}")
            return json.dumps({
                "reasoning": f"Connection Error: {e.reason}",
                "tool": "error",
                "tool_args": {}
            })

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
    # Initialize the live proxy provider
    # It defaults to http://127.0.0.1:11435/v1/chat/completions and llama3:8b
    llm = OllamaProxyProvider()
    
    # Pass the live provider to the agent
    agent = Agent(llm_provider=llm)
    
    # Run the loop with a test goal
    final_state = agent.run("List the files in my current directory.")
