"""Rebuild the runbook index (the Chroma vector store) by hand.

The running server rebuilds the index itself when the runbooks in its image change, so this is the fallback.

Usage:
    uv run python -m scripts.ingest_runbooks          # or: make ingest
    uv run python -m scripts.ingest_runbooks --wipe   # delete the whole store first (see the runbook)
"""

import argparse
import logging
import os
import shutil
import sys

from src.agent.retrieval.embeddings import CHROMA_PERSIST_DIR
from src.agent.retrieval.index import IndexBuildError, rebuild_index

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
logger = logging.getLogger(__name__)


def _wipe_store() -> None:
    """Delete the store's contents, not the directory: in Docker it is a volume mountpoint (EBUSY)."""
    if not CHROMA_PERSIST_DIR.exists():
        return
    logger.info("Wiping the vector store at %s", CHROMA_PERSIST_DIR)
    for child in CHROMA_PERSIST_DIR.iterdir():
        if child.is_dir():
            shutil.rmtree(child)
        else:
            child.unlink()


def main(argv: list[str] | None = None) -> None:
    """Build a new index collection and make it live.

    The documents and one embedding call are checked first, and the new collection replaces the old one only once it
    holds every chunk, so a missing key, a bad config or an API error leaves the live index in place. --wipe deletes
    the whole store first (recovery from a store Chroma cannot open); it gives up that guarantee.
    """
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--wipe", action="store_true", help="delete the whole store before building")
    args = parser.parse_args(argv)

    # Ingest only embeds, and embeddings always use OpenAI. Under LLM_PROVIDER=anthropic the settings would also
    # demand ANTHROPIC_API_KEY, which sre-ingest on infra does not have (#59). The openai provider's check asks for
    # OPENAI_API_KEY, the one key ingest needs, so run as that provider.
    os.environ["LLM_PROVIDER"] = "openai"

    if args.wipe:
        _wipe_store()

    try:
        manifest = rebuild_index("ingest")
    except IndexBuildError:
        logger.exception("Ingest failed; the existing vector store was left in place")
        sys.exit(1)

    logger.info(
        "Done! Collection %s holds %d chunks from %d files. A running sre-agent reads it on its next search.",
        manifest.collection,
        manifest.chunks,
        manifest.files,
    )


if __name__ == "__main__":
    main()
