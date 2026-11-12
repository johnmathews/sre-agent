# Backend repo renamed: `homelab-sre` → `sre-agent`

The backend repo was renamed from `johnmathews/homelab-sre` to
`johnmathews/sre-agent`. Updated every outward link in this webapp's docs
and comments to point at the new URL.

## What changed

- **`README.md`:** backend link + "Related repos" section now point at
  `johnmathews/sre-agent`. Added a reference to the new demo compose file.
- **`docs/architecture.md`, `docs/api-integration.md`, `docs/deployment.md`,
  `docs/development.md`:** all `homelab-sre` mentions and GitHub links
  updated to `sre-agent`. The ASCII architecture diagram in
  `docs/architecture.md` now labels the backend box `FastAPI (sre-agent)`.
- **`src/api/stream.ts`:** a comment that pointed readers at
  `src/api/main.py in homelab-sre` for the backend event-type list now says
  `in sre-agent`. No runtime change.
- **New `docker-compose.demo.yml`:** ready-to-run stack pulling both images
  (this webapp + sre-agent backend) from GHCR. Useful for someone
  evaluating the webapp without checking out either source repo.

## CI workflow: no change needed

The existing `.github/workflows/ci.yml` was already using dynamic
`${{ github.repository }}` for the image name, so it automatically resolves
to `johnmathews/sre-webapp` (which is unchanged — only the backend repo was
renamed). Nice example of why templating repo-derived strings beats
hardcoding.
