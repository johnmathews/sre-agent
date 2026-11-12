# Ingest fixed, the server keeps its runbook index current, and an alert covers it

Issue #59. `docker compose run --rm sre-ingest` failed on infra. Nothing had noticed, because nothing ran it.

## What was wrong

- **Ingest asked for an LLM key it never uses.** Settings require `ANTHROPIC_API_KEY` under
  `LLM_PROVIDER=anthropic` (since a7896d6, 2026-02-28). On infra, `sre-agent` gets its key through `environment:`,
  while `sre-ingest` reads only `.env`, which has none. Ingest only embeds, with OpenAI.
- **Nothing ran ingest.** WUD redeploys `sre-agent` but never touches the setup-profile service, and no Ansible handler
  references it. The runbook said one did.
- **Nothing could notice.** `/health` reported `vector_store` healthy whenever `/app/.chroma_db` existed, empty or
  months stale. No metric or rule covered the store.
- **A manual rebuild needed a restart.** The script wiped the store's files under the running process, whose cached
  Chroma client then served the old index.

## What changed

- **Ingest runs as `LLM_PROVIDER=openai`.** The openai provider's validation asks for `OPENAI_API_KEY`, the one key
  ingest needs. This was chosen over a placeholder key in the infra compose template, which would have left the trap
  for the next required setting.
- **The store records what it was built from.** `index-manifest.json` holds a SHA-256 over every indexed file plus
  the chunk size, overlap and embedding model, and names the live collection.
- **Rebuilds swap in a new collection** (`runbooks-<fingerprint12>-<build id>`), then rewrite the manifest, then delete
  the old collections.
  - A failed build changes nothing.
  - A search during a build reads the old collection. A search that read the manifest just before the old collection
    was deleted retries once, which a threaded test caught (1 error in 42 searches before the retry).
  - The build id makes every build's name unique, so rebuilding unchanged sources never deletes the live collection
    first.
- **sre-agent checks the index** at startup and every 600 s in a background task, and rebuilds when it is out of
  date. It was chosen over running `sre-ingest` from WUD or Ansible, because the reader owns the index: no second
  container's settings, no trigger to wire, no restart.
- **Signals.**
  - `sre_assistant_runbook_index_in_sync`, `_chunks` and `_built_timestamp_seconds` are read from the store on each
    scrape (one read per 5 s), plus `_rebuilds_total{status}`.
  - `/health` `vector_store` is now unhealthy when the store is missing or empty. An out-of-date index stays healthy
    with a detail line, because staleness belongs to the alert rather than the availability SLI.
  - Grafana "SRE agent on Infra VM: runbook index out of date" pages when `in_sync` stays below 1 for 30 minutes
    (home-server `documentation/grafana-alerting.md`, note 11).

## Checked

- With real OpenAI embeddings and the 20 runbooks, into a temp store: one rebuild, 292 chunks (the count infra
  got by hand), `in_sync` 0 → 1, and a search for "how do I rebuild the vector store" returns this runbook.
- The live proof on infra (ingest with no flags, auto-rebuild on deploy, a real page and its resolution) is recorded
  in home-server's journal entry for the alert rule, which lands after it.
