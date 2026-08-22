import asyncio
import collections.abc
import hashlib
import json
import logging
import os
from pathlib import Path
import socket
import stat
import sys
from typing import Any, Dict, Optional
import uuid

# 1. Grab the current fallback path
workspace_root = os.getcwd()
# Define a cross-platform default for the global config
global_config_path = os.path.expanduser("~/.agentic_config.json")

# 2. Extract the true path passed via VS Code CLI arguments
if "--workspace" in sys.argv:
    idx = sys.argv.index("--workspace")
    if idx + 1 < len(sys.argv):
        workspace_root = sys.argv[idx + 1]

# 3. Extract the user-defined global config path
if "--global-config" in sys.argv:
    idx = sys.argv.index("--global-config")
    if idx + 1 < len(sys.argv):
        global_config_path = sys.argv[idx + 1]

# 4. Defeat PyInstaller by physically moving the Python process to the true workspace
if os.path.exists(workspace_root):
    os.chdir(workspace_root)

# Now os.getcwd(), relative paths, and subprocess.run will all be perfectly aligned!
workspace_root = os.getcwd()

script_dir = os.path.dirname(os.path.abspath(__file__))
src_dir = os.path.abspath(os.path.join(script_dir, ".."))  # .../services/orchestrator/src

for path in (src_dir, workspace_root):
    if path not in sys.path:
        sys.path.insert(0, path)

from rag.vector_store import LocalVectorStore
from services.orchestrator.src.agent.agent_loop import Agent, OllamaProxyProvider

# 🎯 Write logs to both the VS Code output panel AND a persistent file
log_file = os.path.expanduser("~/.agentic_backend.log")

logging.basicConfig(
    level=logging.INFO, 
    format="%(asctime)s - %(levelname)s - %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(log_file, encoding='utf-8')
    ]
)
logger = logging.getLogger(__name__)
config_path = os.path.join(workspace_root, '.agentic_config.json')

def load_config():
    """Load config from the workspace, fallback to global, or create a default."""
    local_config_path = os.path.join(workspace_root, '.agentic_config.json')
    
    # 1. Try Workspace Config
    if os.path.exists(local_config_path):
        try:
            with open(local_config_path, 'r') as f:
                return json.load(f)
        except Exception as e:
            logger.warning(f"Error reading local config: {e}")
            
    # 2. Try User-Defined Global Config
    if os.path.exists(global_config_path):
        try:
            with open(global_config_path, 'r') as f:
                return json.load(f)
        except Exception as e:
            logger.warning(f"Error reading global config: {e}")
            
    # 3. Default configuration if neither exists
    default_config = {
        "active_profile": "home",
        "profiles": {
            "home": {
                "llm": {
                    "model_name": "llama3",
                    "endpoint_url": "http://127.0.0.1:11434/v1/chat/completions"
                }
            }
        }
    }
    
    save_config(default_config, force_global=True)
    return default_config

def save_config(new_config, force_global=False):
    """Save the updated config. Prefers local workspace unless forced global."""
    local_config_path = os.path.join(workspace_root, '.agentic_config.json')
    target_path = global_config_path if force_global else local_config_path
    
    try:
        with open(target_path, 'w') as f:
            json.dump(new_config, f, indent=4)
    except Exception as e:
        logger.error(f"Failed to save config to {target_path}: {e}")

def deep_update(d, u):
    """Recursively merges dictionary 'u' into dictionary 'd'."""
    for k, v in u.items():
        if isinstance(v, collections.abc.Mapping):
            d[k] = deep_update(d.get(k, {}), v)
        else:
            d[k] = v
    return d

def get_resource_path(relative_path: str) -> str:
    """Get absolute path to resource, works for dev and for PyInstaller one-file binaries."""
    # getattr safely checks for _MEIPASS without angering the linter
    base_path = getattr(sys, '_MEIPASS', os.path.abspath("."))

    return os.path.join(base_path, relative_path)

class JsonRpcUdsServer:
    """JSON-RPC 2.0 Server over Unix Domain Sockets."""
    
    def __init__(self, socket_path: Optional[str] = None):
        # Bind universally to /tmp/agent.sock so Node.js can consistently find it
        if socket_path is None:
            self.socket_path = "/tmp/agent.sock"
        else:
            self.socket_path = socket_path
            
        self.config = load_config()

        self.running = False

        # Registry for spontaneous events from VS Code
        self.notification_handlers = {}
        
        # Initialize the vector store on startup
        logger.info("Initializing LanceDB Vector Store...")
        self.vector_store = LocalVectorStore()

        # A dictionary to track pending reverse-requests (Requests sent to Node.js)
        self.pending_requests = {}
        # We need a reference to the active writer to push the message
        self.active_writer = None
        # 🎯 Hold the persistent agent in memory
        self.persistent_agent = None

    def register_notification_handler(self, method_name: str, callback_coroutine):
        """Registers an async callback for a specific incoming notification."""
        self.notification_handlers[method_name] = callback_coroutine

    
    async def handle_client(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        """Reads incoming streams, dispatches JSON-RPC requests, and writes responses."""

        try:
            while True:
                data = await reader.readline()
                if not data:
                    break

                print(f"🕵️ [RAW SOCKET] {data.decode('utf-8').strip()}")
                
                payload = data.decode('utf-8').strip()
                if not payload:
                    continue
                    
                req = json.loads(payload)

                # --- Smart Client Routing ---
                # Only designate the connection as the VS Code Extension Host if it sends editor-specific commands.
                # The TUI will never send these, preventing it from stealing the context connection.
                if req.get("method") in ["sync_file", "update_config", "get_config", "ping", "execute_agent_task"]:
                    self.active_writer = writer

                # Is this an unprompted notification from VS Code? (Has method, no ID)
                if "method" in req and "id" not in req:
                    method = req["method"]
                    params = req.get("params", {})
                    logger.info(f"📥 [IPC] Received notification: {method}")
                    
                    if method in self.notification_handlers:
                        # Spin up the handler in the background
                        asyncio.create_task(self.notification_handlers[method](params))
                    else:
                        logger.warning(f"⚠️ [IPC] No handler registered for notification: {method}")
                    continue
                
                # Check if this is a RESPONSE to a reverse-request we sent
                if "result" in req and req.get("id") in self.pending_requests:
                    future = self.pending_requests.pop(req["id"])
                    if not future.done():
                        future.set_result(req["result"])
                    continue
                
                # Handle error responses from Node.js
                if "error" in req and req.get("id") in self.pending_requests:
                    future = self.pending_requests.pop(req["id"])
                    if not future.done():
                        # Pass the error string back to the agent so it knows what failed
                        future.set_result({"content": f"VS Code Context Error: {req['error']}"})
                    continue
                    
                # CONCURRENCY FIX: Process standard requests in a background task!
                # This frees up the loop to immediately read the next line.
                asyncio.create_task(self._dispatch_request(payload, writer))
                
        except Exception as e:
            logger.error(f"Client connection error: {e}")
        finally:
            # --- Safe Disconnect ---
            # Only clear the active writer if the disconnecting client IS the active writer
            if self.active_writer == writer:
                self.active_writer = None
            writer.close()
            await writer.wait_closed()
            # 🎯: Immediately cancel all pending IPC requests if VS Code reloads/disconnects
            for req_id, future in self.pending_requests.items():
                if not future.done():
                    future.set_exception(ConnectionError("VS Code client disconnected abruptly (window reloaded)."))
            self.pending_requests.clear()

    async def _dispatch_request(self, payload: str, writer: asyncio.StreamWriter):
        """Processes the request in the background and writes the response."""
        try:
            response = await self.process_request(payload)
            writer.write((json.dumps(response) + '\n').encode('utf-8'))
            await writer.drain()
        except Exception as e:
            logger.error(f"Error dispatching request: {e}")

    async def request_client_context(self, method: str, params: dict = None) -> dict:        
        """Sends a JSON-RPC request to VS Code and awaits the response."""
        if not self.active_writer:
            raise ConnectionError("No active VS Code client connected.")
            
        req_id = str(uuid.uuid4())
        payload = {
            "jsonrpc": "2.0",
            "id": req_id,
            "method": method,
            "params": params or {}
        }
        
        # Create a Future object to suspend execution until Node.js replies
        loop = asyncio.get_running_loop()
        future = loop.create_future()
        self.pending_requests[req_id] = future
        
        # Fire it over the socket
        self.active_writer.write((json.dumps(payload) + '\n').encode('utf-8'))
        await self.active_writer.drain()
        
        # Wait here until the response handler (above) sets the result
        try:
            return await asyncio.wait_for(future, timeout=300.0)
        except asyncio.TimeoutError:
            self.pending_requests.pop(req_id, None)
            return {"content": "Error: VS Code client timed out."}

    def handle_update_config(self, payload: dict):
        # 1. Use the global config_path instead of the hardcoded project_root path
        logger.info(f"⚙️ Target config path: {config_path}")

        # 2. Load the existing config
        try:
            with open(config_path, "r", encoding="utf-8") as f:
                config_data = json.load(f)
        except FileNotFoundError:
            logger.error(f"❌ Failed to locate config file at: {config_path}")
            # If it's missing we'll initialize an empty config to merge into
            config_data = {}
        except Exception as e:
            logger.error(f"❌ Error reading config.json: {e}")
            return {"error": str(e)}

        # 3. Extract incoming payload data
        active_profile = payload.get("active_profile")
        profile_settings = payload.get("profile_settings", {})

        if active_profile:
            config_data["active_profile"] = active_profile
            
            # 4. Safely DEEP MERGE new settings into the active profile
            if "profiles" in config_data and active_profile in config_data["profiles"]:
                config_data["profiles"][active_profile] = deep_update(
                    config_data["profiles"][active_profile], 
                    profile_settings
                )
            else:
                if "profiles" not in config_data:
                    config_data["profiles"] = {}
                config_data["profiles"][active_profile] = profile_settings

        # 5. Write updated config back to disk
        try:
            with open(config_path, "w", encoding="utf-8") as f:
                json.dump(config_data, f, indent=4)
        except Exception as e:
            logger.error(f"❌ Failed to write config.json: {e}")
            return {"error": f"Failed to save file: {e}"}

        # 6. Update in-memory reference
        self.config = config_data

        # 🎯 Nuke the persistent agent so the memory resets and the new model loads
        self.persistent_agent = None

        logger.info(f"✅ Config successfully updated on disk and in memory for profile: '{active_profile}'")
        return {"status": "success", "message": "Configuration merged and saved."}

    async def process_request(self, payload: str) -> Dict[str, Any]:
        """Validates and routes the JSON-RPC 2.0 payload."""
        req = {}
        try:
            req = json.loads(payload)
            
            if req.get("jsonrpc") != "2.0" or "id" not in req or "method" not in req:
                return self._error_response(None, -32600, "Invalid Request")
            
            method = req["method"]
            params = req.get("params", {})
            req_id = req["id"]
            
            if method == "ping":
                return self._success_response(req_id, "pong")
                
            elif method == "execute_agent_task":
                goal = params.get("goal")
                if not goal:
                    return self._error_response(req_id, -32602, "Invalid params: 'goal' is required")

                # 1. Extract the flag from the VS Code payload (default to False)
                is_auto_approve = params.get("auto_approve", False)
                if is_auto_approve:
                    logger.info("⚡ [Agent Execution] Auto-approve is enabled. Agent will execute without user confirmation.")

                logger.info(f"🧠 [Agent Execution] Starting task: {goal}")
                
                try:
                    result_state = await self._run_agent_async(goal, auto_approve=is_auto_approve)
                    
                    # Extract final observation
                    final_observation = "Task failed or max iterations reached."
                    if result_state.is_complete and result_state.history:
                        last_msg = result_state.history[-1]
                        if last_msg.name == "finish_task":
                            final_observation = last_msg.content

                    return self._success_response(req_id, {
                        "status": "completed", 
                        "iterations": result_state.iterations,
                        "final_observation": final_observation
                    })
                    
                except Exception as e:
                    logger.error(f"Agent execution failed: {e}")
                    return self._error_response(req_id, -32000, f"Agent execution error: {str(e)}")
                    
            elif method == "update_config":
                res = self.handle_update_config(params)
                if "error" in res:
                    return self._error_response(req_id, -32000, res["error"])
                return self._success_response(req_id, res)
            elif method == "get_config":
                # Return the currently loaded configuration directly to VS Code
                return self._success_response(req_id, self.config)
            elif method == "sync_file":
                return self._success_response(req_id, self._handle_sync_file(params))
            elif method == "search_codebase":
                return self._success_response(req_id, self._handle_search_codebase(params))
            else:
                return self._error_response(req_id, -32601, f"Method '{method}' not found")
                
        except json.JSONDecodeError:
            return self._error_response(None, -32700, "Parse error")
        except Exception as e:
            logger.error(f"Internal error processing request: {e}")
            return self._error_response(req.get("id") if isinstance(req, dict) else None, -32603, str(e))

    def _handle_sync_file(self, params: Dict[str, Any]) -> Dict[str, Any]:
        """Hashes the incoming file and upserts it into LanceDB."""
        file_path = params.get("file_path")
        content = params.get("content", "")

        if not file_path:
            raise ValueError("file_path is required for sync_file")

        # Generate a hash to track file modifications
        file_hash = hashlib.md5(content.encode('utf-8')).hexdigest()

        # Run the upsert (this parses the AST and generates vectors)
        self.vector_store.upsert_file(file_path, file_hash, content)

        return {"status": "success", "indexed_path": file_path}

    async def _run_agent_async(self, goal: str, auto_approve: bool = False):
        """Asynchronously instantiates and runs the agent."""
        
        # 1. Determine which profile is active
        active_profile_name = self.config.get("active_profile", "home")
        
        # 2. Extract profiles and rigidly enforce that it MUST be a dictionary
        profiles = self.config.get("profiles", {})
        if not isinstance(profiles, dict):
            profiles = {}
            
        # 3. Safely extract the active config (fallback to the root dict if flat)
        if not isinstance(active_profile_name, str):
            active_profile_name = "home"
            
        active_config = profiles.get(active_profile_name, self.config)
        if not isinstance(active_config, dict):
            active_config = self.config
        
        # 4. Extract LLM settings (fallback to active_config if 'llm' key is missing)
        llm_config = active_config.get("llm", active_config)
        if not isinstance(llm_config, dict):
            llm_config = active_config
        
        # 5. Bulletproof extraction: check for both the new and old UI keys
        endpoint = (
            llm_config.get("endpoint_url") or 
            llm_config.get("endpoint") or 
            self.config.get("endpoint_url") or 
            self.config.get("endpoint")
        )
        
        model_name = (
            llm_config.get("model_name") or 
            llm_config.get("model") or 
            self.config.get("model_name") or 
            self.config.get("model")
        )
        
        # 🛡️ Failsafe: Auto-correct naked Ollama URLs to the chat completions path
        if endpoint and endpoint.endswith("11434"):
            endpoint = f"{endpoint}/v1/chat/completions"

        if not endpoint or not model_name:
            raise ValueError(f"Missing endpoint or model in workspace config. Current config state: {self.config}")
        
        # 🎯 ONLY instantiate the agent if it doesn't exist yet
        if self.persistent_agent is None:
            llm = OllamaProxyProvider(
                endpoint_url=endpoint,
                model=model_name
            )
            
            self.persistent_agent = Agent(
                llm_provider=llm, 
                config=active_config, 
                uds_server=self, 
                workspace_root=workspace_root
            )

        state = await self.persistent_agent.run(goal, auto_approve=auto_approve)
        return state

    def _success_response(self, req_id: Any, result: Any) -> Dict[str, Any]:
        return {"jsonrpc": "2.0", "result": result, "id": req_id}

    def _error_response(self, req_id: Any, code: int, message: str) -> Dict[str, Any]:
        return {"jsonrpc": "2.0", "error": {"code": code, "message": message}, "id": req_id}

    async def start(self):
        """Binds the UDS socket and enforces strict 0600 permissions."""
        if os.path.exists(self.socket_path):
            os.remove(self.socket_path)
            
        # Increase the StreamReader buffer limit to 10MB to handle large file syncs
        server = await asyncio.start_unix_server(
            self.handle_client, 
            path=self.socket_path, 
            limit=10 * 1024 * 1024
        )
        
        os.chmod(self.socket_path, stat.S_IRUSR | stat.S_IWUSR)
        logger.info(f"🔌 UDS JSON-RPC Server listening on {self.socket_path} (Permissions: 0600)")
        
        async with server:
            await server.serve_forever()

    def _handle_search_codebase(self, params: Dict[str, Any]) -> Dict[str, Any]:
        import time
        query = params.get("query", "")
        limit = params.get("limit", 5)

        if not query:
            raise ValueError("Query string is required for search.")

        # Time the operation to verify sub-100ms latency
        start_time = time.time()
        raw_results = self.vector_store.semantic_search(query, limit=limit)
        
        # Strip the massive float vectors out before sending over IPC
        clean_results = []
        for r in raw_results:
            clean_results.append({
                "file_path": str(r.get("file_path")),
                "content": str(r.get("content", "")),
                "score": float(r.get("_distance", 0.0))
            })
            
        elapsed_ms = (time.time() - start_time) * 1000
        logger.info(f"🔍 Vector search completed in {elapsed_ms:.2f}ms")

        return {
            "status": "success",
            "results": clean_results,
            "elapsed_ms": round(elapsed_ms, 2)
        }

    async def send_notification(self, method: str, params: dict):
        """Pushes a one-way notification to the connected client."""
        if not self.active_writer:
            return
            
        payload = {
            "jsonrpc": "2.0",
            "method": method,
            "params": params
        }
        try:
            self.active_writer.write((json.dumps(payload) + '\n').encode('utf-8'))
            await self.active_writer.drain()
        except Exception as e:
            logger.error(f"Failed to send notification: {e}")

async def handle_terminal_error(params: dict):
    """Callback triggered when VS Code detects a terminal failure."""
    command = params.get("command", "unknown")
    exit_code = params.get("exit_code", -1)
    error_output = params.get("error_output", "")
    
    logger.info(f"\n🚨 [Proactive AI] Intercepted terminal error for command: {command}")
    
    goal = (
        f"The user ran the terminal command `{command}` which failed with exit code {exit_code}.\n"
        f"Here is the error output:\n```\n{error_output}\n```\n"
        f"Analyze this error. Use codebase search tools if you need context, "
        f"and propose a fix using the terminal_proxy tool."
    )
    
    # Initialize a fresh Agent instance for this background task
    # Note: Ensure you import Agent and OllamaProxyProvider at the top of your file if not already there
    try:
        active_profile_name = server.config.get("active_profile", "home")
        active_config = server.config.get("profiles", {}).get(active_profile_name, {})
        llm = OllamaProxyProvider(
            endpoint_url=active_config.get("llm", {}).get("endpoint_url"),
            model=active_config.get("llm", {}).get("model_name")
        )
        
        agent = Agent(llm_provider=llm, config=active_config, uds_server=server)
        logger.info("🧠 [Proactive AI] Spinning up background agent to deduce a fix...")
        await agent.run(goal)
    except Exception as e:
        logger.error(f"Failed to start proactive agent: {e}")

if __name__ == "__main__":
    server = JsonRpcUdsServer()
    
    # Register the handler before starting the server
    server.register_notification_handler("terminal_error_detected", handle_terminal_error)
    
    try:
        asyncio.run(server.start())
    except KeyboardInterrupt:
        logger.info("Shutting down UDS server.")
        if os.path.exists(server.socket_path):
            os.remove(server.socket_path)
