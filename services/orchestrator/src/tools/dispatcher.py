import asyncio
import difflib
import json
from loguru import logger
import os
import subprocess
from typing import Any, Dict, Optional

from services.orchestrator.src.tools.sandbox import (
    execute_python_repl,
    validate_python_syntax,
    validate_terminal_command,
)
from opentelemetry import trace

tracer = trace.get_tracer(__name__)
audit_logger = logger.bind(audit=True)

class ToolDispatcher:
    """Handles structured JSON tool requests with a Tiered Operational Rights Proxy."""

    def __init__(
        self,
        uds_server=None,
        workspace_root: Optional[str] = None,
        permission_callback=None,
        sandbox_config: Optional[Dict[str, Any]] = None,
        max_file_read_chars: int = 4000,
    ):
        self.uds_server = uds_server
        self.workspace_root = workspace_root or os.getcwd()
        self.permission_callback = permission_callback
        self.sandbox_config = sandbox_config or {}
        self.max_file_read_chars = max_file_read_chars
        self.shell_deny_list = [
            "rm", "sudo", "mkfs", "fdisk", "dd", "chown", "chmod",
            "shutdown", "reboot", "ufw", "iptables", "firewall-cmd",
            "nano", "vim", "top", "history",
        ]

    async def execute_async(
        self, tool_name: str, arguments: Dict[str, Any], auto_approve: bool = False
    ) -> str:
        logger.info(
            f"🔧 [Tool Call] Dispatching '{tool_name}' with args: {arguments} (Auto-Approve: {auto_approve})"
        )

        try:
            if tool_name == "file_system":
                return await self._handle_file_system_async(arguments, auto_approve=auto_approve)
            elif tool_name == "terminal_proxy":
                return await self._handle_terminal_proxy_async(arguments, auto_approve=auto_approve)
            elif tool_name == "python_repl":
                return execute_python_repl(arguments.get("code", ""))
            elif tool_name == "validate_python_syntax":
                return validate_python_syntax(arguments.get("code", ""))
            elif tool_name == "finish_task":
                return "Task marked as complete by the agent."
            elif tool_name == "apply_inline_diff":
                return await self._handle_apply_inline_diff_async(arguments, auto_approve=auto_approve)
            else:
                return f"Error: Tool '{tool_name}' not recognized."
        except Exception as e:
            return f"Error executing {tool_name}: {str(e)}"

    async def _handle_file_system_async(self, args: Dict[str, Any], auto_approve: bool = False) -> str:
        action = args.get("action")
        path = args.get("path", ".")

        expanded_path = os.path.expanduser(path)
        if not os.path.isabs(expanded_path):
            expanded_path = os.path.join(self.workspace_root, expanded_path)

        abs_target = os.path.abspath(expanded_path)
        abs_workspace = os.path.abspath(self.workspace_root)

        is_authorized = False
        if not self.sandbox_config.get("strict_mode", True):
            is_authorized = True
        else:
            if os.path.commonpath([abs_target, abs_workspace]) == abs_workspace:
                is_authorized = True
            else:
                for allowed_dir in self.sandbox_config.get("allowed_external_paths", []):
                    abs_allowed = os.path.abspath(os.path.expanduser(allowed_dir))
                    if os.path.commonpath([abs_target, abs_allowed]) == abs_allowed:
                        is_authorized = True
                        break

        if not is_authorized:
            return (
                f"error: command blocked by sandbox. Path '{path}' is outside "
                f"authorized workspace root and not in allowed_external_paths."
            )

        path = abs_target

        if action == "read":
            forbidden_files = [".env", ".agentic_config.json", "secrets.json"]
            if any(f in path for f in forbidden_files):
                return "❌ Security Sandbox Violation: Access to configuration and environment files is strictly prohibited."

            if os.path.isdir(path):
                return f"Directory listing for '{path}': {json.dumps(os.listdir(path))}"
            elif os.path.isfile(path):
                with open(path, "r", encoding="utf-8") as f:
                    content = f.read()
                if len(content) > self.max_file_read_chars:
                    return (
                        f"File content of '{path}' (TRUNCATED - File is too large):\n"
                        f"{content[:self.max_file_read_chars]}\n\n"
                        f"...[TRUNCATED]... The file is too large to read entirely. "
                        f"You MUST use the 'search_codebase' tool to query specific parts of this file."
                    )
                return f"File content of '{path}':\n{content}"
            else:
                return f"Error: Path '{path}' does not exist."

        elif action == "write":
            content = args.get("content", "")

            if auto_approve:
                logger.info(f"⚡ [Auto-Approve] Silently writing to '{path}'...")
                with open(path, "w", encoding="utf-8") as f:
                    f.write(content)
                audit_logger.info(f"AUDIT LOG: Auto-approved file write to '{path}'")
                return f"Successfully wrote to file '{path}'."

            if not self.uds_server:
                return "Error: Cannot request write permission. IPC Server not attached."

            logger.info(f"⏸️  [Proxy] Requesting write permission for '{path}'...")
            
            with tracer.start_as_current_span("hitl_write_approval") as span:
                span.set_attribute("hitl.file_path", path)
                
                response = await self.uds_server.request_client_context(
                    "request_write_permission", {"path": path, "content": content}
                )

                if "content" in response and "timed out" in response["content"]:
                    span.set_attribute("hitl.action", "timeout")
                    return response["content"]

                if response.get("status") == "approved":
                    span.set_attribute("hitl.action", "approved")
                    with open(path, "w", encoding="utf-8") as f:
                        f.write(content)
                    audit_logger.info(f"AUDIT LOG: User APPROVED file write to '{path}'")
                    return f"Successfully wrote to file '{path}'."
                else:
                    span.set_attribute("hitl.action", "rejected")
                    audit_logger.warning(f"AUDIT LOG: User REJECTED file write to '{path}'")
                    return f"Action Blocked: The user denied the file write request for '{path}'."

        return f"Error: Unsupported file system action '{action}'."

    async def _handle_terminal_proxy_async(self, args: Dict[str, Any], auto_approve: bool = False) -> str:
        command = args.get("command")
        if not command:
            return "Error: No command provided."

        is_safe, error_msg = validate_terminal_command(self.workspace_root, command)
        if not is_safe:
            logger.warning(f"🔒 [Sandbox Blocked] Command: '{command}'")
            return error_msg

        command_lower = command.lower()
        if any(forbidden in command_lower.split() for forbidden in self.shell_deny_list):
            return f"SECURITY VIOLATION: Command execution blocked. '{command}' contains forbidden keywords."

        if auto_approve:
            logger.info(f"⚡ [Auto-Approve] Silently executing '{command}'...")
            audit_logger.info(f"AUDIT LOG: Auto-approved terminal command: '{command}'")
            user_timeout = 30
            try:
                process = await asyncio.create_subprocess_shell(
                    command,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    cwd=self.workspace_root,
                )
                stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=user_timeout)
                output = stdout.decode("utf-8") if process.returncode == 0 else stderr.decode("utf-8")
                return f"Command exit code {process.returncode}.\nOutput:\n{output}"
            except asyncio.TimeoutError:
                try:
                    process.kill()
                except ProcessLookupError:
                    pass
                return f"Error: Command execution timed out after {user_timeout} seconds."

        if self.permission_callback:
            logger.info(f"⏸️  [Proxy] Requesting TUI permission for '{command}'...")
            is_approved = await self.permission_callback(f"Allow shell execution:\n\n{command}")
            if is_approved:
                audit_logger.info(f"AUDIT LOG: User APPROVED terminal command: '{command}' via TUI")
                result = subprocess.run(
                    command, shell=True, capture_output=True, text=True, cwd=self.workspace_root
                )
                output = result.stdout if result.returncode == 0 else result.stderr
                return f"Command exit code {result.returncode}.\nOutput:\n{output}"
            else:
                audit_logger.warning(f"AUDIT LOG: User REJECTED terminal command: '{command}' via TUI")
                return "Action Blocked: The user denied the shell execution request."

        if not self.uds_server:
            return "Error: Cannot request shell permission. IPC Server not attached."

        logger.info(f"⏸️  [Proxy] Requesting shell execution permission for '{command}'...")
        
        with tracer.start_as_current_span("hitl_shell_approval") as span:
            span.set_attribute("hitl.command", command)
            
            response = await self.uds_server.request_client_context(
                "request_shell_permission", {"command": command}
            )

            if "content" in response and "timed out" in response["content"]:
                span.set_attribute("hitl.action", "timeout")
                return response["content"]

            if response.get("status") == "approved":
                span.set_attribute("hitl.action", "approved")
                audit_logger.info(f"AUDIT LOG: User APPROVED terminal command: '{command}'")
                try:
                    user_timeout = int(response.get("timeout", 30))
                except (ValueError, TypeError):
                    user_timeout = 30

                try:
                    process = await asyncio.create_subprocess_shell(
                        command,
                        stdout=asyncio.subprocess.PIPE,
                        stderr=asyncio.subprocess.PIPE,
                        cwd=self.workspace_root,
                    )
                    stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=user_timeout)
                    output = stdout.decode("utf-8") if process.returncode == 0 else stderr.decode("utf-8")
                    return f"Command exit code {process.returncode}.\nOutput:\n{output}"
                except asyncio.TimeoutError:
                    try:
                        process.kill()
                    except ProcessLookupError:
                        pass
                    return f"Error: Command execution timed out after {user_timeout} seconds."
            else:
                span.set_attribute("hitl.action", "rejected")
                audit_logger.warning(f"AUDIT LOG: User REJECTED terminal command: '{command}'")
                return "Action Blocked: The user denied the shell execution request."

    async def _handle_apply_inline_diff_async(self, args: Dict[str, Any], auto_approve: bool = False) -> str:
        path = args.get("file_path", args.get("path", args.get("file", "")))
        search_string = args.get("search_string", args.get("search", args.get("original_string", "")))
        replace_string = args.get("replace_string", args.get("replace", args.get("new_string", "")))

        if not path or not search_string:
            return "Error: file_path and search_string are required."

        if len(search_string) > 1000:
            return "Error: search_string is too large. You must target a specific function or block of code (under 1000 characters), not the entire file or class."

        abs_target = os.path.abspath(os.path.expanduser(path))
        abs_workspace = os.path.abspath(self.workspace_root)
        if self.sandbox_config.get("strict_mode", True) and not abs_target.startswith(abs_workspace):
            return f"Error: Path '{path}' is outside authorized workspace root."

        if not os.path.isfile(path):
            return f"Error: File '{path}' does not exist."

        with open(path, "r", encoding="utf-8") as f:
            content = f.read()

        count = content.count(search_string)
        if count == 0:
            return "Error: search_string not found in file. Ensure exact whitespace and indentation match."
        if count > 1:
            return f"Error: search_string found {count} times. Include more surrounding lines to make it unique."

        new_content = content.replace(search_string, replace_string)

        return await self._handle_file_system_async(
            {"action": "write", "path": abs_target, "content": new_content},
            auto_approve=auto_approve,
        )
