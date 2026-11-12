"""Embedding pipeline for loading, chunking, and storing runbook documents."""

import logging
from pathlib import Path

from langchain_chroma import Chroma
from langchain_core.documents import Document
from langchain_core.embeddings import Embeddings
from langchain_openai import OpenAIEmbeddings
from pydantic import SecretStr

from src.config import get_settings

logger = logging.getLogger(__name__)

RUNBOOKS_DIR = Path(__file__).resolve().parents[3] / "runbooks"
CHROMA_PERSIST_DIR = Path(__file__).resolve().parents[3] / ".chroma_db"
COLLECTION_NAME = "runbooks"
EMBEDDING_MODEL = "text-embedding-3-small"

# Chunking parameters
CHUNK_SIZE = 800
CHUNK_OVERLAP = 100
HEADING_SEPARATORS = ("\n## ", "\n### ")


def _split_by_headings(text: str, separators: tuple[str, ...]) -> list[str]:
    """Split markdown text on heading boundaries, keeping the heading with its content."""
    chunks: list[str] = []
    current = text

    for sep in separators:
        parts = current.split(sep)
        if len(parts) > 1:
            # First part is content before first heading at this level
            result: list[str] = [parts[0]]
            for part in parts[1:]:
                result.append(sep.lstrip("\n") + part)
            # Recursively split each part by remaining separators
            remaining_seps = tuple(s for s in separators if s != sep)
            for part in result:
                if remaining_seps and len(part) > CHUNK_SIZE:
                    chunks.extend(_split_by_headings(part, remaining_seps))
                else:
                    chunks.append(part)
            return [c for c in chunks if c.strip()]

    return [current] if current.strip() else []


def _is_heading_only(section: str) -> bool:
    """True when every non-blank line of the section is a markdown heading."""
    lines = [line for line in section.splitlines() if line.strip()]
    return bool(lines) and all(line.startswith("#") for line in lines)


def _fold_heading_only(sections: list[str]) -> list[str]:
    """Fold sections that hold only headings into the section that follows.

    A bare heading embeds as a near-empty chunk that wastes a search slot, so its text
    becomes a prefix of the next section. A heading-only section at the end of a file has
    no next section, so it is appended to the previous one. A file whose only section is
    heading-only is kept as is.
    """
    folded: list[str] = []
    pending = ""
    for section in sections:
        if _is_heading_only(section):
            pending += section.rstrip("\n") + "\n\n"
            continue
        folded.append(pending + section)
        pending = ""
    if pending:
        tail = pending.rstrip("\n") + "\n"
        if folded:
            folded[-1] = folded[-1].rstrip("\n") + "\n\n" + tail
        else:
            folded.append(tail)
    return folded


def _leading_heading(chunk: str) -> str:
    """Return the most specific heading of a chunk: the last in its leading heading block.

    Folding can put several headings (`# Title`, `## Section`) ahead of the body. Falls back
    to the first heading anywhere in the chunk when it does not start with one.
    """
    leading: list[str] = []
    for line in chunk.splitlines():
        if not line.strip():
            continue
        if not line.startswith("#"):
            break
        leading.append(line)
    if leading:
        return leading[-1].lstrip("#").strip()
    for line in chunk.splitlines():
        if line.startswith("#"):
            return line.lstrip("#").strip()
    return ""


def _chunk_text(text: str, chunk_size: int, chunk_overlap: int) -> list[str]:
    """Split text into overlapping chunks, respecting paragraph boundaries."""
    paragraphs = text.split("\n\n")
    chunks: list[str] = []
    current_chunk = ""

    for para in paragraphs:
        if len(current_chunk) + len(para) + 2 > chunk_size and current_chunk:
            chunks.append(current_chunk.strip())
            # Keep overlap from end of previous chunk
            overlap_text = current_chunk[-chunk_overlap:] if len(current_chunk) > chunk_overlap else current_chunk
            current_chunk = overlap_text + "\n\n" + para
        else:
            current_chunk = current_chunk + "\n\n" + para if current_chunk else para

    if current_chunk.strip():
        chunks.append(current_chunk.strip())

    return chunks


def load_runbooks(runbooks_dir: Path | None = None) -> list[Document]:
    """Load and chunk all markdown files from a single directory.

    Each document gets metadata with the source filename and section heading.
    """
    directory = runbooks_dir or RUNBOOKS_DIR
    return _load_markdown_dir(directory)


def _load_markdown_dir(directory: Path) -> list[Document]:
    """Load and chunk all markdown files from a directory into LangChain Documents."""
    documents: list[Document] = []

    if not directory.exists():
        logger.warning("Document directory not found: %s", directory)
        return documents

    md_files = sorted(directory.glob("*.md"))
    if not md_files:
        logger.warning("No markdown files found in %s", directory)
        return documents

    source_dir = directory.name

    for md_file in md_files:
        text = md_file.read_text(encoding="utf-8")
        doc_name = md_file.stem

        # Extract title from first H1
        title = doc_name
        for line in text.splitlines():
            if line.startswith("# "):
                title = line[2:].strip()
                break

        # Split by headings first, then chunk large sections
        sections = _fold_heading_only(_split_by_headings(text, HEADING_SEPARATORS))

        for section in sections:
            sub_chunks = _chunk_text(section, CHUNK_SIZE, CHUNK_OVERLAP) if len(section) > CHUNK_SIZE else [section]
            # A heading followed by one oversized paragraph (a wide table) chunks to a bare heading.
            sub_chunks = _fold_heading_only(sub_chunks)

            for i, chunk in enumerate(sub_chunks):
                # Extract section heading from chunk
                section_heading = _leading_heading(chunk)

                documents.append(
                    Document(
                        page_content=chunk,
                        metadata={
                            "source": md_file.name,
                            "source_dir": source_dir,
                            "runbook": doc_name,
                            "title": title,
                            "section": section_heading,
                            "chunk_index": i,
                        },
                    )
                )

    logger.info(
        "Loaded %d chunks from %d files in %s",
        len(documents),
        len(md_files),
        directory,
    )
    return documents


def source_dirs() -> list[Path]:
    """The directories the index is built from: runbooks/, then each usable EXTRA_DOCS_DIRS entry.

    Extra directories are specified via the EXTRA_DOCS_DIRS environment variable as comma-separated absolute
    paths. Relative and missing paths are skipped with a warning.
    """
    dirs = [RUNBOOKS_DIR]

    try:
        settings = get_settings()
        extra_dirs_raw = settings.extra_docs_dirs
    except Exception:
        logger.debug("Settings not available — skipping extra document directories")
        extra_dirs_raw = ""

    if extra_dirs_raw.strip():
        for raw_path in extra_dirs_raw.split(","):
            dir_path = Path(raw_path.strip())
            if not dir_path.is_absolute():
                logger.warning("Skipping non-absolute path: %s", dir_path)
                continue
            if not dir_path.is_dir():
                logger.warning("Skipping non-existent directory: %s", dir_path)
                continue
            dirs.append(dir_path)
    return dirs


def load_documents(dirs: list[Path]) -> list[Document]:
    """Load and chunk the markdown files in each directory, in order. Only .md files are read."""
    documents: list[Document] = []
    for directory in dirs:
        documents.extend(_load_markdown_dir(directory))
    logger.info("Total documents loaded from all sources: %d", len(documents))
    return documents


def load_all_documents() -> list[Document]:
    """Load documents from runbooks/ and any extra directories configured in settings (see source_dirs)."""
    return load_documents(source_dirs())


def get_embeddings() -> OpenAIEmbeddings:
    """Create an OpenAI embeddings instance using project settings.

    Embeddings always use OpenAI (Anthropic has no embeddings API).
    OPENAI_API_KEY is required even when LLM_PROVIDER=anthropic.
    """
    settings = get_settings()
    if not settings.openai_api_key:
        raise ValueError(
            "OPENAI_API_KEY is required for embeddings (used by make ingest and runbook search) "
            "even when LLM_PROVIDER=anthropic. Anthropic does not offer an embeddings API."
        )
    return OpenAIEmbeddings(
        api_key=SecretStr(settings.openai_api_key),
        model=EMBEDDING_MODEL,
        base_url=settings.openai_base_url or None,
    )


def build_vector_store(
    documents: list[Document] | None = None,
    persist_directory: Path | None = None,
    *,
    embeddings: Embeddings | None = None,
    collection_name: str = COLLECTION_NAME,
) -> Chroma:
    """Embed documents into a Chroma collection, creating it or adding to it.

    If documents is None, loads from the default runbooks directory. The index's own rebuild path is
    src.agent.retrieval.index.rebuild_index, which builds a fresh collection and records it in the manifest.
    """
    docs = documents if documents is not None else load_runbooks()
    persist_dir = str(persist_directory or CHROMA_PERSIST_DIR)

    if not docs:
        logger.warning("No documents to embed — vector store will be empty")

    logger.info(
        "Building vector store with %d documents at %s (collection %s)", len(docs), persist_dir, collection_name
    )
    vector_store = Chroma.from_documents(  # pyright: ignore[reportUnknownMemberType]
        documents=docs,
        embedding=embeddings or get_embeddings(),
        collection_name=collection_name,
        persist_directory=persist_dir,
    )
    logger.info("Vector store built successfully")
    return vector_store


def load_vector_store(persist_directory: Path | None = None) -> Chroma:
    """Open the live index: the collection the manifest names, or 'runbooks' for a store built before it."""
    from src.agent.retrieval.index import open_index

    return open_index(persist_dir=persist_directory)
