"""Ingest runbooks into the Chroma vector store.

Usage:
    uv run python -m scripts.ingest_runbooks
    # or via Makefile:
    make ingest
"""

import logging
import os
import shutil
import sys

from src.agent.retrieval.embeddings import CHROMA_PERSIST_DIR, build_vector_store, get_embeddings, load_all_documents

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
logger = logging.getLogger(__name__)


def main() -> None:
    """Load runbooks, embed them, and persist the vector store.

    Settings, the documents and one embedding call are checked before the old store is cleared, so a
    missing key or a bad config leaves the live index in place.
    """
    # Ingest only embeds, and embeddings always use OpenAI. Under LLM_PROVIDER=anthropic the settings would also
    # demand ANTHROPIC_API_KEY, which sre-ingest on infra does not have (#59). The openai provider's check asks for
    # OPENAI_API_KEY, the one key ingest needs, so run as that provider.
    os.environ["LLM_PROVIDER"] = "openai"

    documents = load_all_documents()
    if not documents:
        logger.error("No documents found — nothing to ingest; the existing vector store was left in place")
        sys.exit(1)

    try:
        get_embeddings().embed_query("ingest preflight")
    except Exception:
        logger.exception("Preflight failed (settings or embeddings API); the existing vector store was left in place")
        sys.exit(1)

    # Clear existing store for a clean rebuild.
    # Delete contents rather than the directory itself — when running in Docker,
    # the directory is a volume mountpoint and cannot be removed (EBUSY).
    if CHROMA_PERSIST_DIR.exists():
        logger.info("Clearing existing vector store at %s", CHROMA_PERSIST_DIR)
        for child in CHROMA_PERSIST_DIR.iterdir():
            if child.is_dir():
                shutil.rmtree(child)
            else:
                child.unlink()

    logger.info("Ingesting %d document chunks from all sources...", len(documents))
    store = build_vector_store(documents)

    # Verify
    count = store._collection.count()  # pyright: ignore[reportPrivateUsage]
    logger.info(
        "Done! Vector store contains %d documents. Restart a running sre-agent so it reads the new store.", count
    )


if __name__ == "__main__":
    main()
