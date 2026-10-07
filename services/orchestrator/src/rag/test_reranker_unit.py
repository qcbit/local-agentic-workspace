import pytest
from unittest.mock import MagicMock, patch
from services.orchestrator.src.rag.reranker import (
    Reranker,
    detect_optimal_candidate_k,
    get_reranker,
)


def test_detect_optimal_candidate_k_explicit():
    assert detect_optimal_candidate_k(12) == 12
    assert detect_optimal_candidate_k("20") == 20


def test_detect_optimal_candidate_k_auto():
    k = detect_optimal_candidate_k("auto")
    assert k in (8, 15, 25, 30)


def test_reranker_sorts_descending():
    reranker = Reranker()
    mock_model = MagicMock()
    # Return scores for 3 candidates
    mock_model.rerank.return_value = [0.1, 0.95, 0.45]
    reranker._model = mock_model

    candidates = [
        {"file_path": "a.py", "content": "func a()"},
        {"file_path": "b.py", "content": "func b()"},
        {"file_path": "c.py", "content": "func c()"},
    ]

    results = reranker.rerank("find b", candidates)

    assert len(results) == 3
    assert results[0]["file_path"] == "b.py"
    assert results[0]["_rerank_score"] == 0.95
    assert results[1]["file_path"] == "c.py"
    assert results[1]["_rerank_score"] == 0.45
    assert results[2]["file_path"] == "a.py"
    assert results[2]["_rerank_score"] == 0.1


def test_reranker_fallback_on_model_load_failure():
    reranker = Reranker(model_name="non-existent-model")
    reranker._load_error = True

    candidates = [
        {"file_path": "first.py", "content": "1"},
        {"file_path": "second.py", "content": "2"},
    ]

    results = reranker.rerank("query", candidates)
    # Order preserved, no exception raised
    assert results == candidates


@pytest.mark.asyncio
async def test_reranker_async_timeout_fallback():
    reranker = Reranker(max_latency_ms=10.0)

    # Simulate long-running inference
    def slow_rerank(*args, **kwargs):
        import time
        time.sleep(0.05)
        return []

    with patch.object(reranker, "rerank", side_effect=slow_rerank):
        candidates = [{"file_path": "fallback.py", "content": "fast"}]
        results = await reranker.rerank_async("query", candidates, timeout=0.01)
        assert results == candidates


def test_get_reranker_disabled_config():
    config = {"rag": {"rerank": {"enabled": False}}}
    assert get_reranker(config) is None

def test_get_reranker_enabled_config():
    config = {"rag": {"rerank": {"enabled": True}}}
    reranker = get_reranker(config)
    assert reranker is not None
    assert isinstance(reranker, Reranker)