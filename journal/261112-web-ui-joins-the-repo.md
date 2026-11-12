# The web UI joins this repo

> Purpose
>
> Records why and how `johnmathews/sre-webapp` moved into `web/`, and what changed for CI, docs and deploys.

## 1. Why

- The UI talks only to this server, and features kept crossing the boundary: `tool_end` events (April) and the
  structured SSE error contract (July) each landed as two PRs in two repos.
- Nothing tested the UI's side of the contract, so server work could break the UI and still pass CI.
- The owner stated on 2026-11-12 that the UI is still in use and must be a first-class interface. ADR-0001 had called
  it secondary.

ADR-0006 records the move and ADR-0007 the first-class status. ADR-0007 supersedes ADR-0001.

## 2. How the history came across

- `git filter-repo --to-subdirectory-filter web` on a fresh clone of sre-webapp at `a8b3823` (37 commits), then
  `git merge --allow-unrelated-histories`. `git log --follow web/src/App.vue` reaches the initial scaffold.
- The PR lands with a **merge commit**, not the usual squash. A squash would flatten the imported commits into one.
- `web/docs` moved to `docs/web`, and `web/journal/*` to `journal/`. The webapp's `260722-actionable-agent-errors.md`
  collided with this repo's entry of the same name, so it became `260722-actionable-agent-errors-webapp.md`.
- `web/.github` went (replaced by root CI jobs) and `web/docker-compose.yml` went (a copy of `docker-compose.demo.yml`).
- `web/CLAUDE.md` stays as a nested file: it holds the UI's iOS PWA rules, and Claude Code loads it for work in `web/`.

## 3. CI and deploys

- A `changes` job (`dorny/paths-filter`) splits each push into the server's files and `web/`. `check` still runs on
  every change, since `tests/test_web_contract.py` and the docs tests read files outside `src/`.
- Both image names stay, so the infra VM, WUD and the home-server makefile need no change.
- Behaviour change: before, every push to `main` rebuilt and redeployed the server, dropping `/mcp` for about 20 s. Now
  a docs-only or UI-only push leaves the server running.
- The `sre-webapp` GHCR package was created by the old repo's workflow and is linked to it. `sre-agent`'s
  `GITHUB_TOKEN` needs write access granted in the package's settings ("Manage Actions access") before `web-build` can
  push.

## 4. The contract test

`tests/test_web_contract.py` parses the TypeScript interfaces in `web/src/api/` with regexes and compares them with the
Pydantic models, the stream event literals in the agent code, and `AgentErrorReason`. Each check was seen to fail on a
deliberate break: a dropped `ConversationSummary.turn_count`, an extra `StreamEventType`, a misspelt reason in
`ErrorBubble.vue`, and an optional `question` in `StreamRequest`.

Limits: the regex parser reads `export interface` blocks and string-literal unions only. A type written another way
(a `type X = { ... }` alias, generics) is not seen, and `test_every_mapped_interface_exists_in_the_ui` fails rather
than passing silently when a mapped interface disappears.

## 5. Left open

- `docs/web/runbooks/triaging-webapp-stream-failures.md` is outside `runbooks/`, so RAG ingest does not load it.
