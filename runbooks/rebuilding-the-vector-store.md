# Rebuilding the Chroma vector store

> **Use when:** you've added or edited a runbook (or anything in
> `EXTRA_DOCS_DIRS`) and want it to appear in the agent's RAG search;
> the agent's runbook search returns nothing or stale results; you're
> setting the stack up on a new host for the first time; or a fresh
> deploy hits a `readonly database` error from Chroma.

The sre-agent's runbook search is backed by a Chroma vector store at
`/app/.chroma_db` inside the container, mounted from the
`chroma_data` Docker volume. The store is **rebuilt by hand on
demand** — there is no live re-index. New runbooks aren't searchable
until you run an ingest.

## TL;DR

```bash
# Routine rebuild (re-indexes all configured sources)
docker compose run --rm sre-ingest
```

That's it for the common case. The `--profile setup` flag is *not*
required — naming the service explicitly auto-activates its profile
for that one invocation. Read the rest of this runbook only if the
common case fails or you're doing something unusual.

## What gets indexed

`scripts/ingest_runbooks.py` calls `load_all_documents()` from
`src/agent/retrieval/embeddings.py`. The corpus is:

1. Everything in `runbooks/` inside the repo (these get baked into
   the image at build time via
   `COPY --from=builder --chown=1001:1001 /app/runbooks/ runbooks/`).
2. Plus any directories listed in the `EXTRA_DOCS_DIRS` env var
   (colon-separated). Used in production to mount
   `/path/to/ansible-home-server` into the container so Ansible
   playbooks/inventories are searchable too.

Whatever exists *on disk inside the container at ingest time* gets
indexed. Files added to the host repo after the image was built
are visible only if they're in a bind-mount or a directory listed
in `EXTRA_DOCS_DIRS`.

## Runtime variants

| Where you are | Command |
|---|---|
| Inside the repo, on the host, no Docker | `make ingest` (= `uv run python -m scripts.ingest_runbooks`) — writes to a host-local `.chroma_db/` directory; **does not** touch the container's volume. |
| Local dev with the docker-compose stack | `docker compose run --rm sre-ingest` — runs the setup-profile container, writes to the `chroma_data` volume that `sre-agent` reads. |
| Production (`infra` VM) | Same: `docker compose run --rm sre-ingest` from the deploy directory. The Ansible role for sre-agent uses this in its `restart` handler when runbooks change. |

The host-local `.chroma_db/` and the container volume `chroma_data`
are *separate stores*. If you `make ingest` on the host then start
the container, the container will still see only what was previously
in its volume. This is a frequent source of "I added the runbook,
why doesn't the agent see it?" — the answer is almost always "you
ran the wrong variant for your runtime".

## What the script actually does

`scripts/ingest_runbooks.py:21-32` — clears the existing store first:

```python
if CHROMA_PERSIST_DIR.exists():
    for child in CHROMA_PERSIST_DIR.iterdir():
        if child.is_dir():
            shutil.rmtree(child)
        else:
            child.unlink()
```

Note the *contents* are deleted, not the directory itself — when
running in Docker, the directory is a volume mountpoint and removing
it would fail with `EBUSY`. After clearing, `build_vector_store()`
re-embeds everything and persists. The rebuild is destructive — there
is no incremental ingest path. Cost is minor (low hundreds of small
markdown files) but worth knowing.

## Permissions: the uid 1001 gotcha

Both `sre-agent` and `sre-ingest` run as **uid 1001** (the `app`
user defined in `Dockerfile:33`, with `USER 1001:1001` set as the
runtime default). Both share the `chroma_data` volume.

If the volume on the host was created or written-to by a different
uid, Chroma fails at runtime with `readonly database`. This bit us
once (see `journal/260504-runbook-search-perms-and-docs-mcp-wiring.md`)
when the production volume `infra_chroma_data` had been populated
under root.

**Fix on the prod host (one-shot):**

```bash
sudo chown -R 1001:1001 /var/lib/docker/volumes/infra_chroma_data/_data
docker compose run --rm sre-ingest
docker compose up -d
```

Local docker-compose volumes are usually owned by your host user
(uid 1000 on most Linux distros) and the same fix applies — use
the actual volume path from `docker volume inspect <name>`.

This is **only** an issue when:

1. The volume already has files on it.
2. Those files were created by a uid that isn't 1001.
3. You're running `sre-ingest` (or `sre-agent`) for the first time
   after a Docker / volume / uid change.

A fresh empty volume mounts cleanly without intervention because
the ingest creates files itself, owned by uid 1001.

## Verifying the rebuild

After the ingest container exits cleanly, verify:

```bash
# Confirm something was written.
docker run --rm -v <project>_chroma_data:/data alpine \
  ls -la /data | head

# Or, more usefully, ask the agent to search a runbook you know exists.
# From the API:
curl -s -X POST http://localhost:8000/ask/stream \
  -H 'Content-Type: application/json' \
  -d '{"question":"What is in the disk-management runbook?"}' \
  | tail -20
```

Or from the SRE agent webapp, ask a question whose answer is in a
runbook you just added. If the agent's response cites the new
runbook content, ingest worked. If it answers without citing
anything new, the runbook either wasn't in the indexed paths or
the ingest didn't re-run.

## Adding `EXTRA_DOCS_DIRS` for the first time

In `.env`:

```bash
EXTRA_DOCS_DIRS=/app/ansible-home-server:/app/some-other-docs
```

In `docker-compose.yml`, mount the host directories so the paths
exist inside the container:

```yaml
sre-agent:
  volumes:
    - chroma_data:/app/.chroma_db
    - /home/john/projects/ansible-home-server:/app/ansible-home-server:ro
sre-ingest:
  volumes:
    - chroma_data:/app/.chroma_db
    - /home/john/projects/ansible-home-server:/app/ansible-home-server:ro
```

Then rebuild: `docker compose run --rm sre-ingest`.

The mount must exist on **both** services — `sre-ingest` to read
the docs at index time, `sre-agent` because the retriever stores
absolute paths and may attempt to re-read source files when
producing citations.

## Common mistakes

1. **Ran `make ingest` on the host expecting the container to see
   it.** Different stores. Use `docker compose run --rm sre-ingest`
   for the container variant.
2. **Forgot the new doc isn't baked into the image.** Files in
   `runbooks/` are copied at image build time; rebuilding the
   *vector store* doesn't rebuild the *image*. If you added the
   doc to the repo and rebuilt the image (`docker compose build`),
   then ran ingest, you're fine. If you only edited a file inside
   the running container, that's lost on next pull.
3. **Set `EXTRA_DOCS_DIRS` but didn't mount the path into the
   `sre-ingest` container.** The path exists on the host, but the
   ingest container can't see it — `load_all_documents()` silently
   skips missing dirs. Add the volume mount on `sre-ingest`, not
   just on `sre-agent`.
4. **Ran ingest while `sre-agent` was actively serving requests.**
   The ingest container clears the store before re-populating it.
   Concurrent `sre-agent` reads during the clear-then-rebuild window
   can fail or return empty results. Either bring the agent down
   first (`docker compose stop sre-agent`) or accept a few-second
   blip during rebuild.

## Related

- `Makefile` — the `ingest` target and the host-side variant.
- `scripts/ingest_runbooks.py` — the actual entry point.
- `src/agent/retrieval/embeddings.py` —
  `load_all_documents()` and the `EXTRA_DOCS_DIRS` parsing
  (line 145+).
- `Dockerfile:33-46` — the uid 1001 setup.
- `docker-compose.yml` `sre-ingest` service — profile, env, volumes.
- `journal/260504-runbook-search-perms-and-docs-mcp-wiring.md` —
  the original incident that motivated the uid-1001 fix and this
  runbook.
