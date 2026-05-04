"""LangChain tool for retrieving relevant runbook content via RAG."""

import logging

from langchain_core.tools import tool  # pyright: ignore[reportUnknownVariableType]
from pydantic import BaseModel, Field

from src.agent.retrieval.embeddings import load_vector_store
from src.agent.tools import HOMELAB_CONTEXT

logger = logging.getLogger(__name__)

DEFAULT_K = 4
MAX_K = 10


class RunbookSearchInput(BaseModel):
    """Input for searching the runbook knowledge base."""

    query: str = Field(
        description=(
            "A natural language search query describing the operational topic, "
            "service, or procedure to look up. Examples: 'restart DNS stack', "
            "'NFS mount troubleshooting', 'UPS battery low', 'quiet hours containers'."
        ),
    )
    num_results: int = Field(
        default=DEFAULT_K,
        ge=1,
        le=MAX_K,
        description=f"Number of relevant chunks to retrieve (1-{MAX_K}, default {DEFAULT_K}).",
    )


TOOL_DESCRIPTION = HOMELAB_CONTEXT + (
    "Search the operational runbook knowledge base for homelab procedures, "
    "troubleshooting steps, architecture details, and service documentation. "
    "Use this when the user asks about how to fix something, how a service works, "
    "what the architecture of a component is, or operational procedures.\n\n"
    "Available runbooks cover: DNS stack (AdGuard + Unbound), TrueNAS storage "
    "(ZFS pools: tank, swift, backup), NFS share management and debugging, "
    "disk management, UPS power, Cloudflare tunnel (cloudflared — native systemd "
    "service, not Docker), MikroTik router monitoring, Tailscale VPN, Traefik "
    "reverse proxy, systemd services, media VM, and quiet hours (currently disabled).\n\n"
    "Returns relevant text chunks with source metadata. This searches static "
    "documentation — for live system state, use the Prometheus or Grafana tools instead."
)


@tool("runbook_search", args_schema=RunbookSearchInput)
def runbook_search(query: str, num_results: int = DEFAULT_K) -> str:
    """Search runbooks for relevant operational information. See TOOL_DESCRIPTION."""
    logger.info("Searching runbooks for: %s (k=%d)", query, num_results)

    try:
        vector_store = load_vector_store()
        results = vector_store.similarity_search_with_relevance_scores(query, k=num_results)
    except Exception as e:
        logger.exception("Runbook search failed")
        return (
            f"Runbook search failed ({type(e).__name__}): {e}. "
            "Likely causes: vector store not yet built (run 'make ingest'), "
            "permission error on the persist directory, or a chromadb version mismatch."
        )

    if not results:
        return (
            "No relevant runbook content found for this query. "
            "Try rephrasing or check if the topic is covered in the runbooks."
        )

    lines: list[str] = [f"Found {len(results)} relevant runbook chunk(s):\n"]

    for doc, score in results:
        source = doc.metadata.get("source", "unknown")
        title = doc.metadata.get("title", "")
        section = doc.metadata.get("section", "")

        header = f"[{source}]"
        if title:
            header += f" {title}"
        if section:
            header += f" > {section}"
        header += f" (relevance: {score:.2f})"

        lines.append(f"---\n{header}\n\n{doc.page_content}\n")

    return "\n".join(lines)


runbook_search.description = TOOL_DESCRIPTION
