from fastembed import TextEmbedding
import lancedb
import logging
import os
import pyarrow as pa
import requests
import time
from typing import List, Dict, Any

logger = logging.getLogger(__name__)


class LocalVectorStore:
    def __init__(self):
        # 1. Initialize LanceDB
        db_path = os.path.join(os.getcwd(), ".lancedb") 
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

    
    def _chunk_text(self, text: str, chunk_size: int = 1200, chunk_overlap: int = 200) -> list[str]:
        """Splits text into overlapping chunks to fit within LLM context limits."""
        if not text:
            return []
        
        chunks = []
        start = 0
        text_length = len(text)
        
        while start < text_length:
            end = start + chunk_size
            chunks.append(text[start:end])
            # Step forward by chunk_size, minus the overlap to preserve context between chunks
            start += chunk_size - chunk_overlap 
            
        return chunks

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
            return embeddings[0].tolist()

    # Inside LocalVectorStore class in vector_store.py
    def upsert_file(self, file_path: str, file_hash: str, content: str):
        # 1. Split the massive file into bite-sized chunks
        text_chunks = self._chunk_text(content)
        
        if not text_chunks:
            return

        data_to_insert = []
        
        # 2. Process each chunk
        for chunk in text_chunks:
            try:
                # Generate the embedding for the specific chunk
                vector = self._generate_embedding(chunk)
                
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
                logger.info(f"✅ Indexed {len(data_to_insert)} chunks for {file_path}")
            except Exception as e:
                logger.error(f"Failed to insert into LanceDB: {e}")

    def semantic_search(self, query: str, limit: int = 5):
        if not hasattr(self, 'table'):
            if self.table_name in self.db.table_names():
                self.table = self.db.open_table(self.table_name)
            else:
                logger.warning("Table does not exist yet. Please index files first.")
                return []

        t0 = time.time()
        query_vector = self._generate_embedding(query)
        t1 = time.time()
        
        results = self.table.search(query_vector).limit(limit).to_list()
        t2 = time.time()
        
        embed_ms = (t1 - t0) * 1000
        db_ms = (t2 - t1) * 1000
        logger.info(f"⏱️  FastEmbed CPU: {embed_ms:.2f}ms | ⚡ LanceDB Search: {db_ms:.2f}ms")
        
        return results
