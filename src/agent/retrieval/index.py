"""The runbook index: what it was built from, whether that still matches the sources, and how it is rebuilt.

The Chroma store holds one live collection, named in a manifest file beside it. The manifest records a fingerprint
of the sources it was built from (every indexed markdown file, plus the chunking and embedding settings). Comparing
that with a fingerprint of the files present now says whether the index is out of date, which the server's check
loop, its metrics and its /health read.

A rebuild embeds into a new collection, then points the manifest at it, then deletes the old ones. A failed build
leaves the live collection and manifest untouched, and a search during a build reads the old collection.
"""

import hashlib
import json
import logging
import os
import time
import uuid
from dataclasses import asdict, dataclass
from pathlib import Path

import chromadb
from chromadb.api import ClientAPI
from chromadb.errors import NotFoundError
from langchain_chroma import Chroma
from langchain_core.embeddings import Embeddings

from src.agent.retrieval.embeddings import (
    CHROMA_PERSIST_DIR,
    CHUNK_OVERLAP,
    CHUNK_SIZE,
    COLLECTION_NAME,
    EMBEDDING_MODEL,
    get_embeddings,
    load_documents,
    source_dirs,
)

logger = logging.getLogger(__name__)

MANIFEST_NAME = "index-manifest.json"
# Bump when the fingerprint's inputs change shape, so every store rebuilds once.
FINGERPRINT_VERSION = 1


class IndexBuildError(RuntimeError):
    """A rebuild failed. The live index, if there was one, is unchanged."""


@dataclass(frozen=True)
class Manifest:
    """What the live collection was built from. Written only after a build succeeds."""

    fingerprint: str
    collection: str
    chunks: int
    files: int
    built_at: float
    built_by: str


@dataclass(frozen=True)
class IndexState:
    """The index as it is now, compared with the sources present now."""

    exists: bool
    manifest: Manifest | None
    collection: str | None
    chunks: int
    fingerprint: str
    in_sync: bool
    error: str | None = None


def chroma_client(persist_dir: Path | None = None) -> ClientAPI:
    """The process's Chroma client for the store. Chroma shares one per path, with default settings as here."""
    return chromadb.PersistentClient(path=str(persist_dir or CHROMA_PERSIST_DIR))


def _source_files(dirs: list[Path]) -> list[tuple[str, Path]]:
    """Each indexed file with a stable key: its directory's name and its own. Same glob as the loader."""
    files: list[tuple[str, Path]] = []
    for position, directory in enumerate(dirs):
        if not directory.is_dir():
            continue
        files.extend((f"{position}:{directory.name}/{path.name}", path) for path in sorted(directory.glob("*.md")))
    return files


def source_fingerprint(dirs: list[Path] | None = None) -> str:
    """SHA-256 over every indexed file (key and bytes) and the settings that shape the chunks and vectors."""
    digest = hashlib.sha256()
    header = f"v{FINGERPRINT_VERSION}|{EMBEDDING_MODEL}|{CHUNK_SIZE}|{CHUNK_OVERLAP}\n"
    digest.update(header.encode())
    for key, path in _source_files(source_dirs() if dirs is None else dirs):
        digest.update(key.encode())
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def read_manifest(persist_dir: Path | None = None) -> Manifest | None:
    """The manifest, or None when there is none or it cannot be read (both mean: rebuild)."""
    path = (persist_dir or CHROMA_PERSIST_DIR) / MANIFEST_NAME
    try:
        data = json.loads(path.read_text())
        return Manifest(
            fingerprint=str(data["fingerprint"]),
            collection=str(data["collection"]),
            chunks=int(data["chunks"]),
            files=int(data["files"]),
            built_at=float(data["built_at"]),
            built_by=str(data["built_by"]),
        )
    except FileNotFoundError:
        return None
    except (OSError, ValueError, KeyError, TypeError) as exc:
        logger.warning("Index manifest at %s is unreadable (%s); treating the index as out of date", path, exc)
        return None


def _write_manifest(manifest: Manifest, persist_dir: Path) -> None:
    path = persist_dir / MANIFEST_NAME
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(asdict(manifest), indent=2) + "\n")
    os.replace(tmp, path)


def _is_index_collection(name: str) -> bool:
    return name == COLLECTION_NAME or name.startswith(f"{COLLECTION_NAME}-")


def rebuild_index(
    built_by: str,
    *,
    persist_dir: Path | None = None,
    embeddings: Embeddings | None = None,
    dirs: list[Path] | None = None,
) -> Manifest:
    """Embed the sources into a new collection, make it live, and delete the old ones.

    Raises IndexBuildError when there is nothing to index, the embeddings API fails, or the new collection does not
    hold every chunk. In each case the live collection and manifest are left as they were.
    """
    store_dir = persist_dir or CHROMA_PERSIST_DIR
    source = source_dirs() if dirs is None else dirs
    fingerprint = source_fingerprint(source)
    files = len(_source_files(source))
    documents = load_documents(source)
    if not documents:
        raise IndexBuildError(f"No documents found in {', '.join(str(d) for d in source)}; nothing to index")

    try:
        embedder = embeddings or get_embeddings()
        embedder.embed_query("ingest preflight")
    except Exception as exc:
        raise IndexBuildError(f"Embeddings preflight failed: {type(exc).__name__}: {exc}") from exc

    store_dir.mkdir(parents=True, exist_ok=True)
    client = chroma_client(store_dir)
    collection = f"{COLLECTION_NAME}-{fingerprint[:12]}-{uuid.uuid4().hex[:8]}"
    logger.info("Building index collection %s from %d chunks in %d files", collection, len(documents), files)
    try:
        store = Chroma.from_documents(  # pyright: ignore[reportUnknownMemberType]
            documents=documents, embedding=embedder, collection_name=collection, client=client
        )
        chunks = store._collection.count()  # pyright: ignore[reportPrivateUsage]
        if chunks != len(documents):
            raise IndexBuildError(f"Collection {collection} holds {chunks} chunks, expected {len(documents)}")
    except Exception as exc:
        try:
            client.delete_collection(collection)
        except Exception:
            logger.warning("Could not delete the partial collection %s", collection, exc_info=True)
        if isinstance(exc, IndexBuildError):
            raise
        raise IndexBuildError(f"Building {collection} failed: {type(exc).__name__}: {exc}") from exc

    manifest = Manifest(
        fingerprint=fingerprint,
        collection=collection,
        chunks=chunks,
        files=files,
        built_at=time.time(),
        built_by=built_by,
    )
    _write_manifest(manifest, store_dir)
    logger.info("Index collection %s is live (%d chunks, fingerprint %s)", collection, chunks, fingerprint[:12])

    for old in client.list_collections():
        if old.name != collection and _is_index_collection(old.name):
            try:
                client.delete_collection(old.name)
                logger.info("Deleted old index collection %s", old.name)
            except Exception:
                logger.warning("Could not delete old index collection %s", old.name, exc_info=True)
    return manifest


def active_collection(persist_dir: Path | None = None) -> str:
    """The collection searches read: the manifest's, or 'runbooks' for a store built before the manifest existed."""
    manifest = read_manifest(persist_dir)
    return manifest.collection if manifest else COLLECTION_NAME


def open_index(*, persist_dir: Path | None = None, embeddings: Embeddings | None = None) -> Chroma:
    """Open the live collection for search. Raises when it does not exist, rather than creating an empty one."""
    store_dir = persist_dir or CHROMA_PERSIST_DIR
    return Chroma(
        collection_name=active_collection(store_dir),
        embedding_function=embeddings or get_embeddings(),
        client=chroma_client(store_dir),
        create_collection_if_not_exists=False,
    )


def index_state(*, persist_dir: Path | None = None, dirs: list[Path] | None = None) -> IndexState:
    """Read the store and compare it with the sources. Never raises; a failure to read shows in `error`."""
    store_dir = persist_dir or CHROMA_PERSIST_DIR
    try:
        fingerprint = source_fingerprint(dirs)
    except OSError as exc:
        return IndexState(False, None, None, 0, "", False, f"reading the sources failed: {exc}")

    if not store_dir.is_dir():
        return IndexState(False, None, None, 0, fingerprint, False)

    manifest = read_manifest(store_dir)
    collection = manifest.collection if manifest else COLLECTION_NAME
    error: str | None = None
    try:
        chunks = chroma_client(store_dir).get_collection(collection).count()
    except NotFoundError:
        chunks = 0
    except Exception as exc:
        chunks = 0
        error = f"reading collection {collection} failed: {type(exc).__name__}: {exc}"

    in_sync = manifest is not None and manifest.fingerprint == fingerprint and chunks == manifest.chunks > 0
    return IndexState(True, manifest, collection, chunks, fingerprint, in_sync, error)
