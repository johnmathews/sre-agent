# 2026-05-04 — Runbook search outage and the missing docs MCP wiring

## What prompted this

A user-facing screenshot from the deployed agent: it had been asked "How do I run
the journal insights app locally for dev work?" and replied that it didn't have
a runbook or README for it and that **runbook search was currently down**. The
brief was: figure out what that means and fix it.

## Triage

Hitting `runbook_search` directly via the MCP tool reproduced the failure
immediately — it returned:

> Runbook search failed: Could not connect to tenant default_tenant.
> Are you sure it exists?

That message comes from `src/agent/retrieval/runbooks.py` catching whatever the
underlying chromadb call raised and stuffing it into a generic string. The text
is misleading — there's nothing tenant-related actually wrong. Inside the
container, even constructing a `chromadb.PersistentClient(path="/app/.chroma_db")`
failed before any tenant access, with the real error:

```
chromadb.errors.InternalError: error returned from database: (code: 8)
attempt to write a readonly database
```

A fresh `PersistentClient` on a writable scratch path worked fine — it was
purely a perms issue on the persisted volume.

## Root cause

Two services share the `chroma_data` volume:

1. `sre-agent` (FastAPI runtime) — the prod compose pinned it to
   `user: "1001:1001"`.
2. `sre-ingest` (`make ingest` setup container) — no `user:` override → ran
   as root, wrote sqlite as `root:root` mode `0644`.

ChromaDB 1.x opens the persisted sqlite read-write even when the caller only
intends to read (it needs WAL/journal/migrations). uid 1001 had read but no
write, so init crashed. The langchain wrapper then surfaced a different,
unrelated error string.

The volume on the infra host:

```
$ sudo ls -lan /var/lib/docker/volumes/infra_chroma_data/_data/
drwxr-xr-x 3 0 0 ... .
-rw-r--r-- 1 0 0 ... chroma.sqlite3
drwxr-xr-x 2 0 0 ... <collection-id>/
```

Owned by root, read by uid 1001 — broken.

## Fixes

**Image-level (durable):** added `USER 1001:1001` to the runtime stage of the
`Dockerfile`, with `useradd` plus `COPY --chown=1001:1001` for venv/src/scripts/
runbooks/ and a `chown` of `/app` itself. Both `sre-agent` and `sre-ingest`
build from the same image, so future ingests write the volume as 1001 — no
more drift. The `user: "1001:1001"` override on `sre-agent` in
`/srv/infra/docker-compose.yml` is now redundant (left in place for now).

**Error-message diagnosability:** `runbook_search` now emits the real exception
type and uses `logger.exception` for traceback in logs, and returns plausible
causes (perms, version mismatch, not-yet-built) rather than blaming missing
data. Future failures should be diagnosable from one log line.

**Production rollout:**

1. `docker compose pull sre-agent sre-ingest`
2. `sudo chown -R 1001:1001 /var/lib/docker/volumes/infra_chroma_data/_data` (one-time)
3. `docker compose --profile setup run --rm sre-ingest` — re-built the vector
   store as 1001. 274 chunks from 17 runbook files.
4. `docker compose up -d sre-agent` — restarted on the new image.
5. End-to-end check: `runbook_search` returned real chunks for "DNS
   troubleshooting" both directly and via MCP.

## The bigger thing it surfaced — docs MCP wasn't wired up in prod

The original screenshot question wasn't actually a runbook question. It was a
dev-setup question — the right answer would have come from the indexed
`journal-insights-server` and `journal-insights-webapp` docs in the separate
documentation MCP server. The agent code (`src/agent/sdk_agent.py:149-155`)
already supports this:

```python
if settings.documentation_mcp_url:
    docs_server: McpHttpServerConfig = {..., "url": ...}
    mcp_servers["docs"] = docs_server
    allowed_tools.append("mcp__docs__*")
```

And `src/agent/system_prompt.md:90-91` promises the model that `search_docs`
and `query_docs` exist. But prod's `.env` had no `DOCUMENTATION_MCP_URL`, so
the conditional skipped registration silently, and the system prompt was
lying to the model about its tools. The docs server was healthy and
reachable from the agent's network the whole time.

Fix: added `DOCUMENTATION_MCP_URL=http://192.168.2.106:8085/mcp` to prod
`.env` and restarted. (The internal DNS form `http://documentation-server:8080/mcp`
is preferable — fewer hops, host-IP-independent — but both work.)

End-to-end verification: the same question from the original screenshot now
gets a detailed accurate dev-setup answer (specific filenames, ports, CLI
commands). The docs server's logs show the agent invoking
`search_docs query='journal insights app local development' results=10
duration_ms=1399` — direct evidence the integration is live.

## Open follow-ups

1. The system prompt unconditionally describes `search_docs`/`query_docs` as
   available. If `DOCUMENTATION_MCP_URL` is unset, we should either (a) drop
   those lines from the prompt, or (b) build the prompt dynamically based on
   which servers got registered. Otherwise the model invents tool calls that
   don't exist.
2. The repo's standalone `docker-compose.yml` doesn't have a docs-server
   service or a `DOCUMENTATION_MCP_URL` example — anyone using the repo's
   compose as a starting point won't get docs search by default. Either
   document it or add an opt-in profile.
3. The `user: "1001:1001"` override on the prod `sre-agent` service is now
   redundant after the Dockerfile change. Optional cleanup.
4. `runbook_search` could fail-fast on init by checking the persist-dir's
   writability before constructing the Chroma client, returning a more
   targeted message rather than relying on chromadb's internal error string.

## Diff summary

- `Dockerfile` — added `USER 1001:1001` runtime stage with chowned copies
- `src/agent/retrieval/runbooks.py:55-65` — improved error path
- `docs/architecture.md:557` — note explaining the shared-uid requirement and
  recovery procedure
- `.gitignore` — added `test-results/`
