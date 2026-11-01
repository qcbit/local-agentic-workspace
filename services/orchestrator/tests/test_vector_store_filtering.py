import os
from pathlib import Path
from unittest.mock import MagicMock, patch
import pytest

from rag.vector_store import LocalVectorStore


class FakeEmbedder:
    """Deterministic 384-dim mock embedder to test LanceDB queries without ONNX overhead."""

    def embed(self, texts):
        for _ in texts:
            yield [0.1] * 384


@pytest.fixture
def vector_store(tmp_path: Path):
    """Initializes LocalVectorStore backed by a real LanceDB table in tmp_path with mocked ONNX models."""
    with (
        patch("rag.vector_store.TextEmbedding", return_value=FakeEmbedder()),
        patch("rag.vector_store.SemanticChunker") as MockChunker,
        patch("rag.vector_store.get_reranker", return_value=None),
    ):
        chunker_instance = MagicMock()
        # Default behavior: treat each paragraph separated by double-newline as a chunk
        chunker_instance.chunk_file.side_effect = lambda fp, content: (
            [c.strip() for c in content.split("\n\n") if c.strip()],
            "ast",
        )
        MockChunker.return_value = chunker_instance

        store = LocalVectorStore(workspace_root=str(tmp_path), config={})
        yield store


def test_should_index_file_filters_noise_and_allows_source(vector_store: LocalVectorStore, tmp_path: Path):
    """Verifies noise files from the failure log are blocked while valid source files pass."""
    # Blocked files & directories
    assert not vector_store.should_index_file(str(tmp_path / "apps/vscode-extension/CHANGELOG.md"))
    assert not vector_store.should_index_file(str(tmp_path / "apps/vscode-extension/.agentic_config.json"))
    assert not vector_store.should_index_file(str(tmp_path / "apps/tui/appgod.py"))
    assert not vector_store.should_index_file(str(tmp_path / "apps/vscode-extension/node_modules/pkg/index.js"))
    assert not vector_store.should_index_file(str(tmp_path / "apps/vscode-extension/out/chat.js"))
    assert not vector_store.should_index_file(str(tmp_path / ".venv/lib/site.py"))
    assert not vector_store.should_index_file(str(tmp_path / "bundle.min.js"))

    # Allowed source files
    assert vector_store.should_index_file(
        str(tmp_path / "apps/vscode-extension/src/webview/components/ChatPanel.tsx")
    )
    assert vector_store.should_index_file(
        str(tmp_path / "services/orchestrator/src/ipc/uds_server.py")
    )


def test_reconcile_database_purges_deleted_and_newly_excluded_files(
    vector_store: LocalVectorStore, tmp_path: Path
):
    """Verifies reconcile_database removes both deleted files and legacy noise files like CHANGELOG.md."""
    valid_file = tmp_path / "src" / "main.py"
    valid_file.parent.mkdir(parents=True, exist_ok=True)
    valid_file.write_text("def main(): pass")

    deleted_file = tmp_path / "src" / "ghost.py"
    noise_changelog = tmp_path / "CHANGELOG.md"
    noise_changelog.write_text("### Features\n* Implement new chat")

    # Manually seed LanceDB with a valid file, a deleted file, and a legacy noise file
    vector_store.table.add([
        {"vector": [0.1] * 384, "file_path": str(valid_file), "file_hash": "h1", "content": "def main(): pass"},
        {"vector": [0.1] * 384, "file_path": str(deleted_file), "file_hash": "h2", "content": "def ghost(): pass"},
        {"vector": [0.1] * 384, "file_path": str(noise_changelog), "file_hash": "h3", "content": "Implement new chat"},
    ])

    vector_store.reconcile_database()

    remaining_df = vector_store.table.search().limit(None).to_df()
    remaining_paths = set(remaining_df["file_path"].tolist())

    assert remaining_paths == {str(valid_file)}


def test_semantic_search_identifier_boost_and_test_doc_demotion(
    vector_store: LocalVectorStore, tmp_path: Path
):
    """Verifies exact identifier hits in source code rank above docs and tests."""
    doc_file = str(tmp_path / "services/orchestrator/docs/sqlite-message-persistence-plan.md")
    test_file = str(tmp_path / "services/orchestrator/tests/test_archival.py")
    src_file = str(tmp_path / "services/orchestrator/src/ipc/uds_server.py")

    vector_store.upsert_file(doc_file, "h_doc", "Architecture plan for message persistence")
    vector_store.upsert_file(test_file, "h_test", "async def test_archive_inactive_sessions(): pass")
    vector_store.upsert_file(
        src_file,
        "h_src",
        'elif method == "reset_session":\n    self.persistent_agent = None',
    )

    with patch("rag.vector_store.get_reranker", return_value=None):
        results = vector_store.semantic_search("reset_session", limit=3)

    assert len(results) == 3
    # Source file containing the exact `reset_session` identifier must rank #1
    assert results[0]["file_path"] == src_file


def test_semantic_search_path_prefix_and_per_file_diversity_cap(
    vector_store: LocalVectorStore, tmp_path: Path
):
    """Verifies max_chunks_per_file prevents one large file from monopolizing results, and path_prefix scopes search."""
    tui_file = str(tmp_path / "apps/tui/app.py")
    ext_file = str(tmp_path / "apps/vscode-extension/src/webview/components/ChatPanel.tsx")

    # Insert 5 chunks for tui_file and 1 chunk for ext_file
    tui_content = "\n\n".join([f"def tui_chunk_{i}(): pass" for i in range(5)])
    ext_content = "const handleNewChat = () => { vscode.postMessage({ type: 'reset_session' }); };"

    vector_store.upsert_file(tui_file, "h_tui", tui_content)
    vector_store.upsert_file(ext_file, "h_ext", ext_content)

    with patch("rag.vector_store.get_reranker", return_value=None):
        # 1. Test diversity cap (max_chunks_per_file=2)
        all_results = vector_store.semantic_search("New Chat", limit=5, max_chunks_per_file=2)
        tui_hits = [r for r in all_results if r["file_path"] == tui_file]
        ext_hits = [r for r in all_results if r["file_path"] == ext_file]

        assert len(tui_hits) <= 2, "Expected at most 2 chunks from apps/tui/app.py"
        assert len(ext_hits) == 1, "Expected ChatPanel.tsx to appear in top results"

        # 2. Test path_prefix scoping
        scoped_results = vector_store.semantic_search(
            "New Chat", limit=5, path_prefix="apps/vscode-extension"
        )
        assert len(scoped_results) == 1
        assert scoped_results[0]["file_path"] == ext_file