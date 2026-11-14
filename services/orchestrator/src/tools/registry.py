import asyncio
import json
import logging
import re
from typing import Any, Dict, Optional, cast

from services.orchestrator.src.agent.ast_parser import CodebaseASTParser
from services.orchestrator.src.rag.search_manager import SearchManager
from services.orchestrator.src.rag.vector_store import LocalVectorStore
from services.orchestrator.src.tools.sandbox import execute_python_repl, validate_python_syntax

logger = logging.getLogger(__name__)


class ToolRegistry:
    def __init__(self, uds_server=None, config: Optional[Dict[str, Any]] = None):
        self.uds_server = uds_server
        self.config = config or {}

        # Reuse existing loaded LanceDB/ONNX models from server to avoid double memory footprint
        if self.uds_server and hasattr(self.uds_server, "vector_store"):
            self.vector_store = self.uds_server.vector_store
        else:
            self.vector_store = LocalVectorStore(config=self.config)

        self.search_manager = SearchManager(uds_server=self.uds_server, vector_store=self.vector_store)

        self.tools = {
            "get_symbol_references": {
                "name": "get_symbol_references",
                "description": "Asks the IDE for all cross-file references of a symbol. Returns a list of file URIs and line numbers.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "symbol": {"type": "string", "description": "The name of the class, function, or variable."},
                        "file_path": {"type": "string", "description": "Path to the file containing the symbol."},
                        "line": {"type": "integer", "description": "The 1-indexed line number shown in the editor."},
                        "character": {"type": "integer", "description": "The 0-indexed character column position."},
                    },
                    "required": ["symbol", "file_path"],
                },
            },
            "extract_code_structure": {
                "name": "extract_code_structure",
                "description": "Extracts the exact class or function definition from a file at a specific line number using Tree-sitter AST parsing. Use this to read code without loading massive files.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "file_path": {"type": "string", "description": "Path to the target file."},
                        "line_number": {"type": "integer", "description": "The 0-indexed absolute line number in the file (any line within the target class/function)."},
                    },
                    "required": ["file_path", "line_number"],
                },
            },
            "terminal_proxy": {
                "name": "terminal_proxy",
                "description": "Executes a shell command. Use this ONLY for running tests, compiling, or executing scripts (NEVER for grep/cat/sed/awk file reading).",
                "parameters": {
                    "type": "object",
                    "properties": {"command": {"type": "string", "description": "The bash command to execute."}},
                    "required": ["command"],
                },
            },
            "file_system": {
                "name": "file_system",
                "description": "Reads a file/directory or writes a full file to disk. For partial edits to existing files, prefer apply_inline_diff.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "action": {
                            "type": "string",
                            "enum": ["read", "write"],
                            "description": "Either 'read' (for a file or directory listing) or 'write'.",
                        },
                        "path": {"type": "string", "description": "The target file or directory path."},
                        "content": {
                            "type": "string",
                            "description": "The full string content to write (required when action is 'write').",
                        },
                    },
                    "required": ["action", "path"],
                },
            },
            "finish_task": {
                "name": "finish_task",
                "description": "Marks the agent loop as complete. ALWAYS call this when your goal is achieved.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "summary": {
                            "type": "string",
                            "description": "The comprehensive final answer or summary of changes made.",
                        },
                        "success": {
                            "type": "boolean",
                            "description": "True if the task succeeded, False if reporting an unrecoverable failure.",
                        },
                    },
                    "required": ["summary", "success"],
                },
            },
            "web_search": {
                "name": "web_search",
                "description": "Searches the live web for technical documentation, API specs, errors, or current information. Triggers a tiered fallback: Tavily -> Brave -> SearxNG.",
                "parameters": {
                    "type": "object",
                    "properties": {"query": {"type": "string", "description": "The dense search query string."}},
                    "required": ["query"],
                },
            },
            "vscode_command": {
                "name": "vscode_command",
                "description": "Executes a native VS Code command. Use 'vscode.openFolder' to open a directory workspace, or 'vscode.open' to open a specific file in the editor.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "command": {"type": "string", "description": "The VS Code command ID."},
                        "target_path": {"type": "string", "description": "Optional — only needed for path-aware commands (vscode.open, vscode.openFolder)."},
                    },
                    "required": ["command"],
                },
            },
            "validate_python_syntax": {
                "name": "validate_python_syntax",
                "description": "Natively validates Python code for syntax errors without executing it. ALWAYS use this to verify refactored code strings before writing them to the file system.",
                "parameters": {
                    "type": "object",
                    "properties": {"code": {"type": "string", "description": "The complete Python code to validate."}},
                    "required": ["code"],
                },
            },
            "python_repl": {
                "name": "python_repl",
                "description": "A sandboxed Python environment. Use this to execute Python code for mathematical calculations, data formatting, and complex logic. You MUST use print() to output results so they can be read.",
                "parameters": {
                    "type": "object",
                    "properties": {"code": {"type": "string", "description": "The Python script to execute."}},
                    "required": ["code"],
                },
            },
            "search_codebase": {
                "name": "search_codebase",
                "description": (
                    "Searches the workspace codebase using hybrid semantic and lexical search. "
                    "ALWAYS prefer this tool over shell grep/find."
                ),
                "handler": self._search_codebase_async,
                "parameters": {
                    "type": "object",
                    "properties": {
                        "query": {"type": "string", "description": "Search terms or exact code identifier."},
                        "path_prefix": {
                            "type": "string",
                            "description": "Optional subdirectory filter (e.g. 'apps/vscode-extension').",
                        },
                        "limit": {"type": "integer", "description": "Max results to return (default 5)."},
                    },
                    "required": ["query"],
                    "limit": {
                            "type": "integer", 
                            "description": "Max results to return. Default is 5, but lower this to 1 or 2 if context window is nearly full."
                        },
                        "offset": {
                            "type": "integer",
                            "description": "Number of results to skip for pagination (default 0). Use this to fetch the next set of results if a previous search was truncated."
                        },
                },
            },
            "get_active_file_content": {
                "name": "get_active_file_content",
                "description": "Retrieves the full source code text currently visible in the user's active VS Code editor window.",
                "parameters": {"type": "object", "properties": {}},
            },
            "get_selected_text": {
                "name": "get_selected_text",
                "description": "Retrieves the specific string of text the user currently has highlighted/selected in VS Code.",
                "parameters": {"type": "object", "properties": {}},
            },
            "apply_inline_diff": {
                "name": "apply_inline_diff",
                "description": "Precisely replaces a specific string block in a file. Use this to surgically modify code without rewriting the entire file.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "file_path": {"type": "string", "description": "Target file path."},
                        "search_string": {"type": "string", "description": "The EXACT string block to be replaced (under 1000 chars)."},
                        "replace_string": {"type": "string", "description": "The new string block to insert."},
                    },
                    "required": ["file_path", "search_string", "replace_string"],
                },
            },
        }

        if self.uds_server is None:
            for tool_name in ["get_active_file_content", "apply_inline_diff"]:
                self.tools.pop(tool_name, None)
            logger.info("🖥️ [TUI Mode] VS Code specific tools disabled.")

    def get_formatted_tool_descriptions(self) -> str:
        """Formats tool descriptions with explicit parameter names, types, enums, and required flags."""
        lines = []
        for name, info in self.tools.items():
            desc = cast(dict, info).get("description", "")
            params = cast(dict, info).get("parameters", {})
            props = params.get("properties", {})
            req = set(params.get("required", []))

            if props:
                param_strs = []
                for p_name, p_info in props.items():
                    p_type = p_info.get("type", "any")
                    p_desc = p_info.get("description", "")
                    opt_marker = "" if p_name in req else "?"
                    enum_vals = p_info.get("enum")
                    if enum_vals:
                        p_type = "|".join(f"'{v}'" for v in enum_vals)
                    param_strs.append(f'"{p_name}"{opt_marker}: <{p_type}> ({p_desc})')
                args_schema = "{ " + ", ".join(param_strs) + " }"
            else:
                args_schema = "{}"

            lines.append(f"- {name}: {desc}\n  tool_args schema: {args_schema}")
        return "\n".join(lines)

    @staticmethod
    def canonicalize_tool_args(tool_name: str, tool_args: Dict[str, Any]) -> Dict[str, Any]:
        """Normalizes common LLM parameter aliases into the canonical schema keys."""
        if not isinstance(tool_args, dict):
            return {}
        args = dict(tool_args)

        if tool_name == "file_system":
            if "path" not in args:
                for alias in ("file_path", "filepath", "file", "target_path", "directory"):
                    if alias in args:
                        args["path"] = args.pop(alias)
                        break
        elif tool_name in ("apply_inline_diff", "extract_code_structure", "get_symbol_references"):
            if "file_path" not in args:
                for alias in ("path", "filepath", "file", "uri", "file_uri", "target_path"):
                    if alias in args:
                        args["file_path"] = args.pop(alias)
                        break
            if tool_name == "apply_inline_diff":
                if "search_string" not in args:
                    for alias in ("search", "original_string", "old_str", "find"):
                        if alias in args:
                            args["search_string"] = args.pop(alias)
                            break
                if "replace_string" not in args:
                    for alias in ("replace", "new_string", "new_str", "replacement"):
                        if alias in args:
                            args["replace_string"] = args.pop(alias)
                            break
            elif tool_name == "extract_code_structure":
                if "line_number" not in args and "line" in args:
                    args["line_number"] = args.pop("line")
            elif tool_name == "get_symbol_references":
                if "line" not in args and "line_number" in args:
                    args["line"] = args.pop("line_number")
                if "character" not in args:
                    for alias in ("character_position", "char", "column", "col"):
                        if alias in args:
                            args["character"] = args.pop(alias)
                            break

        elif tool_name == "search_codebase":
            if "query" not in args:
                for alias in ("search_query", "pattern", "term", "keyword"):
                    if alias in args:
                        args["query"] = args.pop(alias)
                        break
            if "path_prefix" not in args:
                for alias in ("path", "directory", "dir", "folder", "scope"):
                    if alias in args:
                        args["path_prefix"] = args.pop(alias)
                        break

        elif tool_name == "terminal_proxy":
            if "command" not in args:
                for alias in ("cmd", "shell_command", "script", "bash"):
                    if alias in args:
                        args["command"] = args.pop(alias)
                        break

        return args

    def validate_tool_args(self, tool_name: str, tool_args: Dict[str, Any]) -> Optional[str]:
        """Validates tool_args against the tool's JSON schema and returns an actionable error hint if invalid."""
        if tool_name not in self.tools:
            return None
        schema = self.tools[tool_name].get("parameters", {})
        props = schema.get("properties", {})
        required = schema.get("required", [])

        # 1. Check missing required arguments
        missing = [
            req_key
            for req_key in required
            if req_key not in tool_args or tool_args[req_key] is None or tool_args[req_key] == ""
        ]
        if missing:
            expected_sig = {k: v.get("type", "any") for k, v in props.items()}
            return (
                f"Error [Invalid Tool Arguments]: Missing required parameter(s) {missing} for tool '{tool_name}'. "
                f"You passed keys {list(tool_args.keys())}, but '{tool_name}' requires schema: {json.dumps(expected_sig)}."
            )

        # 2. Check enum constraints & integer string coercion
        for key, val in list(tool_args.items()):
            if key in props:
                allowed_enums = props[key].get("enum")
                if allowed_enums and val not in allowed_enums:
                    return (
                        f"Error [Invalid Tool Arguments]: Invalid value '{val}' for parameter '{key}' in '{tool_name}'. "
                        f"Allowed values are: {allowed_enums}."
                    )
                if props[key].get("type") == "integer" and isinstance(val, str) and val.strip().isdigit():
                    tool_args[key] = int(val.strip())

        # 3. Special check for file_system write requiring content
        if tool_name == "file_system" and tool_args.get("action") == "write" and "content" not in tool_args:
            return "Error [Invalid Tool Arguments]: Parameter 'content' is required when file_system action is 'write'."

        return None

    async def execute_tool_async(self, tool_name: str, arguments: dict) -> Optional[str]:
        try:
            arguments = self.canonicalize_tool_args(tool_name, arguments)

            if tool_name == "search_codebase":
                return await self._search_codebase_async(arguments)

            elif tool_name == "vscode_command":
                if not self.uds_server:
                    return "Error: IPC Server not attached to ToolRegistry."
                response = await self.uds_server.request_client_context(tool_name, arguments)
                return response.get("content", "Error: No confirmation received from VS Code.")

            elif tool_name == "web_search":
                query = arguments.get("query")
                if not query or not isinstance(query, str):
                    return "Error: A non-empty 'query' string is required for web_search."

                max_chars = arguments.get("max_chars", 4000)
                run_id = arguments.get("run_id", "default_run")
                search_config = arguments.get("search_config", {})

                return await self.search_manager.execute_search(
                    query=query, run_id=run_id, search_config=search_config, max_chars=max_chars
                )

            elif tool_name in ["get_active_file_content", "get_selected_text"]:
                if not self.uds_server:
                    return "Error: IPC Server not attached to ToolRegistry."
                response = await self.uds_server.request_client_context(tool_name)
                return response.get("content", "Error: No content received from VS Code.")

            elif tool_name == "python_repl":
                return execute_python_repl(arguments.get("code", ""))

            elif tool_name == "validate_python_syntax":
                return validate_python_syntax(arguments.get("code", ""))

            elif tool_name == "get_symbol_references":
                if not self.uds_server:
                    return "Error: IPC Server not attached to ToolRegistry."

                path_arg = arguments.get("file_path")
                line_arg = arguments.get("line", 0)
                char_arg = arguments.get("character", 0)
                symbol_arg = arguments.get("symbol")

                if not path_arg:
                    return "Error: You must provide a valid 'file_path'."

                if str(path_arg).startswith("file://"):
                    path_arg = str(path_arg).replace("file://", "")

                lsp_line = int(line_arg) - 1 if int(line_arg) > 0 else 0  # type: ignore[assignment]
                lsp_char: int = int(char_arg)

                if symbol_arg:
                    try:
                        with open(path_arg, "r", encoding="utf-8") as f:
                            lines = f.readlines()
                            def_pattern = re.compile(rf"^(?:class|def|async def)\s+{re.escape(symbol_arg)}\b")
                            usage_pattern = re.compile(rf"\b{re.escape(symbol_arg)}\b")
                            char_offset = len(symbol_arg) // 2

                            if 0 <= lsp_line < len(lines) and def_pattern.search(lines[lsp_line].strip()):
                                char_idx = lines[lsp_line].find(symbol_arg)
                                lsp_char = char_idx + char_offset if char_idx != -1 else 0
                            else:
                                found = False
                                for i, line in enumerate(lines):
                                    if def_pattern.search(line.strip()):
                                        lsp_line = i
                                        char_idx = line.find(symbol_arg)
                                        lsp_char = char_idx + char_offset if char_idx != -1 else 0
                                        found = True
                                        break
                                if not found:
                                    if 0 <= lsp_line < len(lines) and usage_pattern.search(lines[lsp_line]):
                                        char_idx = lines[lsp_line].find(symbol_arg)
                                        lsp_char = char_idx + char_offset if char_idx != -1 else 0
                                    else:
                                        for i, line in enumerate(lines):
                                            if usage_pattern.search(line):
                                                lsp_line = i
                                                char_idx = line.find(symbol_arg)
                                                lsp_char = char_idx + char_offset if char_idx != -1 else 0
                                                break
                    except Exception as e:
                        logger.warning(f"Failed to auto-calculate LSP coordinates: {e}")

                payload = {"uri": path_arg, "line": lsp_line, "character": lsp_char}
                response = await self.uds_server.request_client_context("get_references", payload)

                if isinstance(response, dict) and "error" in response:
                    return f"LSP Error: {response['error']}"

                refs = response if isinstance(response, list) else cast(Dict[str, Any], response).get("result", [])
                if not refs:
                    return "No cross-file references found for this symbol."

                def find_line(obj):
                    if isinstance(obj, dict):
                        if "line" in obj:
                            return obj["line"]
                        for v in obj.values():
                            res = find_line(v)
                            if res is not None:
                                return res
                    elif isinstance(obj, list):
                        for item in obj:
                            res = find_line(item)
                            if res is not None:
                                return res
                    return None

                formatted_response = "Symbol References Found:\n"
                for loc in refs:
                    uri_obj = loc.get("uri", {}) or loc.get("targetUri", {})
                    file_path = uri_obj.get("fsPath") if isinstance(uri_obj, dict) else str(uri_obj)
                    line_num = "Unknown"
                    found_line = find_line(loc.get("range") or loc.get("targetSelectionRange") or loc)
                    if found_line is not None:
                        line_num = int(found_line) + 1
                    formatted_response += f"- File: {file_path} (Line: {line_num})\n"
                return formatted_response

            elif tool_name == "extract_code_structure":
                ast_parser = CodebaseASTParser()
                return ast_parser.get_node_at_line(
                    str(arguments.get("file_path", "")), int(arguments.get("line_number", 0))
                )
            else:
                return f"Tool {tool_name} not found."

        except Exception as e:
            return f"Error executing {tool_name}: {str(e)}"

    async def _search_codebase_async(self, args: Dict[str, Any]) -> str:
        query = str(args.get("query", "")).strip()
        if not query:
            return "Error: 'query' argument is required for search_codebase."

        limit = int(args.get("limit", 5))
        offset = int(args.get("offset", 0))
        path_prefix = (
            args.get("path_prefix")
            or args.get("path")
            or args.get("directory")
            or None
        )
        if isinstance(path_prefix, str):
            path_prefix = path_prefix.strip() or None

        if not self.vector_store:
            return "Error: Vector store is not initialized."

        try:
            results = await asyncio.to_thread(
                self.vector_store.semantic_search,
                query=query,
                limit=limit + offset,  # Fetch enough to cover the offset skip
                path_prefix=path_prefix,
            )
        except Exception as e:
            return f"Error executing codebase search: {e}"

        # Apply the pagination slice
        paged_results = results[offset:]

        if not paged_results:
            scope_note = f" within '{path_prefix}'" if path_prefix else ""
            return f"No codebase search results found for query '{query}'{scope_note} (offset: {offset})."

        formatted = [f"Codebase Search Results (Showing {limit} results, offset {offset}):\n"]
        for i, res in enumerate(paged_results, start=offset + 1):
            file_path = res.get("file_path", "unknown")
            content = res.get("content", "")
            formatted.append(f"--- Result {i} (File: {file_path}) ---\n{content}\n")

        return "\n".join(formatted)