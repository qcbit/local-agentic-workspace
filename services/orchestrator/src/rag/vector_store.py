import os
import lancedb
import pyarrow as pa
import requests
import logging
from typing import List, Dict, Any

logger = logging.getLogger(__name__)

class LocalVectorStore:
    def __init__(self, db_path: str = ".lancedb", table_name: str = "codebase"):
        self.db_path = db_path
        self.table_name = table_name
        self.embed_model = "nomic-embed-text"
        self.ollama_url = "http://127.0.0.1:11434/api/embeddings"
        
        # Connect to the embedded LanceDB instance
        self.db = lancedb.connect(self.db_path)
        self.table = self._initialize_table()

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

    def _generate_embedding(self, text: str) -> List[float]:
        """Calls the local Ollama API to generate a vector embedding."""
        try:
            response = requests.post(self.ollama_url, json={
                "model": self.embed_model,
                "prompt": text
            })
            response.raise_for_status()
            return response.json()["embedding"]
        except Exception as e:
            logger.error(f"Failed to generate embedding: {e}")
            return []

    def upsert_file(self, file_path: str, file_hash: str, chunks: List[str]):
        """Removes old chunks for a file and inserts the updated ones."""
        # 1. Delete existing vectors for this specific file to prevent duplication
        self.table.delete(f"file_path = '{file_path}'")
        
        if not chunks:
            return

        # 2. Generate new embeddings and structure the data
        data = []
        for chunk in chunks:
            vector = self._generate_embedding(chunk)
            if vector:
                data.append({
                    "vector": vector,
                    "file_path": file_path,
                    "file_hash": file_hash,
                    "content": chunk
                })
        
        # 3. Insert into LanceDB
        if data:
            self.table.add(data)
            logger.info(f"✅ Indexed {len(data)} chunks for {file_path}")

    def semantic_search(self, query: str, limit: int = 5) -> List[Dict[str, Any]]:
        """Embeds the query and performs a vector similarity search."""
        query_vector = self._generate_embedding(query)
        if not query_vector:
            return []
            
        results = self.table.search(query_vector).limit(limit).to_list()
        return results
