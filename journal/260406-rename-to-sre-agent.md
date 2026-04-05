# Rename: `sre-assistant` / `homelab-sre` → `sre-agent`

The GitHub repo and local directory were renamed to `sre-agent`. Updated all
outward-facing references so docs, workflows, and docker-compose stacks point
at the new repo URL and GHCR image name.

## What changed

- **CI workflow (`.github/workflows/ci.yml`):** image tag pushed to GHCR is
  now `ghcr.io/johnmathews/sre-agent:latest` (and `:sha-<commit>`). This was
  the one hard-coded string — everything else in that workflow already used
  dynamic refs.
- **`readme.md`:** clone URL (`…/sre-agent.git`), `cd` path, directory-tree
  name at the top of the repo-structure section, and all compose snippets
  (both the inline-embed and the Building-from-Source flows) now reference
  `ghcr.io/johnmathews/sre-agent:latest`. Added a section pointing at the new
  demo compose file.
- **`docker-compose.yml`:** header comment + `build:` fallback comment
  updated to the new image name.
- **`.env.example`, `docs/architecture.md`, `docs/tool-reference.md`:** the
  `claude mcp add --transport http <alias>` examples were updated to use
  `sre-agent` as the alias. The alias is user-chosen so this is only a
  documentation-hygiene change.
- **New `docker-compose.demo.yml`:** a ready-to-run stack that pulls both
  images (sre-agent + sre-webapp) straight from GHCR, so someone evaluating
  the project can spin it up without a source checkout. The existing
  `docker-compose.yml` keeps `build: .` for local development.
- **`.gitignore`:** added `.claude/` for parity with the webapp repo
  (workspace-local Claude Code settings should not be committed).

## What intentionally stayed the same

- Prometheus metric names (`sre_assistant_request_duration_seconds` etc.) —
  renaming these would break continuity of any already-scraped series,
  existing dashboards, and alert rules. Metric names are a wire contract, not
  a branding string.
- Grafana dashboard file name (`dashboards/sre-assistant-sli.json`), its
  UID, and its tag array — changing the UID orphans any embeds or deeplinks.
- `FastMCP("sre-assistant")` in `src/api/mcp_server.py` — this is the
  server-side identity string returned during MCP handshake. Changing it
  would force every already-registered client to re-register.
- `pyproject.toml` name (`homelab-sre-assistant`) — internal Python package
  metadata with no external consumers; leaving it avoids forcing a
  `uv.lock` regeneration just for a cosmetic change.
- Product name "HomeLab SRE Assistant" — the repo name is now `sre-agent`
  but the product name is unchanged. Common pattern.

## Surprise: venv hardcoded the old absolute path

`make test` failed after the directory rename with
`ModuleNotFoundError: No module named 'fastmcp'` — despite `uv run python
-c "import fastmcp"` working fine. The venv's wrapper scripts
(`.venv/bin/pytest`, etc.) had a shebang hardcoded to
`/Users/john/projects/homelab-sre/homelab-sre/.venv/bin/python`, which no
longer existed after the rename. When the kernel couldn't resolve that
interpreter, it fell back to the pyenv-installed Python 3.13.2 outside the
venv, which of course didn't have the project's deps.

Fix: `rm -rf .venv && uv sync --group dev` to regenerate the wrappers with
the correct absolute path. Also cleared `.pytest_cache`, `.mypy_cache`, and
`__pycache__/` dirs which had stale bytecode with the old path. Something
to remember next time the project directory moves.
