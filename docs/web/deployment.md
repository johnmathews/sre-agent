# Deployment

The frontend runs as a multi-stage Docker image: a Node 22 builder stage
compiles the Vue SPA, and an nginx 1.27 runtime stage serves the static
bundle and reverse-proxies `/api/*` to the FastAPI backend.

## Image

`ghcr.io/johnmathews/sre-webapp`, built from `web/` for `linux/amd64` by the
`web-build` job in `.github/workflows/ci.yml`. It runs on a push to `main`
that changed `web/`, after `web-test` (typecheck and Playwright) passes.
WUD on the infra VM redeploys `sre-webapp` when `latest` moves.

Tags published:

| Tag                | Meaning                                         |
|--------------------|-------------------------------------------------|
| `latest`           | Most recent push to `main` that changed `web/`  |
| `sha-<commit>`     | Exact commit, full SHA                          |

The image's `org.opencontainers.image.source` label points at
`johnmathews/sre-agent`, and `revision` at the commit.

## Configuration

Single environment variable:

| Variable       | Default                   | Purpose                            |
|----------------|---------------------------|------------------------------------|
| `API_UPSTREAM` | `http://sre-api:8000`     | Where nginx proxies `/api/*`       |

`API_UPSTREAM` is substituted into the nginx config at container start via
nginx's built-in `envsubst` entrypoint.

nginx resolves the upstream lazily (per-request) using Docker's embedded
DNS at `127.0.0.11`, so the webapp container starts cleanly even if the
backend isn't up yet.

## docker-compose

See [`docker-compose.demo.yml`](../../docker-compose.demo.yml) at the repo
root for a ready-to-run stack (webapp + sre-agent backend) using pre-built
GHCR images — nothing to build locally. [`docker-compose.yml`](../../docker-compose.yml)
builds both images from source.

Or add it to an existing stack:

```yaml
services:
  sre-webapp:
    image: ghcr.io/johnmathews/sre-webapp:latest
    ports:
      - "8080:80"
    environment:
      - API_UPSTREAM=http://sre-api:8000
    depends_on:
      sre-api:
        condition: service_healthy
    restart: unless-stopped
    mem_limit: 64m
```

The service must share a docker network with `sre-api` so `sre-api:8000`
resolves. The default `docker-compose.yml` user-defined network is fine.

## Running outside docker-compose

If you run the image somewhere without Docker's embedded DNS, edit
`docker/nginx.conf.template` and replace `127.0.0.11` with an appropriate
resolver (e.g. `8.8.8.8` for public DNS, or your host's resolver).

## Health check

The image exposes `HEALTHCHECK` that hits `http://localhost/` every 30s.
No dedicated `/health` endpoint — nginx serving `index.html` is the signal
that the image is alive.
