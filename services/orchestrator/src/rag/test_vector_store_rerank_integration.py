import pytest
import os
from unittest.mock import MagicMock, patch
from services.orchestrator.src.rag.vector_store import LocalVectorStore

@pytest.fixture
def mock_db():
    with patch('lancedb.connect') as mock_connect:
        yield mock_connect

def test_semantic_search_reranks_results():
    config = {
        "rag": {
            "rerank": {
                "enabled": True,
                "candidate_pool_k": 10
            }
        }
    }
    
    # Setup mock VectorStore
    with patch('services.orchestrator.src.rag.vector_store.TextEmbedding') as MockEmbedder:
        mock_embedder_instance = MockEmbedder.return_value
        mock_embedder_instance.embed.return_value = [[0.1] * 384]
        
        vs = LocalVectorStore(workspace_root="/tmp", config=config)
        
        # Mock the table and its search chain
        mock_table = MagicMock()
        mock_search = MagicMock()
        mock_limit = MagicMock()
        
        # Fake L2 distance results (LanceDB returns lower distances first)
        fake_results = [
            {"file_path": "c.py", "content": "class C", "_distance": 0.1},
            {"file_path": "b.py", "content": "class B", "_distance": 0.2},
            {"file_path": "a.py", "content": "class A", "_distance": 0.3},
        ]
        
        vs.table = mock_table
        mock_table.search.return_value = mock_search
        mock_search.limit.return_value = mock_limit
        mock_limit.to_list.return_value = fake_results
        
        # Mock Reranker
        with patch('services.orchestrator.src.rag.vector_store.get_reranker') as mock_get_reranker:
            mock_reranker = MagicMock()
            mock_get_reranker.return_value = mock_reranker
            
            # Reverse the order via reranker
            mock_reranker.rerank.return_value = [
                {"file_path": "a.py", "content": "class A", "_rerank_score": 0.9},
                {"file_path": "b.py", "content": "class B", "_rerank_score": 0.8},
                {"file_path": "c.py", "content": "class C", "_rerank_score": 0.7},
            ]
            
            # Execute search
            results = vs.semantic_search("query", limit=2)
            
            # Assertions
            assert len(results) == 2
            assert results[0]["file_path"] == "a.py"
            assert results[1]["file_path"] == "b.py"
            mock_limit.to_list.assert_called_once()
            
            # verify candidate_k was passed to LanceDB
            mock_search.limit.assert_called_with(10)
            mock_reranker.rerank.assert_called_once_with("query", fake_results)
