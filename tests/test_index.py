"""The runbook index: source fingerprint, manifest, swap-in rebuild and the state the health check and metrics read.

These run real Chroma in a temp directory with a deterministic fake embedding model, so they make no network calls.
"""

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from chromadb.errors import NotFoundError
from langchain_core.documents import Document
from langchain_core.embeddings import DeterministicFakeEmbedding
from prometheus_client import REGISTRY

from src.agent.retrieval import index
from src.agent.retrieval.embeddings import COLLECTION_NAME, build_vector_store, load_documents
from src.agent.retrieval.runbooks import runbook_search
from src.observability.metrics import observe_runbook_index


@pytest.fixture(autouse=True)
def _no_chroma_telemetry(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ANONYMIZED_TELEMETRY", "False")


@pytest.fixture
def sources(tmp_path: Path) -> Path:
    """One runbook directory with two small runbooks."""
    directory = tmp_path / "runbooks"
    directory.mkdir()
    (directory / "dns.md").write_text("# DNS\n\n## Restart\n\nRestart AdGuard, then Unbound.\n")
    (directory / "ups.md").write_text("# UPS\n\n## On battery\n\nCheck the NUT status flags.\n")
    return directory


@pytest.fixture
def store(tmp_path: Path) -> Path:
    return tmp_path / "chroma"


EMBEDDINGS = DeterministicFakeEmbedding(size=8)


class BrokenEmbeddings(DeterministicFakeEmbedding):
    def embed_query(self, text: str) -> list[float]:
        raise RuntimeError("401 invalid api key")


def _rebuild(sources: Path, store: Path, built_by: str = "test") -> index.Manifest:
    return index.rebuild_index(built_by, persist_dir=store, embeddings=EMBEDDINGS, dirs=[sources])


def _collections(store: Path) -> list[str]:
    return sorted(c.name for c in index.chroma_client(store).list_collections())


class TestFingerprint:
    def test_same_sources_same_fingerprint(self, sources: Path) -> None:
        assert index.source_fingerprint([sources]) == index.source_fingerprint([sources])

    def test_one_byte_changes_it(self, sources: Path) -> None:
        before = index.source_fingerprint([sources])
        (sources / "dns.md").write_text("# DNS\n\n## Restart\n\nRestart AdGuard, then Unbound!\n")
        assert index.source_fingerprint([sources]) != before

    def test_a_rename_changes_it(self, sources: Path) -> None:
        before = index.source_fingerprint([sources])
        (sources / "dns.md").rename(sources / "dns-stack.md")
        assert index.source_fingerprint([sources]) != before

    def test_a_new_file_changes_it(self, sources: Path) -> None:
        before = index.source_fingerprint([sources])
        (sources / "nfs.md").write_text("# NFS\n")
        assert index.source_fingerprint([sources]) != before


class TestState:
    def test_missing_store(self, sources: Path, store: Path) -> None:
        state = index.index_state(persist_dir=store, dirs=[sources])
        assert not state.exists
        assert state.chunks == 0
        assert not state.in_sync

    def test_store_built_before_the_manifest_is_out_of_sync(self, sources: Path, store: Path) -> None:
        """The infra store today: a 'runbooks' collection and no manifest. Searchable, but nothing says from what."""
        build_vector_store(load_documents([sources]), persist_directory=store, embeddings=EMBEDDINGS)

        state = index.index_state(persist_dir=store, dirs=[sources])

        assert state.manifest is None
        assert state.collection == COLLECTION_NAME
        assert state.chunks > 0
        assert not state.in_sync

    def test_rebuilt_store_is_in_sync(self, sources: Path, store: Path) -> None:
        manifest = _rebuild(sources, store)

        state = index.index_state(persist_dir=store, dirs=[sources])

        assert state.in_sync
        assert state.chunks == manifest.chunks > 0
        assert state.collection == manifest.collection

    def test_changed_runbook_is_out_of_sync(self, sources: Path, store: Path) -> None:
        _rebuild(sources, store)
        (sources / "nfs.md").write_text("# NFS\n\nRemount the share.\n")

        assert not index.index_state(persist_dir=store, dirs=[sources]).in_sync

    def test_tampered_manifest_is_out_of_sync(self, sources: Path, store: Path) -> None:
        _rebuild(sources, store)
        path = store / index.MANIFEST_NAME
        data = json.loads(path.read_text())
        data["fingerprint"] = "test-tampered"
        path.write_text(json.dumps(data))

        assert not index.index_state(persist_dir=store, dirs=[sources]).in_sync

    def test_state_read_mid_swap_reads_the_manifest_again(
        self, sources: Path, store: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The manifest named a collection a rebuild deleted before the count: read the manifest again."""
        stale = _rebuild(sources, store)
        live = _rebuild(sources, store)
        reads = iter([stale, live])
        monkeypatch.setattr(index, "read_manifest", lambda persist_dir=None: next(reads))

        state = index.index_state(persist_dir=store, dirs=[sources])

        assert state.collection == live.collection
        assert state.chunks == live.chunks
        assert state.in_sync

    def test_unreadable_manifest_is_out_of_sync(self, sources: Path, store: Path) -> None:
        _rebuild(sources, store)
        (store / index.MANIFEST_NAME).write_text("{not json")

        state = index.index_state(persist_dir=store, dirs=[sources])

        assert state.manifest is None
        assert not state.in_sync


class TestRebuild:
    def test_writes_manifest_matching_the_sources(self, sources: Path, store: Path) -> None:
        manifest = _rebuild(sources, store, built_by="sre-agent")

        assert index.read_manifest(store) == manifest
        assert manifest.fingerprint == index.source_fingerprint([sources])
        assert manifest.files == 2
        assert manifest.built_by == "sre-agent"
        assert manifest.collection.startswith(f"{COLLECTION_NAME}-{manifest.fingerprint[:12]}-")

    def test_leaves_exactly_one_collection(self, sources: Path, store: Path) -> None:
        build_vector_store(load_documents([sources]), persist_directory=store, embeddings=EMBEDDINGS)
        _rebuild(sources, store)
        (sources / "nfs.md").write_text("# NFS\n\nRemount the share.\n")
        manifest = _rebuild(sources, store)

        assert _collections(store) == [manifest.collection]

    def test_rebuild_of_same_sources_swaps_in_a_new_collection(self, sources: Path, store: Path) -> None:
        """A rebuild never deletes the live collection before its replacement holds every chunk."""
        first = _rebuild(sources, store)
        second = _rebuild(sources, store)

        assert second.collection != first.collection
        assert _collections(store) == [second.collection]
        assert index.index_state(persist_dir=store, dirs=[sources]).chunks == first.chunks

    def test_failed_embedding_leaves_the_live_index(self, sources: Path, store: Path) -> None:
        live = _rebuild(sources, store)
        (sources / "nfs.md").write_text("# NFS\n\nRemount the share.\n")

        with pytest.raises(index.IndexBuildError):
            index.rebuild_index("test", persist_dir=store, embeddings=BrokenEmbeddings(size=8), dirs=[sources])

        assert index.read_manifest(store) == live
        assert _collections(store) == [live.collection]
        assert index.index_state(persist_dir=store, dirs=[sources]).chunks == live.chunks

    def test_failure_mid_build_deletes_the_partial_collection(self, sources: Path, store: Path) -> None:
        class FailsOnDocuments(DeterministicFakeEmbedding):
            def embed_documents(self, texts: list[str]) -> list[list[float]]:
                raise RuntimeError("429 rate limited")

        live = _rebuild(sources, store)

        with pytest.raises(index.IndexBuildError, match="429"):
            index.rebuild_index("test", persist_dir=store, embeddings=FailsOnDocuments(size=8), dirs=[sources])

        assert index.read_manifest(store) == live
        assert _collections(store) == [live.collection]

    def test_failed_manifest_write_leaves_the_live_index(
        self, sources: Path, store: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        live = _rebuild(sources, store)

        def disk_full(manifest: index.Manifest, persist_dir: Path) -> None:
            raise OSError(28, "No space left on device")

        monkeypatch.setattr(index, "_write_manifest", disk_full)

        with pytest.raises(index.IndexBuildError, match="No space left"):
            _rebuild(sources, store)

        assert index.read_manifest(store) == live
        assert _collections(store) == [live.collection]

    def test_preflight_error_drops_key_fragments(self, sources: Path, store: Path) -> None:
        class BadKey(DeterministicFakeEmbedding):
            def embed_query(self, text: str) -> list[float]:
                raise RuntimeError("Incorrect API key provided: sk-proj-abc1****wxyz.")

        with pytest.raises(index.IndexBuildError) as raised:
            index.rebuild_index("test", persist_dir=store, embeddings=BadKey(size=8), dirs=[sources])

        assert "abc1" not in str(raised.value)
        assert "sk-[redacted]" in str(raised.value)

    def test_overlapping_builds_leave_a_live_manifest(
        self, sources: Path, store: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The server's loop and a manual sre-ingest overlap: the later build must not delete the earlier one's
        collection between its count check and its manifest write."""
        import threading

        real_write = index._write_manifest  # pyright: ignore[reportPrivateUsage]
        second_done = threading.Event()
        first_writer = threading.Event()

        def write(manifest: index.Manifest, persist_dir: Path) -> None:
            if not first_writer.is_set():
                first_writer.set()
                second_done.wait(timeout=1.0)  # without the build lock, the second build finishes in this window
            real_write(manifest, persist_dir)

        monkeypatch.setattr(index, "_write_manifest", write)
        first = threading.Thread(target=_rebuild, args=(sources, store))
        first.start()
        first_writer.wait(timeout=5.0)
        second = threading.Thread(target=lambda: (_rebuild(sources, store), second_done.set()))
        second.start()
        first.join()
        second.join()

        manifest = index.read_manifest(store)
        assert manifest is not None
        assert _collections(store) == [manifest.collection]
        assert index.index_state(persist_dir=store, dirs=[sources]).in_sync

    def test_no_documents_is_an_error(self, tmp_path: Path, store: Path) -> None:
        empty = tmp_path / "empty"
        empty.mkdir()

        with pytest.raises(index.IndexBuildError, match="No documents"):
            index.rebuild_index("test", persist_dir=store, embeddings=EMBEDDINGS, dirs=[empty])

    def test_search_reads_the_manifest_collection(self, sources: Path, store: Path) -> None:
        manifest = _rebuild(sources, store)

        vector_store = index.open_index(persist_dir=store, embeddings=EMBEDDINGS)

        assert vector_store._collection.name == manifest.collection  # pyright: ignore[reportPrivateUsage]
        assert vector_store.similarity_search("NUT status", k=1)


class TestSearchDuringRebuild:
    def test_search_retries_once_when_its_collection_was_just_swapped_out(self) -> None:
        """A search that read the manifest just before a rebuild deleted that collection opens the new one."""
        old = MagicMock()
        old.similarity_search_with_relevance_scores.side_effect = NotFoundError(
            "Collection [runbooks-old] does not exist"
        )
        new = MagicMock()
        new.similarity_search_with_relevance_scores.return_value = [
            (Document(page_content="Restart AdGuard.", metadata={"source": "dns.md"}), 0.9)
        ]

        with patch("src.agent.retrieval.runbooks.load_vector_store", side_effect=[old, new]):
            result = runbook_search.invoke({"query": "restart DNS"})

        assert "Found 1 relevant runbook chunk" in result


def _rebuilds(status: str) -> float:
    return REGISTRY.get_sample_value("sre_assistant_runbook_index_rebuilds_total", {"status": status}) or 0.0


class TestCheckLoop:
    async def test_out_of_sync_index_is_rebuilt(self, sources: Path, store: Path) -> None:
        before = _rebuilds("success")

        state = await index.check_index_once(True, persist_dir=store, dirs=[sources], embeddings=EMBEDDINGS)

        assert state.in_sync
        assert index.read_manifest(store) is not None
        assert _rebuilds("success") == before + 1

    async def test_in_sync_index_is_left_alone(self, sources: Path, store: Path) -> None:
        live = _rebuild(sources, store)
        before = _rebuilds("success")

        state = await index.check_index_once(True, persist_dir=store, dirs=[sources], embeddings=EMBEDDINGS)

        assert state.in_sync
        assert index.read_manifest(store) == live
        assert _rebuilds("success") == before

    async def test_failed_rebuild_is_counted_and_retried_next_tick(self, sources: Path, store: Path) -> None:
        live = _rebuild(sources, store)
        (sources / "nfs.md").write_text("# NFS\n\nRemount the share.\n")
        errors = _rebuilds("error")

        failed = await index.check_index_once(
            True, persist_dir=store, dirs=[sources], embeddings=BrokenEmbeddings(size=8)
        )

        assert not failed.in_sync
        assert index.read_manifest(store) == live
        assert _rebuilds("error") == errors + 1

        healed = await index.check_index_once(True, persist_dir=store, dirs=[sources], embeddings=EMBEDDINGS)
        assert healed.in_sync

    async def test_auto_rebuild_off_only_reports(self, sources: Path, store: Path) -> None:
        state = await index.check_index_once(False, persist_dir=store, dirs=[sources], embeddings=EMBEDDINGS)

        assert not state.in_sync
        assert not store.exists()

    async def test_loop_with_interval_zero_checks_once(self, sources: Path, store: Path) -> None:
        await index.keep_index_current(0, True, persist_dir=store, dirs=[sources], embeddings=EMBEDDINGS)

        assert index.index_state(persist_dir=store, dirs=[sources]).in_sync


class TestGauges:
    def test_gauges_read_the_index_on_each_scrape(self, sources: Path, store: Path) -> None:
        observe_runbook_index(lambda: index.index_state(persist_dir=store, dirs=[sources]), max_age_s=0)
        assert REGISTRY.get_sample_value("sre_assistant_runbook_index_in_sync") == 0.0
        assert REGISTRY.get_sample_value("sre_assistant_runbook_index_chunks") == 0.0

        manifest = _rebuild(sources, store)

        assert REGISTRY.get_sample_value("sre_assistant_runbook_index_in_sync") == 1.0
        assert REGISTRY.get_sample_value("sre_assistant_runbook_index_chunks") == manifest.chunks
        assert REGISTRY.get_sample_value("sre_assistant_runbook_index_built_timestamp_seconds") == manifest.built_at

    def test_a_failing_read_exports_out_of_sync(self) -> None:
        def broken() -> index.IndexState:
            raise OSError("disk gone")

        observe_runbook_index(broken, max_age_s=0)
        assert REGISTRY.get_sample_value("sre_assistant_runbook_index_in_sync") == 0.0
        assert REGISTRY.get_sample_value("sre_assistant_runbook_index_chunks") == 0.0
