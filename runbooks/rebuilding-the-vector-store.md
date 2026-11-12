# Rebuilding the Chroma vector store

> **Use when:** the "SRE agent on Infra VM: runbook index out of date" alert fires; the agent's runbook search returns nothing
> or stale results; `/health` shows `vector_store` unhealthy; you're setting the stack up on a new host; or Chroma
> fails with `readonly database`.

The agent's runbook search reads a Chroma vector store at `/app/.chroma_db` inside the container, mounted from the
`chroma_data` Docker volume (`infra_chroma_data` on infra). **sre-agent keeps it current itself.** At startup and every
`RUNBOOK_INDEX_CHECK_INTERVAL_S` (default 600 s) it compares the store with the runbooks in its image, and rebuilds
the store in the background when they differ. A new image with changed runbooks is indexed within a minute or two of
WUD redeploying it, with no restart and no manual step.

## TL;DR

```bash
# Is it current? 1 = in sync. (Prometheus, or the sre-agent MCP prometheus_instant_query)
max(sre_assistant_runbook_index_in_sync{job="sre-agent"})

# Why not? The check loop logs every decision.
docker logs sre-agent 2>&1 | grep -i "runbook index"

# Rebuild by hand (from /srv/infra on infra). No restart needed afterwards.
docker compose run --rm sre-ingest
```

## How the index knows it is current

The store holds one live collection, named `runbooks-<fingerprint>-<build id>`, and a manifest beside it,
`/app/.chroma_db/index-manifest.json`:

```json
{ "fingerprint": "835cf138faad…", "collection": "runbooks-835cf138faad-33d764cf",
  "chunks": 292, "files": 20, "built_at": 1762953600.0, "built_by": "sre-agent" }
```

- **The fingerprint** is a SHA-256 over every indexed `.md` file (its name and bytes) plus the chunk size, chunk
  overlap and embedding model. Any edit, new file, rename or chunking change changes it.
- **In sync** means the manifest's fingerprint equals the fingerprint of the files present now, and the collection
  holds the manifest's chunk count.
- **A rebuild** embeds into a new collection, rewrites the manifest to point at it, then deletes the old collections.
  A failed build changes nothing, and a search during a build reads the old collection.
- **The code** is `src/agent/retrieval/index.py`.

## What gets indexed

1. Every `*.md` file directly in `runbooks/`, baked into the image at build time.
2. Every `*.md` file directly in each directory in `EXTRA_DOCS_DIRS` (comma-separated absolute paths; empty on
   infra). Relative or missing paths are skipped with a warning, so a directory has to be mounted into `sre-agent`
   (and into `sre-ingest`, if you run it) to be indexed.

## Signals

| Signal | Meaning |
|---|---|
| `sre_assistant_runbook_index_in_sync` | 1 when in sync, 0 otherwise. Read from the store on each scrape. |
| `sre_assistant_runbook_index_chunks` | Chunks in the live collection (292 from 20 runbooks on 2026-11-12). |
| `sre_assistant_runbook_index_built_timestamp_seconds` | When the live collection was built; 0 with no manifest. |
| `sre_assistant_runbook_index_rebuilds_total{status}` | Rebuilds the check loop started: `success` or `error`. |
| `/health` → `vector_store` | Unhealthy when the store is missing or empty. Out of date is healthy, with a detail line. |
| Grafana "SRE agent on Infra VM: runbook index out of date" | `in_sync` below 1 for 30 min, or no data. Pushover. |

The alert's 30 minutes spans three checks, so it fires only when rebuilds keep failing, auto-rebuild is off, or
sre-agent is down.

## When the alert fires

1. Read the loop's log: `docker logs sre-agent 2>&1 | grep -i "runbook index"`. Each check logs one line: in sync,
   or not in sync with the reason, then either the new collection or `Runbook index rebuild failed` with a traceback.
2. Match the failure:
   - **`Embeddings preflight failed`** — `OPENAI_API_KEY` is missing, revoked or out of credit. Embeddings always use
     OpenAI, whatever `LLM_PROVIDER` says. Fix the key in `/srv/infra/.env`, then `docker compose up -d sre-agent`.
   - **`readonly database`** or a permission error — see "Permissions" below.
   - **`No documents found`** — the image has no runbooks, which means a broken build. Check the image.
   - **No data** (the gauge is absent) — sre-agent is down or Prometheus can't scrape it. The "Scrape target down"
     rule fires alongside.
3. Once the cause is fixed, the next check (within 10 minutes) rebuilds. To rebuild now, run
   `docker compose run --rm sre-ingest`, or `docker restart sre-agent` (its startup check rebuilds; `/mcp` drops for
   about 20 s).

## Rebuilding by hand

| Where you are | Command |
|---|---|
| Production (infra, `/srv/infra`) | `docker compose run --rm sre-ingest` |
| Local docker-compose stack | `docker compose run --rm sre-ingest` |
| The repo on the host, no Docker | `make ingest` — writes a host-local `.chroma_db/`, separate from any container's volume |

`sre-ingest` runs `python -m scripts.ingest_runbooks` from the same image. It needs `OPENAI_API_KEY` and nothing
else from the LLM settings: it runs as `LLM_PROVIDER=openai` whatever `.env` says, because it only embeds. Naming the
service auto-activates its `setup` profile, so `--profile setup` is not needed.

A running sre-agent reads the new collection on its next search, because every search opens the collection the
manifest names. No restart is needed. A manual build and the server's own build never overlap: each takes a lock
file (`.index-build.lock`) in the store, so one waits for the other.

Run `sre-ingest` from the same image as `sre-agent` (`docker compose pull` updates both). An older image's runbooks
give a different fingerprint, so the server's next check rebuilds over the manual build.

`--wipe` deletes the whole store before building. Use it only when Chroma cannot open the store at all, and stop
sre-agent first, because it deletes files the running server has open:

```bash
docker compose stop sre-agent
docker compose run --rm sre-ingest python -m scripts.ingest_runbooks --wipe
docker compose up -d sre-agent
```

Arguments after the service name replace its command, so give the whole command. `--wipe` gives up the "a failed
build changes nothing" guarantee: if the build then fails, the store is empty until the next successful one.

## Permissions: the uid 1001 gotcha

Both `sre-agent` and `sre-ingest` run as **uid 1001** (the `app` user in the `Dockerfile`). If the volume was written
by another uid, Chroma fails with `readonly database`. This happened once, when `infra_chroma_data` had been populated
as root (`journal/260504-runbook-search-perms-and-docs-mcp-wiring.md`). Fix on the host:

```bash
sudo chown -R 1001:1001 /var/lib/docker/volumes/infra_chroma_data/_data
docker compose run --rm sre-ingest
```

A fresh, empty volume needs no fix: the first build creates its files as uid 1001.

## Turning auto-rebuild off

Set `RUNBOOK_INDEX_AUTO_REBUILD=false` in `.env` and `docker compose up -d sre-agent`. The loop then only reports,
the gauge still goes to 0 when the index falls behind, and the alert tells you to run `sre-ingest`.
`RUNBOOK_INDEX_CHECK_INTERVAL_S=0` keeps the startup check only.

## Verifying a rebuild

- `in_sync` is 1 and `chunks` is about 292 (it grows with the runbooks).
- The log shows `Index collection runbooks-… is live (N chunks, fingerprint …)`.
- Through the agent or the sre-agent MCP: `runbook_search` for a phrase you know is in a new runbook returns it.

## Related

- `src/agent/retrieval/index.py`: fingerprint, manifest, rebuild, check loop.
- `scripts/ingest_runbooks.py`: the manual entry point.
- home-server `documentation/grafana-alerting.md`, note 11: the alert rule.
- `journal/261112-ingest-auto-rebuild-and-alert.md`: why it works this way (issue #59).
