import asyncio
import json
import os
import stat
import logging
import sys
from pathlib import Path
from typing import Any, Dict

# Dynamically resolve the project root to ensure imports work from any execution path
project_root = str(Path(__file__).resolve().parent.parent.parent.parent.parent)
if project_root not in sys.path:
    sys.path.append(project_root)

from services.orchestrator.src.agent.agent_loop import Agent, OllamaProxyProvider

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)

class JsonRpcUdsServer:
    """JSON-RPC 2.0 Server over Unix Domain Sockets."""
    
    def __init__(self, socket_path: str = "/tmp/agent.sock"):
        self.socket_path = socket_path

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
                    
            else:
                return self._error_response(req_id, -32601, "Method not found")
                
        except json.JSONDecodeError:
            return self._error_response(None, -32700, "Parse error")
        except Exception as e:
            return self._error_response(req.get("id"), -32603, f"Internal error: {str(e)}")

    def _run_agent_sync(self, goal: str):
        """Synchronous wrapper to instantiate and run the agent."""
        llm = OllamaProxyProvider()
        agent = Agent(llm_provider=llm)
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

if __name__ == "__main__":
    server = JsonRpcUdsServer()
    try:
        asyncio.run(server.start())
    except KeyboardInterrupt:
        logger.info("Shutting down UDS server.")
        if os.path.exists(server.socket_path):
            os.remove(server.socket_path)
