import os
import sys
import asyncio
import datetime

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
# --- Inject .env loader using the resolved workspace root ---
from dotenv import load_dotenv
load_dotenv(dotenv_path=os.path.join(workspace_root, ".env"), override=True)

import azure.identity
import collections.abc
import copy
import hashlib
import httpx
import json
from opentelemetry import trace
from loguru import logger
import logging
import openai
from pathlib import Path
import pydantic
import socket
import stat
from typing import Any, Dict, Optional
import uuid

# 🎯 FAILSAFE: Force UTF-8 encoding on Windows to prevent emoji crashes
if sys.stdout:
    reconfigure = getattr(sys.stdout, 'reconfigure', None)
    if callable(reconfigure):
        reconfigure(encoding='utf-8', errors='replace')
if sys.stderr:
    reconfigure = getattr(sys.stderr, 'reconfigure', None)
    if callable(reconfigure):
        reconfigure(encoding='utf-8', errors='replace')

script_dir = os.path.dirname(os.path.abspath(__file__))
src_dir = os.path.abspath(os.path.join(script_dir, ".."))  # .../services/orchestrator/src

for path in (src_dir, workspace_root):
    if path not in sys.path:
        sys.path.insert(0, path)

# Added here to prevent ModuleNotFoundError because 
# uds_server.py is executedly directly during dev mod
# and is buried deep inside the ipc folder where Python 
# does not naturally know about the services directory
# until the internal sys.path.insert logic executes.
from rag.vector_store import LocalVectorStore
from services.orchestrator.src.agent.agent_loop import Agent, Message, Role
from services.orchestrator.src.llm.provider import UniversalLLMProvider
from services.orchestrator.src.llm.tracing import setup_phoenix_tracing
from services.orchestrator.src.logging.log_filter import scrub_secrets_from_string, SecretRedactingFilter
from services.orchestrator.src.memory.persistence import MessageStore

from opentelemetry import trace
tracer = trace.get_tracer(__name__)

def extract_auto_approve(params):
    """Extracts auto-approve flag only from the top two levels of the params dict, avoiding history blobs."""
    if "auto_approve" in params:
        return params["auto_approve"]
    if "autoApprove" in params:
        return params["autoApprove"]
    
    for k, v in params.items():
        if isinstance(v, dict):
            if "auto_approve" in v:
                return v["auto_approve"]
            if "autoApprove" in v:
                return v["autoApprove"]
    return False

def setup_logging(config_data=None):
    # 1. Defaults
    obs_enabled = False
    obs_endpoint = "http://127.0.0.1:6006/v1/traces"
    log_level = "INFO"
    log_rotation = "10 MB"
    log_retention = "5 days"

    if config_data:
        active_profile_name = config_data.get("active_profile", "Default")
        profiles = config_data.get("profiles", {})
        active_config = profiles.get(active_profile_name, config_data) if isinstance(profiles, dict) else config_data
        
        if "observability" in active_config:
            obs_enabled = active_config["observability"].get("enabled", obs_enabled)
            obs_endpoint = active_config["observability"].get("endpoint", obs_endpoint)
        
        if "logging" in active_config:
            log_level = active_config["logging"].get("level", log_level)
            log_rotation = active_config["logging"].get("rotation", log_rotation)
            log_retention = active_config["logging"].get("retention", log_retention)

    # 2. Configure OpenTelemetry ONLY if enabled
    if obs_enabled:
        try:
            setup_phoenix_tracing(endpoint=obs_endpoint)
        except Exception:
            pass  # Only initialize once

    # 3. Configure Loguru
    log_dir = os.path.join(workspace_root, ".agentic_workspace", "logs")
    os.makedirs(log_dir, exist_ok=True)

    def secret_redacting_filter(record):
        record["message"] = scrub_secrets_from_string(str(record["message"]))
        return True

    logger.remove()
    logger.add(sys.stdout, format="<green>{time:HH:mm:ss}</green> | <level>{level: <8}</level> | <level>{message}</level>", filter=secret_redacting_filter, level=log_level)
    
    # Standard flat text log for lean operation
    logger.add(
        os.path.join(log_dir, "agentic_backend.log"),
        rotation=log_rotation,
        retention=log_retention,
        level=log_level,
        filter=secret_redacting_filter
    )

    # 4. Only write heavy JSONL traces and audit logs if observability is explicitly enabled
    if obs_enabled:
        logger.add(
            os.path.join(log_dir, "telemetry.jsonl"), 
            serialize=True,
            rotation=log_rotation,
            retention=log_retention,
            level=log_level,
            filter=lambda record: "audit" not in record["extra"] and secret_redacting_filter(record)
        )
        
        # Immutable Security & Audit Logger
        logger.add(
            os.path.join(log_dir, "audit.jsonl"),
            serialize=True,
            rotation="50 MB",
            retention="1 year",
            level="INFO",
            filter=lambda record: "audit" in record["extra"] and secret_redacting_filter(record)
        )

# Initial setup before config is loaded
setup_logging()

config_path = os.path.join(workspace_root, '.agentic_config.json')

def load_config():
    """Load config from the workspace, fallback to global, or create a default."""
    local_config_path = os.path.join(workspace_root, '.agentic_config.json')
    if os.path.exists(local_config_path):
        try:
            with open(local_config_path, 'r') as f:
                return json.load(f)
        except Exception as e:
            logger.warning(f"Error reading local config: {e}")
            
    default_config = {
        "active_profile": "Default",
        "profiles": {
            "Default": {
                "llm": {
                    "model_name": "llama3:8b",
                    "endpoint_url": "http://127.0.0.1:11434/v1/chat/completions",
                    "chars_per_token": 4.0
                },
                "memory": {
                    "max_tokens": 6000
                },
                "logging": {
                    "level": "INFO",
                    "rotation": "10 MB",
                    "retention": "5 days"
                },
                "observability": {
                    "enabled": False,
                    "endpoint": "http://127.0.0.1:6006/v1/traces"
                }
            }
        }
    }
    
    save_config(default_config, force_global=False)
    logger.info(f"⚙️ Auto-created missing workspace config at: {local_config_path}")
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
    """JSON-RPC 2.0 Server over TCP."""
    
    def __init__(self, workspace_root: str = '.', host: str = '127.0.0.1', port: int = 7777):
        self.host = host
        self.port = port
        self.config = load_config()
        setup_logging(self.config)
        self.running = False
        self.notification_handlers = {}
        self.workspace_root = workspace_root
        logger.info("Initializing LanceDB Vector Store...")
        self.vector_store = LocalVectorStore(workspace_root=workspace_root, config=self.config)
        logger.info("Initializing SQLite Message Store...")
        db_path = Path(workspace_root) / ".agentic_workspace" / "db" / "orchestrator.db"
        self.message_store = MessageStore(db_path=db_path)
        self.pending_requests = {}
        self.active_writer = None
        self.persistent_agent = None
        self.sync_queue = asyncio.Queue()
        self.sync_task = None

    def register_notification_handler(self, method_name: str, callback_coroutine):
        """Registers an async callback for a specific incoming notification."""
        self.notification_handlers[method_name] = callback_coroutine

    async def _sync_worker(self):
        """Background worker that pulls files from the queue and indexes them without blocking the event loop."""
        logger.info("Background sync worker started.")
        while True:
            try:
                file_path, file_hash, file_content = await self.sync_queue.get()
                with tracer.start_as_current_span("background_sync_processing") as span:
                    span.set_attribute("queue.depth", self.sync_queue.qsize())
                    span.set_attribute("sync.file_path", file_path)
                    logger.info(f"⏳ [Sync Worker] Start index: {file_path} (Queue depth: {self.sync_queue.qsize()})")
                    await asyncio.to_thread(self.vector_store.upsert_file, file_path, file_hash, file_content)
                    logger.info(f"✅ [Sync Worker] Finish index: {file_path}")
                self.sync_queue.task_done()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"❌ [Sync Worker] Error during background sync: {e}")

    async def handle_client(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        """Reads incoming streams, dispatches JSON-RPC requests, and writes responses."""

        try:
            while True:
                data = await reader.readline()
                if not data:
                    break

                import time
                t0 = time.time()
                payload = data.decode('utf-8').strip()
                if not payload:
                    continue
                    
                req = json.loads(payload)
                t1 = time.time()
                
                with tracer.start_as_current_span("ipc_receive") as span:
                    span.set_attribute("ipc.payload_bytes", len(data))
                    span.set_attribute("ipc.deserialization_ms", (t1 - t0) * 1000)
                    span.set_attribute("ipc.method", req.get("method", "response"))

                safe_msg = sanitize_ipc_payload(req)
                logger.debug(f"🕵️ [RAW SOCKET] {json.dumps(safe_msg)}")

                # --- Smart Client Routing ---
                # Only designate the connection as the VS Code Extension Host if it sends editor-specific commands.
                # The TUI will never send these, preventing it from stealing the context connection.
                if req.get("method") in ["sync_file", "update_config", "get_config", "ping", "execute_agent_task"]:
                    self.active_writer = writer

                if "method" in req and "id" not in req:
                    method = req["method"]
                    params = req.get("params", {})
                    logger.info(f"📥 [IPC] Received notification: {method}")
                    
                    if method == "cancel_agent_task":
                        if self.persistent_agent and hasattr(self.persistent_agent, 'state'):
                            self.persistent_agent.state.is_canceled = True
                            logger.info("🛑 Emergency brake pulled! Stopping agent loop...")
                            # Unblock any pending IPC UI requests immediately
                            for req_id, future in self.pending_requests.items():
                                if not future.done():
                                    future.set_result({"status": "denied", "content": "Action Blocked: Task was canceled."})
                            self.pending_requests.clear()
                        continue

                    if method in self.notification_handlers:
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
            try:
                await writer.wait_closed()
            except (ConnectionResetError, BrokenPipeError) as e:
                logger.error(f"Error while waiting for writer to close: {e}")

            # 🎯: Immediately cancel all pending IPC requests if VS Code reloads/disconnects
            for req_id, future in self.pending_requests.items():
                if not future.done():
                    future.set_exception(ConnectionError("VS Code client disconnected abruptly (window reloaded)."))
            self.pending_requests.clear()

    async def _dispatch_request(self, payload: str, writer: asyncio.StreamWriter):
        """Processes the request in the background and writes the response."""
        try:
            response = await self.process_request(payload)
            raw_json = json.dumps(response)
            safe_json = scrub_secrets_from_string(raw_json)
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
        active_profile = payload.get("active_profile")
        profile_settings = payload.get("profile_settings", {})
        if not active_profile:
            return {"error": "active_profile is required"}
        self.config["active_profile"] = active_profile
        if "profiles" not in self.config:
            self.config["profiles"] = {}
        if active_profile not in self.config["profiles"]:
            self.config["profiles"][active_profile] = {}

        # Safely deep merge the incoming settings from the UI
        if profile_settings:
            self.config["profiles"][active_profile] = deep_update(
                self.config["profiles"][active_profile], 
                profile_settings
            )
        save_config(self.config)
        self.persistent_agent = None
        setup_logging(self.config)
        logger.info(f"✅ Config successfully updated and saved for profile: '{active_profile}'")
        
        # CRITICAL: Return the full config so the React frontend can update its state
        return {"status": "success", "config": self.config}

    async def process_request(self, payload: str) -> Dict[str, Any]:
        req = {}
        try:
            req = json.loads(payload)
            method = req["method"]
            params = req.get("params", {})
            req_id = req["id"]
            
            if method == "ping":
                return self._success_response(req_id, "pong")
                
            elif method == "execute_agent_task":
                goal = params.get("goal")
                if not goal:
                    return self._error_response(req_id, -32602, "Invalid params: 'goal' is required")

                is_auto_approve = extract_auto_approve(params)
                if isinstance(is_auto_approve, str):
                    is_auto_approve = is_auto_approve.lower() == 'true'
                else:
                    is_auto_approve = bool(is_auto_approve)
                if is_auto_approve:
                    logger.info("⚡ [Agent Execution] Auto-approve is enabled. Agent will execute without user confirmation.")

                logger.info(f"🧠 [Agent Execution] Starting task: {goal}")
                
                # Immediately notify the frontend that the agent is busy to force the Stop button to render
                await self.send_notification("agent_status", {"status": "busy", "message": "Initializing task..."})

                workflow_config = params.get("workflow_config", None)
                history_payload = params.get("history", [])
                session_id = params.get("session_id", None)
                
                try:
                    result_state = await self._run_agent_async(
                        goal, 
                        auto_approve=is_auto_approve, 
                        workflow_config=workflow_config, 
                        history=history_payload,
                        session_id=session_id
                    )
                    
                    if not result_state.is_complete and not result_state.is_canceled:
                        return self._success_response(req_id, {
                            "status": "paused_max_iterations",
                            "iterations": result_state.iterations
                        })

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
                    
            elif method == "cancel_agent_task":
                if self.persistent_agent and hasattr(self.persistent_agent, 'state'):
                    self.persistent_agent.state.is_canceled = True
                    logger.info("🛑 Emergency brake pulled via request! Stopping agent loop...")
                    for rid, future in self.pending_requests.items():
                        if not future.done():
                            future.set_result({"status": "denied", "content": "Action Blocked: Task was canceled."})
                    self.pending_requests.clear()
                return self._success_response(req_id, {"status": "canceled"})

            elif method == "resume_agent_task":
                mode = params.get("mode", "continue")
                is_auto_approve = extract_auto_approve(params)
                if isinstance(is_auto_approve, str):
                    is_auto_approve = is_auto_approve.lower() == 'true'
                else:
                    is_auto_approve = bool(is_auto_approve)
                
                if not self.persistent_agent:
                    return self._error_response(req_id, -32000, "No active agent session to resume.")
                
                if mode == "unhinged":
                    self.persistent_agent.state.max_iterations = 999999
                    logger.info("⚠️ Agent is now UNHINGED. Infinite loop limit removed.")
                else:
                    self.persistent_agent.state.iterations = 0
                    self.persistent_agent.state.max_iterations = self.persistent_agent.max_iterations
                    logger.info(f"🔄 Agent iterations reset. Resuming task (max_iterations={self.persistent_agent.max_iterations}).")
                
                # Resume without passing a new goal so it picks up where it left off
                result_state = await self._run_agent_async(None, auto_approve=is_auto_approve)
                
                if not result_state.is_complete and not result_state.is_canceled:
                    return self._success_response(req_id, {
                        "status": "paused_max_iterations",
                        "iterations": result_state.iterations
                    })
                    
                final_observation = "Task failed or max iterations reached."
                if result_state.history and result_state.history[-1].name == "finish_task":
                    final_observation = result_state.history[-1].content
                    
                return self._success_response(req_id, {
                    "status": "completed", 
                    "iterations": result_state.iterations,
                    "final_observation": final_observation
                })

            elif method == "set_auto_approve":
                if not self.persistent_agent:
                    return self._error_response(req_id, -32000, "No active agent session.")
                
                new_state = extract_auto_approve(params)
                self.persistent_agent.auto_approve = new_state
                logger.info(f"⚙️ Mid-task auto-approve set to: {new_state}")
                return self._success_response(req_id, {"status": "success", "auto_approve": new_state})
            elif method == "fetch_history":
                session_id = params.get("session_id")
                db_path = Path(self.workspace_root) / ".agentic_workspace" / "db" / "orchestrator.db"
                if db_path.exists() and session_id:
                    def _sync_fetch():
                        import sqlite3
                        with sqlite3.connect(str(db_path), timeout=5.0) as conn:
                            conn.row_factory = sqlite3.Row
                            cursor = conn.cursor()
                            cursor.execute("""
                                SELECT role, content, token_count, created_at 
                                FROM messages 
                                WHERE session_id = ? 
                                ORDER BY seq ASC
                            """, (session_id,))
                            return [dict(row) for row in cursor.fetchall()]
                    rows = await asyncio.to_thread(_sync_fetch)
                    return self._success_response(req_id, {"status": "success", "history": rows})
                else:
                    return self._success_response(req_id, {"status": "success", "history": []})
            elif method == "load_history_list":
                query_str = params.get("query", "").strip()
                db_path = Path(self.workspace_root) / ".agentic_workspace" / "db" / "orchestrator.db"
                if db_path.exists():
                    def _sync_load_list():
                        import sqlite3
                        with sqlite3.connect(str(db_path), timeout=5.0) as conn:
                            conn.row_factory = sqlite3.Row
                            cursor = conn.cursor()
                            if query_str:
                                cursor.execute("""
                                    SELECT DISTINCT session_id
                                    FROM messages
                                    WHERE content LIKE ? AND session_id IS NOT NULL AND session_id != ''
                                """, (f"%{query_str}%",))
                                matching_ids = {row["session_id"] for row in cursor.fetchall()}
                                if not matching_ids:
                                    return []
                                placeholders = ','.join(['?'] * len(matching_ids))
                                cursor.execute(f"""
                                    SELECT session_id, MAX(created_at) as timestamp
                                    FROM messages
                                    WHERE session_id IN ({placeholders})
                                    GROUP BY session_id
                                    ORDER BY timestamp DESC
                                    LIMIT 50
                                """, list(matching_ids))
                            else:
                                cursor.execute("""
                                    SELECT session_id, MAX(created_at) as timestamp
                                    FROM messages
                                    WHERE session_id IS NOT NULL AND session_id != ''
                                    GROUP BY session_id
                                    ORDER BY timestamp DESC
                                    LIMIT 50
                                """)
                            raw_sessions = cursor.fetchall()
                            sessions = []
                            for row in raw_sessions:
                                sid = row["session_id"]
                                timestamp = row["timestamp"]
                                cursor.execute("""
                                    SELECT content FROM messages 
                                    WHERE session_id = ? AND role = 'user' 
                                    ORDER BY seq ASC LIMIT 1
                                """, (sid,))
                                first_msg = cursor.fetchone()
                                title = f"Session {sid[:8]}..."
                                if first_msg and first_msg["content"]:
                                    try:
                                        content_data = json.loads(first_msg["content"])
                                        text_val = content_data.get("text") if isinstance(content_data, dict) else str(content_data)
                                        title = text_val[:30] + ('...' if len(text_val) > 30 else '')
                                    except Exception:
                                        title = str(first_msg["content"])[:30]
                                try:
                                    epoch = int(datetime.datetime.strptime(timestamp.replace('Z', ''), "%Y-%m-%dT%H:%M:%S.%f").replace(tzinfo=datetime.timezone.utc).timestamp() * 1000)
                                except Exception:
                                    try:
                                        epoch = int(datetime.datetime.strptime(timestamp.replace('Z', ''), "%Y-%m-%dT%H:%M:%S").replace(tzinfo=datetime.timezone.utc).timestamp() * 1000)
                                    except Exception:
                                        epoch = 0
                                sessions.append({"id": sid, "title": title, "timestamp": epoch})
                            return sessions

                    sessions = await asyncio.to_thread(_sync_load_list)
                    return self._success_response(req_id, {"status": "success", "sessions": sessions})
                else:
                    return self._success_response(req_id, {"status": "success", "sessions": []})
            elif method == "delete_history":
                session_id = params.get("session_id")
                db_path = Path(self.workspace_root) / ".agentic_workspace" / "db" / "orchestrator.db"
                if db_path.exists() and session_id:
                    def _sync_delete():
                        import sqlite3
                        with sqlite3.connect(str(db_path), timeout=5.0) as conn:
                            cursor = conn.cursor()
                            cursor.execute("DELETE FROM messages WHERE session_id = ?", (session_id,))
                            cursor.execute("DELETE FROM context_meta WHERE session_id = ?", (session_id,))
                            conn.commit()
                    await asyncio.to_thread(_sync_delete)
                    if self.persistent_agent and getattr(self.persistent_agent.state, "run_id", None) == session_id:
                        self.persistent_agent = None
                        logger.info(f"🗑️ Active session {session_id} deleted; persistent agent cleared.")
                    return self._success_response(req_id, {"status": "success", "session_id": session_id})
                else:
                    return self._success_response(req_id, {"status": "success", "deleted": False})
            elif method == "inject_message":
                if not self.persistent_agent:
                    return self._error_response(req_id, -32000, "No active agent session.")
                
                user_input = params.get("message", "").strip()
                if user_input:
                    self.persistent_agent.user_inbox.put_nowait(user_input)
                    logger.info(f"📥 Mid-task user input received: {user_input}")
                return self._success_response(req_id, {"status": "queued"})
            elif method == "update_config":
                res = self.handle_update_config(params)
                if "error" in res:
                    return self._error_response(req_id, -32000, res["error"])
                return self._success_response(req_id, res)
            elif method == "get_config":
                return self._success_response(req_id, self.config)
            elif method == "sync_file":
                return self._success_response(req_id, await self._handle_sync_file(params))
            elif method == "delete_file":
                return self._success_response(req_id, self._handle_delete_file(params))
            elif method == "search_codebase":
                return self._success_response(req_id, self._handle_search_codebase(params))
            elif method == "reset_session":
                self.persistent_agent = None
                logger.info("🗑️ Agent memory wiped for new session.")
                return self._success_response(req_id, {"status": "cleared"})
                
            elif method == "restore_session":
                session_id = params.get("session_id")
                if not session_id:
                    return self._error_response(req_id, -32602, "session_id is required to restore state.")
                
                self._ensure_agent()
                self.persistent_agent.state.run_id = session_id
                
                # The agent loop will automatically call context_manager.rehydrate() 
                # when it runs, pulling the untrimmed trajectory directly from disk!
                return self._success_response(req_id, {"status": "restored"})

            elif method == "create_profile":
                profile_name = params.get("profile_name")
                profile_data = params.get("profile_data", {})
                if not profile_name:
                    return self._error_response(req_id, -32602, "Profile name is required")
                if "profiles" not in self.config:
                    self.config["profiles"] = {}
                self.config["profiles"][profile_name] = profile_data
                save_config(self.config)
                return self._success_response(req_id, {"status": "success", "profiles": self.config["profiles"]})

            elif method == "delete_profile":
                profile_name = params.get("profile_name")
                if profile_name == "Default" or profile_name == self.config.get("active_profile"):
                    return self._error_response(req_id, -32600, "Cannot delete the default or currently active profile.")
                if profile_name in self.config.get("profiles", {}):
                    del self.config["profiles"][profile_name]
                    save_config(self.config)
                    return self._success_response(req_id, {"status": "success", "profiles": self.config["profiles"]})
                return self._error_response(req_id, -40404, "Profile not found")
            else:
                return self._error_response(req_id, -32601, f"Method '{method}' not found")
                
        except json.JSONDecodeError:
            return self._error_response(None, -32700, "Parse error")
        except Exception as e:
            logger.error(f"Internal error processing request: {e}")
            return self._error_response(req.get("id") if isinstance(req, dict) else None, -32603, str(e))

    async def _handle_sync_file(self, params: Dict[str, Any]) -> Dict[str, Any]:
        """Hashes the incoming file and queues it for background LanceDB upsert."""
        file_path = params.get("file_path")
        content = params.get("content", "")
        if not file_path:
            raise ValueError("file_path is required for sync_file")

        # Generate a hash to track file modifications
        file_hash = hashlib.md5(content.encode('utf-8')).hexdigest()

        # Push to background queue instead of blocking the event loop
        await self.sync_queue.put((file_path, file_hash, content))
        return {"status": "success", "indexed_path": file_path, "message": "Queued for background sync"}

    def _handle_delete_file(self, params: Dict[str, Any]) -> Dict[str, Any]:
        """Removes all indexed chunks for the given file_path from LanceDB."""
        file_path = params.get("file_path")
        if not file_path:
            raise ValueError("file_path is required for delete_file")
        deleted = self.vector_store.delete_file(file_path)
        logger.info(f"🗑️ [IPC] delete_file called for: {file_path} (deleted={deleted})")
        return {"status": "success", "deleted_path": file_path, "deleted": bool(deleted)}

    def _ensure_agent(self):
        """Initializes the persistent agent if it doesn't exist."""
        if self.persistent_agent is not None:
            return

        active_profile_name = self.config.get("active_profile", "home")
        profiles = self.config.get("profiles", {})
        active_config = profiles.get(active_profile_name, self.config) if isinstance(profiles, dict) else self.config
        
        llm_config = active_config.get("llm", active_config)
        endpoint = (llm_config.get("endpoint_url") or llm_config.get("endpoint") or 
                   self.config.get("endpoint_url") or self.config.get("endpoint"))
        model_name = (llm_config.get("model_name") or llm_config.get("model") or 
                     self.config.get("model_name") or self.config.get("model"))
        api_key = (llm_config.get("apiKey") or llm_config.get("api_key") or 
                  self.config.get("apiKey") or self.config.get("api_key"))

        if endpoint and endpoint.endswith("11434"):
            endpoint = f"{endpoint}/v1/chat/completions"

        llm = UniversalLLMProvider(endpoint_url=endpoint, model=model_name, api_key=api_key)
        
        # workspace_root is defined globally at the top of uds_server.py
        self.persistent_agent = Agent(llm_provider=llm, config=active_config, uds_server=self, workspace_root=workspace_root)

    async def _run_agent_async(
        self,
        goal: str,
        auto_approve: bool = False,
        workflow_config: Optional[Dict[str, Any]] = None,
        history: list = None,
        session_id: Optional[str] = None
    ):
        """Asynchronously instantiates and runs the agent."""
        self._ensure_agent()
        if session_id and not self.persistent_agent.state.history:
            self.persistent_agent.state.run_id = session_id
        state = await self.persistent_agent.run(goal, auto_approve=auto_approve, workflow_config=workflow_config, history_payload=history)
        return state

    def _success_response(self, req_id: Any, result: Any) -> Dict[str, Any]:
        return {"jsonrpc": "2.0", "result": result, "id": req_id}

    def _error_response(self, req_id: Any, code: int, message: str) -> Dict[str, Any]:
        return {"jsonrpc": "2.0", "error": {"code": code, "message": message}, "id": req_id}

    async def start(self):
        """Binds the TCP socket."""
        # Open the global database connection before serving clients
        # Here to populate the history list ASAP
        await self.message_store.open()

        # 🧹 Run startup database reconciliation in a background thread
        logger.info("Starting database reconciliation sweep...")
        await asyncio.to_thread(self.vector_store.reconcile_database)

        # 🗄️ Run session archival sweep (Sessions older than 14 days)
        archive_db = Path(workspace_root) / ".agentic_workspace" / "db" / "orchestrator_archive.db"
        archived_count = await self.message_store.archive_inactive_sessions(retention_days=14, archive_path=archive_db)
        if archived_count > 0:
            logger.info(f"Archived {archived_count} stale message rows to {archive_db.name}")

        # Start the background sync worker
        self.sync_task = asyncio.create_task(self._sync_worker())
        server = await asyncio.start_server(
            self.handle_client, 
            self.host, 
            self.port,
            limit=50 * 1024 * 1024
        )
        logger.info(f"🔌 TCP JSON-RPC Server listening on {self.host}:{self.port}")
        async with server:
            await server.serve_forever()

        # Add cleanup (if server_forever breaks)
        await self.message_store.close()

    def _handle_search_codebase(self, params: Dict[str, Any]) -> Dict[str, Any]:
        import time
        query = params.get("query", "")
        limit = params.get("limit", 5)
        path_prefix = params.get("path_prefix", None)
        if not query:
            raise ValueError("Query string is required for search.")

        # Time the operation to verify sub-100ms latency
        start_time = time.time()
        raw_results = self.vector_store.semantic_search(
            query, limit=limit, path_prefix=path_prefix
        )
        
        # Strip the massive float vectors out before sending over IPC
        clean_results = []
        for r in raw_results:
            # 🎯 CACHE DEFEAT FIX: Truncate the file content to a 3-line snippet
            content = str(r.get("content", ""))
            lines = content.split('\n')
            snippet = '\n'.join(lines[:3])
            if len(lines) > 3:
                snippet += '\n... [TRUNCATED - Use extract_code_structure to read the full block]'

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
            raw_json = json.dumps(payload)
            safe_json = scrub_secrets_from_string(raw_json)
            self.active_writer.write((json.dumps(payload) + '\n').encode('utf-8'))
            await self.active_writer.drain()
        except Exception as e:
            logger.error(f"Failed to send notification: {e}")

def sanitize_ipc_payload(payload: dict) -> dict:
    """
    Recursively scrubs the IPC payload before logging.
    1. Redacts sensitive credentials (API keys).
    2. Truncates massive text blocks to prevent terminal flooding.
    """
    safe_payload = copy.deepcopy(payload)

    def _clean_node(node):
        if isinstance(node, dict):
            for key, value in node.items():
                k_lower = str(key).lower()
                
                # 1. Truncate Massive Context (Files, Terminal Output, Code)
                if k_lower in ["content", "output", "code", "file_content"] and isinstance(value, str):
                    if len(value) > 300:
                        node[key] = f"{value[:300]}\n... [TRUNCATED {len(value) - 300} chars for logs] ..."
                
                # 2. Recurse deeper
                else:
                    _clean_node(value)
        elif isinstance(node, list):
            for item in node:
                _clean_node(item)

    _clean_node(safe_payload)
    return safe_payload

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
        llm = UniversalLLMProvider(
            endpoint_url=active_config.get("llm", {}).get("endpoint_url"),
            model=active_config.get("llm", {}).get("model_name")
        )
        agent = Agent(llm_provider=llm, config=active_config, uds_server=server)
        logger.info("🧠 [Proactive AI] Spinning up background agent to deduce a fix...")
        await agent.run(goal)
    except Exception as e:
        logger.error(f"Failed to start proactive agent: {e}")

if __name__ == "__main__":
    import sys
    import argparse
    
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=7777)
    parser.add_argument("--workspace", default=".")
    parser.add_argument("--global-config", default="")
    args, unknown = parser.parse_known_args()

    server = JsonRpcUdsServer(
        host=args.host, 
        port=args.port, 
        workspace_root=args.workspace
    )
    
    # Register the handler before starting the server
    server.register_notification_handler("terminal_error_detected", handle_terminal_error)
    try:
        asyncio.run(server.start())
    except KeyboardInterrupt:
        logger.info("Shutting down TCP server gracefully.")
