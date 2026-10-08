from fastembed import TextEmbedding
import lancedb
from loguru import logger
from opentelemetry import trace

tracer = trace.get_tracer(__name__)
import os
import pyarrow as pa
import requests
import time
from typing import List, Dict, Any, Optional
from .semantic_chunker import SemanticChunker
from .reranker import get_reranker, detect_optimal_candidate_k



class LocalVectorStore:
    def __init__(self, workspace_root: Optional[str] = None, config: Optional[Dict[str, Any]] = None):
        self.config = config or {}
        # 1. Initialize LanceDB
        self.workspace_root = workspace_root or os.getcwd()
        db_path = os.path.join(self.workspace_root, ".lancedb") 
        self.db = lancedb.connect(db_path)
        self.table_name = "codebase"
        
        schema = pa.schema([
            pa.field("vector", pa.list_(pa.float32(), 384)),
            pa.field("file_path", pa.string()),
            pa.field("file_hash", pa.string()),
            pa.field("content", pa.string())
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
        # We explicitly request the same model to preserve our 384-dimension schema
        self.embedder = TextEmbedding(model_name="sentence-transformers/all-MiniLM-L6-v2")

        # 3. Initialize the Tree-sitter AST Chunker
        logger.info("Initializing Semantic Chunker...")
        self.chunker = SemanticChunker()

    def _initialize_table(self):
        """Creates the LanceDB table with a strict PyArrow schema if it doesn't exist."""
        # nomic-embed-text outputs a 768-dimensional vector
        schema = pa.schema([
            pa.field("vector", pa.list_(pa.float32(), 768)),
            pa.field("file_path", pa.string()),
            pa.field("file_hash", pa.string()),
            pa.field("content", pa.string())
        ])
        
        if self.table_name not in self.db.table_names():
            logger.info(f"📁 Creating new LanceDB table: {self.table_name}")
            return self.db.create_table(self.table_name, schema=schema)
        
        return self.db.open_table(self.table_name)

    def _generate_embedding(self, text: str) -> list[float]:
        # FastEmbed requires a list of strings and returns a generator of NumPy arrays
        embeddings = list(self.embedder.embed([text]))
        emb = embeddings[0]
        # Handle both numpy arrays (production) and standard lists (test mocks)
        return emb.tolist() if hasattr(emb, "tolist") else emb

    # Inside LocalVectorStore class in vector_store.py
    def delete_file(self, file_path: str) -> int:
        """Removes all chunks belonging to a specific file from the LanceDB table.

        Returns 1 on success, 0 on failure.
        """
        try:
            self.table.delete(f"file_path = '{file_path}'")
            logger.info(f"🗑️ Deleted all chunks for {file_path}")
            return 1
        except Exception as e:
            logger.error(f"Failed to delete file '{file_path}' from LanceDB: {e}")
            return 0

    def _get_stored_hash(self, file_path: str) -> Optional[str]:
        """Queries LanceDB for the stored hash of a given file_path.

        Returns the hash if found, or None if the file is not indexed.
        """
        try:
            df = self.table.search().where(f"file_path = '{file_path}'").limit(1).to_df()
            if df is not None and len(df) > 0:
                return df.iloc[0]["file_hash"]
        except Exception as e:
            logger.warning(f"Hash lookup failed for {file_path}: {e}")
        return None

    def upsert_file(self, file_path: str, file_hash: str, content: str):
        # 0. Incremental sync: Check if this exact version is already indexed
        stored_hash = self._get_stored_hash(file_path)
        if stored_hash is not None:
            if stored_hash == file_hash:
                logger.debug(f"⏭️  Skipping {file_path} — hash unchanged ({file_hash[:8]}…)")
                return
            else:
                logger.info(f"🔄 File modified: {file_path} (old {stored_hash[:8]}… → new {file_hash[:8]}…)")
                self.delete_file(file_path)

        # 1. Split the massive file into bite-sized chunks
        text_chunks, chunk_type = self.chunker.chunk_file(file_path, content)

        if not text_chunks:
            return

        data_to_insert = []
        
        # 2. Process each chunk
        for chunk in text_chunks:
            try:
                # Generate the embedding for the specific chunk
                enriched_chunk = f"File: {file_path}\n\n{chunk}"
                vector = self._generate_embedding(enriched_chunk)
                
                # Format the data explicitly to match your PyArrow schema
                data_to_insert.append({
                    "vector": vector,
                    "file_path": file_path,
                    "file_hash": file_hash,
                    "content": chunk
                })
            except Exception as e:
                logger.error(f"Failed to generate embedding for chunk in {file_path}: {e}")
                continue # Skip the broken chunk and move to the next

        # 3. Upsert the batch into LanceDB
        if data_to_insert:
            try:
                # We use mode="append" assuming we want to add new chunks, 
                # or you can use LanceDB's merge capabilities if you need to deduplicate.
                self.table.add(data_to_insert) 
                logger.info(f"✅ Indexed {len(data_to_insert)} {chunk_type} chunks for {file_path}")
            except Exception as e:
                logger.error(f"Failed to insert into LanceDB: {e}")

    def semantic_search(self, query: str, limit: int = 5):
        if not hasattr(self, 'table'):
            if self.table_name in self.db.table_names():
                self.table = self.db.open_table(self.table_name)
            else:
                logger.warning("Table does not exist yet. Please index files first.")
                return []

        with tracer.start_as_current_span("rag_semantic_search") as span:
            span.set_attribute("search.query", query)
            span.set_attribute("search.limit", limit)

            reranker = get_reranker(self.config)
            if reranker:
                rag_config = self.config.get("rag", {}).get("rerank", {})
                candidate_k = detect_optimal_candidate_k(rag_config.get("candidate_pool_k", "auto"))
                candidate_k = max(limit, candidate_k)
            else:
                candidate_k = limit
                
            span.set_attribute("search.candidate_k", candidate_k)

            t0 = time.time()
            query_vector = self._generate_embedding(query)
            t1 = time.time()
            
            results = self.table.search(query_vector).limit(candidate_k).to_list()
            t2 = time.time()
            
            embed_ms = (t1 - t0) * 1000
            db_ms = (t2 - t1) * 1000
            logger.info(f"⏱️  FastEmbed CPU: {embed_ms:.2f}ms | ⚡ LanceDB Search (k={candidate_k}): {db_ms:.2f}ms")
            
            if reranker and results:
                results = reranker.rerank(query, results)
                
            results = results[:limit]
            
            span.set_attribute("latency.embedding_ms", embed_ms)
            span.set_attribute("latency.lancedb_ms", db_ms)
            span.set_attribute("search.results_count", len(results))
            
            for i, res in enumerate(results):
                span.set_attribute(f"result.{i}.file_path", str(res.get("file_path")))
                span.set_attribute(f"result.{i}.distance", float(res.get("_distance", 0.0)))
                if "_rerank_score" in res:
                    span.set_attribute(f"result.{i}.rerank_score", float(res.get("_rerank_score")))
            
            return results

    def reconcile_database(self):
        """Scans the database on startup and drops chunks for files that no longer exist on disk."""
        if not hasattr(self, 'table'):
            return

        try:
            # 1. Get all unique file paths currently in the vector database
            # We use a set to deduplicate paths across thousands of chunks
            df = self.table.search().limit(None).to_df()
            if df is None or df.empty or "file_path" not in df.columns:
                return
                
            indexed_files = set(df["file_path"].tolist())
            
            # 2. Verify existence on the filesystem
            ghost_files = [path for path in indexed_files if not os.path.exists(path)]
            
            # 3. Purge ghost files
            if ghost_files:
                logger.info(f"🧹 [Reconciliation] Found {len(ghost_files)} ghost files. Purging from database...")
                for path in ghost_files:
                    self.delete_file(path)
                    
        except Exception as e:
            logger.error(f"Failed to run database reconciliation sweep: {e}")
