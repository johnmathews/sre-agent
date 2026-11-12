"""The ingest script: runs without an LLM key, leaves the live index on failure, and --wipe clears the store."""

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from scripts import ingest_runbooks
from src.agent.retrieval.index import IndexBuildError, Manifest

MANIFEST = Manifest(
    fingerprint="f" * 64,
    collection="runbooks-ffffffffffff-1234abcd",
    chunks=3,
    files=1,
    built_at=0.0,
    built_by="ingest",
)


@pytest.fixture
def store_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A store directory holding one old file, standing in for the live index."""
    directory = tmp_path / "chroma"
    directory.mkdir()
    (directory / "chroma.sqlite3").write_text("old index")
    monkeypatch.setattr(ingest_runbooks, "CHROMA_PERSIST_DIR", directory)
    return directory


def test_build_failure_exits_and_leaves_store_in_place(store_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    rebuild = MagicMock(side_effect=IndexBuildError("Embeddings preflight failed: 401 invalid api key"))
    monkeypatch.setattr(ingest_runbooks, "rebuild_index", rebuild)

    with pytest.raises(SystemExit) as exit_info:
        ingest_runbooks.main([])

    assert exit_info.value.code == 1
    assert (store_dir / "chroma.sqlite3").read_text() == "old index"


def test_success_rebuilds_without_wiping(store_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    rebuild = MagicMock(return_value=MANIFEST)
    monkeypatch.setattr(ingest_runbooks, "rebuild_index", rebuild)

    ingest_runbooks.main([])

    rebuild.assert_called_once_with("ingest")
    assert (store_dir / "chroma.sqlite3").exists()


def test_wipe_clears_the_store_first(store_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (store_dir / "segment").mkdir()
    seen_at_build: list[list[str]] = []

    def rebuild(built_by: str) -> Manifest:
        seen_at_build.append(sorted(p.name for p in store_dir.iterdir()))
        return MANIFEST

    monkeypatch.setattr(ingest_runbooks, "rebuild_index", rebuild)

    ingest_runbooks.main(["--wipe"])

    assert seen_at_build == [[]]
    assert store_dir.is_dir()


def test_ingest_needs_no_llm_key(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """sre-ingest on infra: LLM_PROVIDER=anthropic, an OpenAI key, no Anthropic key. Ingest only embeds (#59)."""
    from langchain_core.embeddings import DeterministicFakeEmbedding

    from src.agent.retrieval import embeddings as embeddings_module
    from src.agent.retrieval import index as index_module
    from src.config import get_settings

    monkeypatch.setenv("LLM_PROVIDER", "anthropic")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    monkeypatch.setenv("PROMETHEUS_URL", "http://prometheus.test")
    monkeypatch.setenv("GRAFANA_URL", "http://grafana.test")
    monkeypatch.setenv("GRAFANA_SERVICE_ACCOUNT_TOKEN", "test-token")
    monkeypatch.setenv("ANONYMIZED_TELEMETRY", "False")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    get_settings.cache_clear()
    # The real settings and get_embeddings run; only the OpenAI client is faked, and the store is a temp dir.
    monkeypatch.setattr(embeddings_module, "OpenAIEmbeddings", lambda **_: DeterministicFakeEmbedding(size=8))
    store_dir = tmp_path / "chroma"
    monkeypatch.setattr(index_module, "CHROMA_PERSIST_DIR", store_dir)

    ingest_runbooks.main([])

    manifest = index_module.read_manifest(store_dir)
    assert manifest is not None
    assert manifest.built_by == "ingest"
    assert manifest.chunks > 0
