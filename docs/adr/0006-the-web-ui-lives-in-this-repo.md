# ADR-0006: The web UI lives in this repo under `web/`

**Status:** Accepted. **Date:** 2026-11-12.

## 1. Context

- The Vue web UI lived in its own repo, `johnmathews/sre-webapp`. It talks only to this server: it calls
  `/ask/stream`, `/conversations*` and `/health`, and its nginx only serves the SPA and proxies `/api/` here.
- Features already crossed the boundary. `tool_end` events (April 2026) and the structured SSE error contract (July
  2026) each needed a server change and a UI change, landed as separate PRs in two repos.
- Nothing tested the UI's side of the contract against the server. A server change could break the UI and pass CI.
- The two already deployed as one unit: the home-server makefile upgrades `sre-agent` and `sre-webapp` together.

## 2. Decision

The web UI lives in this repo under `web/`, with its git history imported from `johnmathews/sre-webapp`
(`git filter-repo --to-subdirectory-filter web`, merged with a merge commit so the history survives). Its docs are
in `docs/web/` and its journal entries in `journal/`. `johnmathews/sre-webapp` is archived.

## 3. Consequences

1. Each side keeps its own image and name: `ghcr.io/johnmathews/sre-agent` from the repo root and
   `ghcr.io/johnmathews/sre-webapp` from `web/`. Infra, WUD and the compose files on the infra VM did not change.
2. `.github/workflows/ci.yml` filters on paths. A change under `web/` runs `web-test` (typecheck and Playwright) and
   publishes the web image. A change to the server's files publishes the server image. `check` runs on every change.
   Before this ADR every push to `main` redeployed the server; a docs-only or UI-only push now leaves it running, and
   `/mcp` stays up.
3. `tests/test_web_contract.py` checks the UI's TypeScript types against the server's models and SSE events. A change
   to either side that breaks the other fails `check`.
4. A change to an endpoint the UI uses lands with its UI change in one PR.
5. Two toolchains share the repo: uv for Python at the root, npm for the UI in `web/`. `web/CLAUDE.md` holds the UI's
   own conventions.
