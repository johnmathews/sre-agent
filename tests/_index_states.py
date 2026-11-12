"""Runbook index states for /health tests, which patch src.api.main.index_state with one of these."""

from src.agent.retrieval.index import IndexState, Manifest

MANIFEST = Manifest(
    fingerprint="a" * 64, collection="runbooks-aaaaaaaaaaaa-1234abcd", chunks=292, files=20, built_at=0.0, built_by="t"
)
HEALTHY_INDEX = IndexState(True, MANIFEST, MANIFEST.collection, 292, MANIFEST.fingerprint, True)
MISSING_INDEX = IndexState(False, None, None, 0, "b" * 64, False)
EMPTY_INDEX = IndexState(True, MANIFEST, MANIFEST.collection, 0, MANIFEST.fingerprint, False)
STALE_INDEX = IndexState(True, MANIFEST, MANIFEST.collection, 292, "b" * 64, False)
