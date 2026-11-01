import os
import re
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Set

from fastembed import TextEmbedding
import lancedb
from loguru import logger
from opentelemetry import trace
import pyarrow as pa

from .reranker import detect_optimal_candidate_k, get_reranker
from .semantic_chunker import SemanticChunker

tracer = trace.get_tracer(__name__)

# Directories that should never be indexed in the vector store
DEFAULT_EXCLUDED_DIRS: Set[str] = {
    ".git",
    ".hg",
    ".svn",
    ".venv",
    "venv",
    "env",
    "__pycache__",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
    ".tox",
    "node_modules",
    "out",
    "dist",
    "build",
    "coverage",
    ".next",
    ".nuxt",
    ".agentic_workspace",
}

# Specific filenames and scratch files that pollute code search
DEFAULT_EXCLUDED_FILES: Set[str] = {
    "changelog.md",
    "package-lock.json",
    "yarn.lock",
    "pnpm-lock.yaml",
    "poetry.lock",
    "pipfile.lock",
    "cargo.lock",
    ".ds_store",
    ".env",
    ".agentic_config.json",
    "secrets.json",
    "appgod.py",
}

# File extensions that are binary, minified, or low-signal for code RAG
DEFAULT_EXCLUDED_EXTENSIONS: Set[str] = {
    ".min.js",
    ".min.css",
    ".map",
    ".lock",
    ".log",
    ".db",
    ".sqlite",
    ".sqlite3",
    ".pyc",
    ".pyo",
    ".so",
    ".dylib",
    ".dll",
    ".exe",
    ".bin",
    ".png",
    ".jpg",
    ".jpeg",
    ".gif",
    ".svg",
    ".ico",
    ".pdf",
    ".zip",
    ".tar",
    ".gz",
    ".vsix",
}


class LocalVectorStore:
    def __init__(self, workspace_root: Optional[str] = None, config: Optional[Dict[str, Any]] = None):
        self.config = config or {}
        self.workspace_root = os.path.abspath(workspace_root or os.getcwd())

        # Allow custom exclusions from config while keeping safe defaults
        rag_cfg = self.config.get("rag", {})
        self.excluded_dirs = DEFAULT_EXCLUDED_DIRS | set(rag_cfg.get("excluded_dirs", []))
        self.excluded_files = DEFAULT_EXCLUDED_FILES | {
            f.lower() for f in rag_cfg.get("excluded_files", [])
        }
        self.excluded_extensions = DEFAULT_EXCLUDED_EXTENSIONS | {
            ext.lower() for ext in rag_cfg.get("excluded_extensions", [])
        }

        # 1. Initialize LanceDB
        lancedb_path = os.path.join(self.workspace_root, ".agentic_workspace", "vector_store")
        self.db = lancedb.connect(lancedb_path)
        self.table_name = "codebase"

        schema = pa.schema([
            pa.field("vector", pa.list_(pa.float32(), 384)),
            pa.field("file_path", pa.string()),
            pa.field("file_hash", pa.string()),
            pa.field("content", pa.string()),
        ])

        if self.table_name in self.db.table_names():
            self.table = self.db.open_table(self.table_name)
            if self.table.schema.field("vector").type.list_size != 384:
                self.db.drop_table(self.table_name)
                self.table = self.db.create_table(self.table_name, schema=schema)
        else:
            self.table = self.db.create_table(self.table_name, schema=schema)

        # 2. Load FastEmbed (ONNX CPU Runtime)
        logger.info("Loading FastEmbed ONNX model...")
        self.embedder = TextEmbedding(model_name="sentence-transformers/all-MiniLM-L6-v2")

        # 3. Initialize the Tree-sitter AST Chunker
        logger.info("Initializing Semantic Chunker...")
        self.chunker = SemanticChunker()

    @staticmethod
    def _escape_sql_string(val: str) -> str:
        """Escapes single quotes for safe LanceDB SQL predicate expressions."""
        return val.replace("'", "''")

    def should_index_file(self, file_path: str) -> bool:
        """Determines whether a file path should be indexed or excluded as noise."""
        if not file_path:
            return False

        norm_path = os.path.normpath(file_path)
        path_obj = Path(norm_path)
        filename_lower = path_obj.name.lower()

        # 1. Block hidden dotfiles (e.g., .env, .agentic_config.json, .gitignore)
        if filename_lower.startswith(".") or filename_lower in self.excluded_files:
            return False

        # 2. Block excluded extensions (including compound extensions like .min.js)
        for ext in self.excluded_extensions:
            if filename_lower.endswith(ext):
                return False

        # 3. Block files inside excluded or hidden directories
        try:
            rel_path = os.path.relpath(norm_path, self.workspace_root)
            parts = Path(rel_path).parts if not rel_path.startswith("..") else path_obj.parts
        except ValueError:
            parts = path_obj.parts

        for part in parts[:-1]:
            part_lower = part.lower()
            if part_lower in self.excluded_dirs or (part_lower.startswith(".") and part_lower not in {".", ".."}):
                return False

        return True

    def _generate_embedding(self, text: str) -> List[float]:
        """Generates a single embedding vector."""
        embeddings = list(self.embedder.embed([text]))
        emb = embeddings[0]
        return emb.tolist() if hasattr(emb, "tolist") else list(emb)

    def _generate_embeddings_batch(self, texts: List[str]) -> List[List[float]]:
        """Generates embeddings for a batch of chunks in a single ONNX pass."""
        embeddings = list(self.embedder.embed(texts))
        return [emb.tolist() if hasattr(emb, "tolist") else list(emb) for emb in embeddings]

    def delete_file(self, file_path: str) -> int:
        """Removes all chunks belonging to a specific file from the LanceDB table.

        Returns 1 on success, 0 on failure.
        """
        try:
            safe_path = self._escape_sql_string(file_path)
            self.table.delete(f"file_path = '{safe_path}'")
            logger.info(f"🗑️ Deleted all chunks for {file_path}")
            return 1
        except Exception as e:
            logger.error(f"Failed to delete file '{file_path}' from LanceDB: {e}")
            return 0

    def _get_stored_hash(self, file_path: str) -> Optional[str]:
        """Queries LanceDB for the stored hash of a given file_path."""
        try:
            safe_path = self._escape_sql_string(file_path)
            df = self.table.search().where(f"file_path = '{safe_path}'").limit(1).to_df()
            if df is not None and len(df) > 0:
                return df.iloc[0]["file_hash"]
        except Exception as e:
            logger.warning(f"Hash lookup failed for {file_path}: {e}")
        return None

    def upsert_file(self, file_path: str, file_hash: str, content: str):
        # 0. Filter out excluded noise files (and purge if previously indexed)
        if not self.should_index_file(file_path):
            logger.debug(f"🚫 Skipping excluded file from vector index: {file_path}")
            if self._get_stored_hash(file_path) is not None:
                self.delete_file(file_path)
            return

        # 1. Incremental sync: Check if this exact version is already indexed
        stored_hash = self._get_stored_hash(file_path)
        if stored_hash is not None:
            if stored_hash == file_hash:
                logger.debug(f"⏭️  Skipping {file_path} — hash unchanged ({file_hash[:8]}…)")
                return
            else:
                logger.info(f"🔄 File modified: {file_path} (old {stored_hash[:8]}… → new {file_hash[:8]}…)")
                self.delete_file(file_path)

        # 2. Split the file into semantic AST chunks
        text_chunks, chunk_type = self.chunker.chunk_file(file_path, content)
        if not text_chunks:
            return

        try:
            rel_path = os.path.relpath(file_path, self.workspace_root)
        except ValueError:
            rel_path = file_path

        enriched_chunks = [f"File: {rel_path}\n\n{chunk}" for chunk in text_chunks]

        # 3. Batch-embed all chunks at once (fallback to per-chunk on mock/error)
        data_to_insert = []
        try:
            vectors = self._generate_embeddings_batch(enriched_chunks)
            for chunk, vector in zip(text_chunks, vectors):
                data_to_insert.append({
                    "vector": vector,
                    "file_path": file_path,
                    "file_hash": file_hash,
                    "content": chunk,
                })
        except Exception:
            for chunk, enriched in zip(text_chunks, enriched_chunks):
                try:
                    vector = self._generate_embedding(enriched)
                    data_to_insert.append({
                        "vector": vector,
                        "file_path": file_path,
                        "file_hash": file_hash,
                        "content": chunk,
                    })
                except Exception as e:
                    logger.error(f"Failed to generate embedding for chunk in {file_path}: {e}")
                    continue

        # 4. Upsert the batch into LanceDB
        if data_to_insert:
            try:
                self.table.add(data_to_insert)
                logger.info(f"✅ Indexed {len(data_to_insert)} {chunk_type} chunks for {file_path}")
            except Exception as e:
                logger.error(f"Failed to insert into LanceDB: {e}")

    @staticmethod
    def _extract_code_identifiers(query: str) -> List[str]:
        """Extracts snake_case, camelCase, or PascalCase identifiers from a query for lexical boosting."""
        tokens = re.findall(r"[a-zA-Z_][a-zA-Z0-9_]{2,}", query)
        identifiers = []
        for t in tokens:
            if "_" in t or (any(c.isupper() for c in t[1:]) and any(c.islower() for c in t)):
                identifiers.append(t)
        return identifiers

    @staticmethod
    def _compute_adjustment_score(
        query: str, file_path: str, content: str, identifiers: List[str]
    ) -> float:
        """
        Computes a tie-breaker/priority boost so primary source code and exact identifier
        hits rank above docs, tests, and changelogs.
        """
        bonus = 0.0
        path_lower = file_path.lower()
        query_lower = query.lower()

        # 1. Exact query substring or code identifier match in chunk content
        if len(query_lower) >= 4 and query_lower in content.lower():
            bonus += 0.25
        for ident in identifiers:
            if ident in content:
                bonus += 0.30
            elif ident.lower() in content.lower():
                bonus += 0.15

        # 2. Demote tests and docs unless the query explicitly asks for them
        wants_tests = any(w in query_lower for w in ("test", "pytest", "vitest", "spec", "mock"))
        wants_docs = any(w in query_lower for w in ("doc", "plan", "readme", "markdown", "architecture"))

        if not wants_tests and (
            "/tests/" in path_lower
            or "/test/" in path_lower
            or ".test." in path_lower
            or ".spec." in path_lower
            or path_lower.endswith("_test.py")
            or os.path.basename(path_lower).startswith("test_")
        ):
            bonus -= 0.20

        if not wants_docs and (path_lower.endswith(".md") or "/docs/" in path_lower):
            bonus -= 0.25

        return bonus

    def semantic_search(
        self,
        query: str,
        limit: int = 5,
        path_prefix: Optional[str] = None,
        max_chunks_per_file: int = 2,
    ) -> List[Dict[str, Any]]:
        if not hasattr(self, "table"):
            if self.table_name in self.db.table_names():
                self.table = self.db.open_table(self.table_name)
            else:
                logger.warning("Table does not exist yet. Please index files first.")
                return []

        with tracer.start_as_current_span("rag_semantic_search") as span:
            span.set_attribute("search.query", query)
            span.set_attribute("search.limit", limit)
            if path_prefix:
                span.set_attribute("search.path_prefix", path_prefix)

            reranker = get_reranker(self.config)
            if reranker:
                rag_config = self.config.get("rag", {}).get("rerank", {})
                candidate_k = detect_optimal_candidate_k(rag_config.get("candidate_pool_k", "auto"))
                candidate_k = max(limit * 3, candidate_k)
            else:
                candidate_k = max(limit * 3, 15)

            span.set_attribute("search.candidate_k", candidate_k)

            t0 = time.time()
            query_vector = self._generate_embedding(query)
            t1 = time.time()

            search_builder = self.table.search(query_vector)
            if path_prefix:
                norm_prefix = os.path.normpath(path_prefix).strip("/")
                safe_prefix = self._escape_sql_string(norm_prefix)
                search_builder = search_builder.where(f"file_path LIKE '%{safe_prefix}%'")

            results = search_builder.limit(candidate_k).to_list()

            # Hybrid Lexical Injection: If the query contains a code identifier (e.g. `reset_session`),
            # ensure chunks containing that exact identifier are in the candidate pool even if dense search missed them.
            identifiers = self._extract_code_identifiers(query)
            if identifiers:
                existing_keys = {(r.get("file_path"), r.get("content")) for r in results}
                for ident in identifiers[:2]:
                    safe_ident = self._escape_sql_string(ident)
                    where_clause = f"content LIKE '%{safe_ident}%'"
                    if path_prefix:
                        norm_prefix = self._escape_sql_string(os.path.normpath(path_prefix).strip("/"))
                        where_clause += f" AND file_path LIKE '%{norm_prefix}%'"
                    try:
                        lexical_hits = self.table.search().where(where_clause).limit(5).to_list()
                        for hit in lexical_hits:
                            key = (hit.get("file_path"), hit.get("content"))
                            if key not in existing_keys:
                                hit["_distance"] = hit.get("_distance", 0.25)
                                results.append(hit)
                                existing_keys.add(key)
                    except Exception:
                        pass

            t2 = time.time()

            # Filter out any legacy noise files that haven't been reconciled yet
            results = [r for r in results if self.should_index_file(str(r.get("file_path", "")))]

            embed_ms = (t1 - t0) * 1000
            db_ms = (t2 - t1) * 1000
            logger.info(f"⏱️  FastEmbed CPU: {embed_ms:.2f}ms | ⚡ LanceDB Search (k={candidate_k}): {db_ms:.2f}ms")

            if reranker and results:
                # Temporarily prepend the relative file path so the cross-encoder knows which file a chunk belongs to
                original_contents = []
                for r in results:
                    orig = str(r.get("content", ""))
                    original_contents.append((r, orig))
                    fp = str(r.get("file_path", ""))
                    try:
                        rel_fp = os.path.relpath(fp, self.workspace_root)
                    except ValueError:
                        rel_fp = fp
                    r["content"] = f"File: {rel_fp}\n{orig}"

                results = reranker.rerank(query, results)

                # Restore clean chunk content after reranking
                for r, orig in original_contents:
                    r["content"] = orig

            # Apply source-code / exact-identifier priority adjustment
            for r in results:
                adj = self._compute_adjustment_score(
                    query,
                    str(r.get("file_path", "")),
                    str(r.get("content", "")),
                    identifiers,
                )
                if "_rerank_score" in r:
                    r["_effective_score"] = float(r["_rerank_score"]) + adj
                else:
                    # Lower _distance is better, so subtract adjustment from distance
                    r["_effective_score"] = -float(r.get("_distance", 1.0)) + adj

            results.sort(key=lambda x: x.get("_effective_score", 0.0), reverse=True)

            # Enforce per-file diversity cap so a single file doesn't monopolize top-k
            diversified_results = []
            file_counts: Dict[str, int] = {}
            for r in results:
                fp = str(r.get("file_path", ""))
                if file_counts.get(fp, 0) >= max_chunks_per_file:
                    continue
                file_counts[fp] = file_counts.get(fp, 0) + 1
                r.pop("_effective_score", None)
                diversified_results.append(r)
                if len(diversified_results) >= limit:
                    break

            span.set_attribute("latency.embedding_ms", embed_ms)
            span.set_attribute("latency.lancedb_ms", db_ms)
            span.set_attribute("search.results_count", len(diversified_results))

            for i, res in enumerate(diversified_results):
                span.set_attribute(f"result.{i}.file_path", str(res.get("file_path")))
                span.set_attribute(f"result.{i}.distance", float(res.get("_distance", 0.0)))
                if "_rerank_score" in res:
                    span.set_attribute(f"result.{i}.rerank_score", float(res.get("_rerank_score")))

            return diversified_results

    def reconcile_database(self):
        """Scans the database on startup and drops chunks for deleted or newly-excluded files."""
        if not hasattr(self, "table"):
            return

        try:
            df = self.table.search().limit(None).to_df()
            if df is None or df.empty or "file_path" not in df.columns:
                return

            indexed_files = set(df["file_path"].tolist())

            # Purge both deleted files AND excluded noise files (e.g., CHANGELOG.md, .agentic_config.json, appgod.py)
            stale_or_excluded = [
                path
                for path in indexed_files
                if not os.path.exists(path) or not self.should_index_file(path)
            ]

            if stale_or_excluded:
                logger.info(
                    f"🧹 [Reconciliation] Found {len(stale_or_excluded)} ghost or excluded noise files. Purging from database..."
                )
                for path in stale_or_excluded:
                    self.delete_file(path)

        except Exception as e:
            logger.error(f"Failed to run database reconciliation sweep: {e}")