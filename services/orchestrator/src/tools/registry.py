import logging
import re
from typing import Optional

from services.orchestrator.src.agent.ast_parser import CodebaseASTParser
from services.orchestrator.src.rag.search_manager import SearchManager
from services.orchestrator.src.rag.vector_store import LocalVectorStore
from services.orchestrator.src.tools.sandbox import execute_python_repl, validate_python_syntax

logger = logging.getLogger(__name__)


class ToolRegistry:
    def __init__(self, uds_server=None):
        self.uds_server = uds_server
        self.vector_store = LocalVectorStore()
        self.search_manager = SearchManager(uds_server=uds_server, vector_store=self.vector_store)

        self.tools = {
            "get_symbol_references": {
                "name": "get_symbol_references",
                "description": "Asks the IDE for all cross-file references of a symbol. Returns a list of file URIs and line numbers.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "symbol": {"type": "string", "description": "The name of the class, function, or variable."},
                        "file_path": {"type": "string", "description": "Absolute path to the active file."},
                        "line": {"type": "integer", "description": "The 1-indexed line number shown in the editor."},
                        "character": {"type": "integer", "description": "The 0-indexed character column position."},
                    },
                    "required": ["symbol", "file_path", "line", "character"],
                },
            },
            "extract_code_structure": {
                "name": "extract_code_structure",
                "description": "Extracts the exact class or function definition from a file at a specific line number using Tree-sitter AST parsing. Use this to read code without loading massive files.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "file_path": {"type": "string", "description": "Absolute path to the file."},
                        "line_number": {"type": "integer", "description": "0-indexed line number."},
                    },
                    "required": ["file_path", "line_number"],
                },
            },
            "terminal_proxy": {
                "name": "terminal_proxy",
                "description": "Executes a shell command. Use this for running tests, compiling, or executing scripts.",
                "parameters": {
                    "type": "object",
                    "properties": {"command": {"type": "string", "description": "The bash command to execute."}},
                    "required": ["command"],
                },
            },
            "file_system": {
                "name": "file_system",
                "description": "Reads or writes files to the local disk.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "action": {"type": "string", "description": "Either 'read' or 'write'."},
                        "path": {"type": "string", "description": "The target file path."},
                        "content": {"type": "string", "description": "The string to write (required if action is 'write')."},
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
                        "summary": {"type": "string", "description": "The comprehensive final answer or requested information."}
                    },
                    "required": ["summary"],
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
                        "target_path": {"type": "string", "description": "The absolute path to the file or folder"},
                    },
                    "required": ["command", "target_path"],
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
                "description": "Searches the local codebase using semantic vector embeddings. Use this when you need to find where a function, class, or variable is defined, or to understand how a specific part of the local project works.",
                "parameters": {
                    "type": "object",
                    "properties": {"query": {"type": "string", "description": "The semantic search query"}},
                    "required": ["query"],
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
                        "search_string": {"type": "string", "description": "The EXACT string block to be replaced."},
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

                path_arg = arguments.get("file_path") or arguments.get("file_uri") or arguments.get("uri")
                line_arg = arguments.get("line") or arguments.get("line_number") or 0
                char_arg = arguments.get("character") or arguments.get("character_position") or arguments.get("char") or 0
                symbol_arg = arguments.get("symbol")

                if not path_arg:
                    return "Error: You must provide a valid 'file_path'."

                if str(path_arg).startswith("file://"):
                    path_arg = str(path_arg).replace("file://", "")

                lsp_line = int(line_arg) - 1 if int(line_arg) > 0 else 0
                lsp_char = int(char_arg)

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

                refs = response if isinstance(response, list) else response.get("result", [])
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
                    arguments.get("file_path"), arguments.get("line_number", 0)
                )
            else:
                return f"Tool {tool_name} not found."

        except Exception as e:
            return f"Error executing {tool_name}: {str(e)}"