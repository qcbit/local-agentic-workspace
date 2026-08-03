import os
import logging
from typing import List
from tree_sitter import Language, Parser
import tree_sitter_python as tspython
import tree_sitter_typescript as tstypescript

logger = logging.getLogger(__name__)

class SemanticChunker:
    def __init__(self):
        # Initialize the parsers with the pre-compiled language grammars
        self.parsers = {
            ".py": self._setup_parser(tspython.language()),
            ".ts": self._setup_parser(tstypescript.language_typescript()),
            ".tsx": self._setup_parser(tstypescript.language_tsx()),
        }

        # Define which AST node types represent complete logical blocks
        self.chunkable_nodes = {
            ".py": {"function_definition", "class_definition"},
            ".ts": {"function_declaration", "class_declaration", "method_definition", "interface_declaration", "type_alias_declaration"},
            ".tsx": {"function_declaration", "class_declaration", "method_definition", "interface_declaration", "type_alias_declaration"}
        }

    def _setup_parser(self, language_ptr) -> Parser:
        """Helper to instantiate a Tree-sitter parser for a specific language."""
        lang = Language(language_ptr)
        return Parser(lang)

    def chunk_file(self, file_path: str, content: str) -> List[str]:
        """Parses a file and returns semantic chunks. Falls back to basic chunking if unsupported."""
        ext = os.path.splitext(file_path)[1].lower()
        
        # If the file type isn't supported by our Tree-sitter setup, fallback to naive chunking
        if ext not in self.parsers:
            return self._naive_chunking(content)

        try:
            parser = self.parsers[ext]
            source_bytes = content.encode("utf8")
            tree = parser.parse(source_bytes)
            
            chunks = self._extract_nodes(tree.root_node, source_bytes, self.chunkable_nodes[ext])
            
            # If the file had no functions/classes, wrap the whole file as a single chunk
            if not chunks and content.strip():
                return [content.strip()]
                
            return chunks
            
        except Exception as e:
            logger.error(f"Tree-sitter failed to parse {file_path}: {e}")
            return self._naive_chunking(content)

    def _extract_nodes(self, node, source_bytes: bytes, target_types: set) -> List[str]:
        """Recursively traverses the AST to extract target nodes as strings."""
        chunks = []
        
        if node.type in target_types:
            # We found a complete logical block. Extract it.
            chunk_text = source_bytes[node.start_byte:node.end_byte].decode("utf8")
            chunks.append(chunk_text)
            
            # Do not traverse children of this node, so we don't get nested duplicate chunks 
            # (e.g., extracting a class, and then extracting its methods again as separate chunks).
            return chunks

        # Continue searching down the tree
        for child in node.children:
            chunks.extend(self._extract_nodes(child, source_bytes, target_types))
            
        return chunks

    def _naive_chunking(self, content: str, chunk_size: int = 1500, overlap: int = 200) -> List[str]:
        """A simple character-count fallback chunker for unsupported files like .md or .json."""
        chunks = []
        i = 0
        while i < len(content):
            chunks.append(content[i:i + chunk_size])
            i += chunk_size - overlap
        return chunks
