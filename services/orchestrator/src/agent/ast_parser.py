import tree_sitter_python as tspython
from tree_sitter import Language, Parser
from typing import Any, cast

class CodebaseASTParser:
    """Extracts structural code blocks (functions/classes) from source files using Tree-sitter."""
    def __init__(self):
        # Handle new tree-sitter API where language() returns the object directly
        try:
            py_lang = tspython.language()
            if type(py_lang).__name__ != 'Language':
                py_lang = Language(py_lang)
        except Exception:
            # Fallback for older tree-sitter versions
            py_lang = cast(Any, Language)(tspython.language(), "python")

        self.languages = {
            ".py": py_lang,
        }

    def get_node_at_line(self, file_path: str, target_line: int) -> str:
        """
        Parses a file and returns the exact AST node (function/class) 
        encompassing the target line provided by the LSP.
        """
        import os
        ext = os.path.splitext(file_path)[1]
        
        if ext not in self.languages:
            return "Error: Unsupported language for AST parsing."

        # 🎯 FIX: Instantiate a new Parser directly with the requested language
        parser = Parser(cast(Language, self.languages[ext]))

        with open(file_path, 'rb') as f:
            source_bytes = f.read()

        tree = parser.parse(source_bytes)
        cursor = tree.walk()
        
        # Traverse down to find the node spanning the target line
        node = self._find_encompassing_node(cursor.node, target_line)
        
        if node:
            # Return the exact source code for this structural block
            return source_bytes[node.start_byte:node.end_byte].decode('utf-8')
        return "Error: Node not found at specified line."

    def _find_encompassing_node(self, current_node, target_line):
        # Tree-sitter lines are 0-indexed
        if current_node.start_point[0] <= target_line <= current_node.end_point[0]:
            # Drill down to the most specific block-level node
            for child in current_node.children:
                result = self._find_encompassing_node(child, target_line)
                if result:
                    return result
            
            # If no child encompasses it strictly better while being a block,
            # return this node if it matches a known structural statement.
            valid_node_types = [
                'function_definition', 
                'class_definition', 
                'impl_item',
                'decorated_definition',
                'import_statement',
                'import_from_statement',
                'expression_statement',
                'assignment'
            ]
            
            if current_node.type in valid_node_types:
                return current_node
                
        return None
