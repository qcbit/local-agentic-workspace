import asyncio
import hashlib
import json
import logging
import os
import socket
import stat
import sys
from pathlib import Path
from typing import Any, Dict, Optional

script_dir = os.path.dirname(os.path.abspath(__file__))
src_dir = os.path.abspath(os.path.join(script_dir, ".."))                             # .../services/orchestrator/src
workspace_root = os.path.abspath(os.path.join(script_dir, "..", "..", "..", ".."))

for path in (src_dir, workspace_root):
    if path not in sys.path:
        sys.path.insert(0, path)

from rag.vector_store import LocalVectorStore
from services.orchestrator.src.agent.agent_loop import Agent, OllamaProxyProvider

# Dynamically resolve the project root to ensure imports work from any execution path
project_root = str(Path(__file__).resolve().parent.parent.parent.parent.parent)
if project_root not in sys.path:
    sys.path.append(project_root)

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)

class JsonRpcUdsServer:
    """JSON-RPC 2.0 Server over Unix Domain Sockets."""
    
    def __init__(self, socket_path: Optional[str] = None):
        # Bind universally to /tmp/agent.sock so Node.js can consistently find it
        if socket_path is None:
            self.socket_path = "/tmp/agent.sock"
        else:
            self.socket_path = socket_path
            
        self.config = self._load_config()

        self.running = False
        
        # Initialize the vector store on startup
        logger.info("Initializing LanceDB Vector Store...")
        self.vector_store = LocalVectorStore()

    def _load_config(self) -> Dict[str, Any]:
        """Loads the orchestrator configuration from disk."""
        config_path = os.path.join(project_root, "services", "orchestrator", "config.json")
        try:
            with open(config_path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            logger.error(f"Failed to load config.json: {e}. Using defaults.")
            return {}

    async def handle_client(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        """Reads incoming streams, dispatches JSON-RPC requests, and writes responses."""
        try:
            while True:
                data = await reader.readline()
                if not data:
                    break
                
                payload = data.decode('utf-8').strip()
                if not payload:
                    continue
                    
                response = await self.process_request(payload)
                
                writer.write((json.dumps(response) + '\n').encode('utf-8'))
                await writer.drain()
                
        except Exception as e:
            logger.error(f"Client connection error: {e}")
        finally:
            writer.close()
            await writer.wait_closed()

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
                
                logger.info(f"🧠 [Agent Execution] Starting task: {goal}")
                
                try:
                    # Offload the blocking agent loop to a worker thread
                    result_state = await asyncio.to_thread(self._run_agent_sync, goal)
                    
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
                active_profile = params.get("active_profile", "home")
                profile_settings = params.get("profile_settings", {})
                
                # Update active profile marker
                self.config["active_profile"] = active_profile
                
                # Merge the new settings into that specific profile
                if "profiles" not in self.config:
                    self.config["profiles"] = {}
                if active_profile not in self.config["profiles"]:
                    self.config["profiles"][active_profile] = {}
                    
                self.config["profiles"][active_profile].update(profile_settings)
                
                # Write to disk so it persists across server restarts
                config_path = os.path.join(project_root, "services", "orchestrator", "config.json")
                with open(config_path, "w", encoding="utf-8") as f:
                    json.dump(self.config, f, indent=4)
                    
                logger.info(f"⚙️ Orchestrator configuration updated. Active profile: {active_profile}")
                return self._success_response(req_id, {"status": "config_updated"})
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

    def _run_agent_sync(self, goal: str):
        """Synchronous wrapper to instantiate and run the agent."""
        
        # 1. Determine which profile is active
        active_profile_name = self.config.get("active_profile", "home")
        profiles = self.config.get("profiles", {})
        active_config = profiles.get(active_profile_name, {})
        
        # 2. Extract LLM settings for this specific environment
        llm_config = active_config.get("llm", {})
        
        llm = OllamaProxyProvider(
            endpoint_url=llm_config.get("endpoint_url", "http://127.0.0.1:11435/v1/chat/completions"),
            model=llm_config.get("model_name", "llama3:8b")
        )
        
        # Pass the environment-specific config down to the Agent
        agent = Agent(llm_provider=llm, config=active_config)
        return agent.run(goal)

    def _success_response(self, req_id: Any, result: Any) -> Dict[str, Any]:
        return {"jsonrpc": "2.0", "result": result, "id": req_id}

    def _error_response(self, req_id: Any, code: int, message: str) -> Dict[str, Any]:
        return {"jsonrpc": "2.0", "error": {"code": code, "message": message}, "id": req_id}

    async def start(self):
        """Binds the UDS socket and enforces strict 0600 permissions."""
        if os.path.exists(self.socket_path):
            os.remove(self.socket_path)
            
        server = await asyncio.start_unix_server(self.handle_client, path=self.socket_path)
        
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

if __name__ == "__main__":
    server = JsonRpcUdsServer()
    try:
        asyncio.run(server.start())
    except KeyboardInterrupt:
        logger.info("Shutting down UDS server.")
        if os.path.exists(server.socket_path):
            os.remove(server.socket_path)
