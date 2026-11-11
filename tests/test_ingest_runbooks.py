"""The ingest script checks settings and the embeddings API before it clears the store."""

from pathlib import Path
from unittest.mock import MagicMock

import pytest
from langchain_core.documents import Document

from scripts import ingest_runbooks


@pytest.fixture
def store_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A store directory holding one old file, standing in for the live index."""
    directory = tmp_path / "chroma"
    directory.mkdir()
    (directory / "chroma.sqlite3").write_text("old index")
    monkeypatch.setattr(ingest_runbooks, "CHROMA_PERSIST_DIR", directory)
    monkeypatch.setattr(ingest_runbooks, "load_all_documents", lambda: [Document(page_content="runbook text")])
    return directory


def test_settings_error_leaves_store_in_place(store_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def broken_settings() -> MagicMock:
        raise ValueError("ANTHROPIC_API_KEY is required when LLM_PROVIDER=anthropic")

    build = MagicMock()
    monkeypatch.setattr(ingest_runbooks, "get_embeddings", broken_settings)
    monkeypatch.setattr(ingest_runbooks, "build_vector_store", build)

    with pytest.raises(SystemExit):
        ingest_runbooks.main()

    assert (store_dir / "chroma.sqlite3").read_text() == "old index"
    build.assert_not_called()


def test_embedding_probe_failure_leaves_store_in_place(store_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    embeddings = MagicMock()
    embeddings.embed_query.side_effect = RuntimeError("401 invalid api key")
    build = MagicMock()
    monkeypatch.setattr(ingest_runbooks, "get_embeddings", lambda: embeddings)
    monkeypatch.setattr(ingest_runbooks, "build_vector_store", build)

    with pytest.raises(SystemExit):
        ingest_runbooks.main()

    assert (store_dir / "chroma.sqlite3").read_text() == "old index"
    build.assert_not_called()


def test_success_clears_then_builds(store_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    seen_at_build: list[bool] = []

    def build(documents: list[Document]) -> MagicMock:
        seen_at_build.append((store_dir / "chroma.sqlite3").exists())
        store = MagicMock()
        store._collection.count.return_value = len(documents)
        return store

    monkeypatch.setattr(ingest_runbooks, "get_embeddings", MagicMock)
    monkeypatch.setattr(ingest_runbooks, "build_vector_store", build)

    ingest_runbooks.main()

    assert seen_at_build == [False]
