# Request Failure Investigation

## Purpose

Localise a "my request failed" report (page didn't load, upload failed, API returned an error) on `*.itsa-pizza.com` to
the layer that actually rejected or dropped it. Walks the request path bottom-up, since the bottom is cheap to check
and most often holds the answer.

## Architecture

```
client (browser / iOS app)
    │
    ▼
Cloudflare edge                            ← only visible in CF dashboard
    │  (HTTPS)
    ▼
cloudflared (LXC 192.168.2.101, systemd)   ← `journalctl -u cloudflared`
    │  (HTTP, tunnel)
    ▼
traefik (LXC 192.168.2.108, Docker)        ← `journalctl CONTAINER_NAME=traefik`
    │  (HTTP)                                  (also Loki: {service_name="traefik"})
    ▼
origin (per-service; see cloudflared config.yml ingress map)
```

Logging coverage at each layer:

| Layer            | Logs successful requests? | Logs failures?           | Source of truth                       |
| ---------------- | ------------------------- | ------------------------ | ------------------------------------- |
| Origin app       | Usually yes               | Yes                      | per-app log location                  |
| Traefik          | Yes (access log)          | Yes                      | journalctl on traefik LXC, Loki       |
| Cloudflared      | **No**                    | Connection-level only    | journalctl on cloudflared LXC         |
| Cloudflare edge  | Yes                       | Yes                      | Cloudflare dashboard (HTTP Logs)      |

## Investigation flow

Always work bottom-up. Origin first, edge last.

### 1. Origin app

Did the request arrive at the application? Check the app's own log for the user-reported timestamp. Convert timezones
explicitly — user-reported times are usually local; app logs may be UTC or local depending on the service.

- **Yes, app saw the request** → failure is in the app or downstream of it. Stop walking up; pivot to debugging the
  app.
- **No, app didn't see it** → continue.

### 2. Traefik

See `traefik-reverse-proxy` runbook for retrieval. Three outcomes:

- **Traefik logged a 2xx but origin didn't see it** — very rare. Check the `<origin-url>` field in the access-log line
  to confirm traefik tried the right upstream.
- **Traefik logged a 4xx/5xx** — failure is at traefik or the origin returned an error before logging:
  - `502/503/504` — origin unreachable or timed out.
  - `413` — request body too large (traefik or middleware limit).
  - `499` (or absent) — client disconnected mid-request.
- **Traefik has no entry at all** — continue to step 3. **First verify traefik is still emitting logs** (see the
  silently-stops-logging failure mode in the traefik runbook). An empty result is more often a logging gap than a
  missing request.

### 3. Cloudflared

See `cloudflared-tunnel` runbook for retrieval. cloudflared logs only failures, not access. Two outcomes:

- **Error at the timestamp** for the matching `originService=` or hostname — failure is between cloudflared and the
  origin. The error message points at the cause (origin unreachable, stream canceled, request ended abruptly).
- **No error** — does **not** prove success. Either the request didn't reach cloudflared, or it succeeded at the tunnel
  level but failed earlier (a non-2xx that cloudflared considers a normal response). Continue to step 4.

### 4. Cloudflare edge

If steps 1–3 all turned up empty, the only remaining source of truth is the Cloudflare dashboard.

1. Cloudflare dashboard → zone `itsa-pizza.com` → Analytics & Logs → HTTP Logs.
2. Filter by hostname (e.g. `journal-insights.itsa-pizza.com`) and the user-reported timestamp.
3. Look for edge-side 5xx or Cloudflare-specific 1xxx codes.

Common edge-side culprits:

- **520 / 521 / 522** — Cloudflare couldn't reach the origin (tunnel dropped, slow handshake).
- **524** — origin took longer than 100s to respond. Synchronous large uploads (e.g. multi-image OCR) can hit this.
- **413** at the edge — request exceeded plan-limit body size (free plan: 100 MB).
- **WAF / rate-limit block** — shows as a managed-rule challenge or block in the dashboard, not a normal 4xx.

## Symptom → likely cause

| Symptom in logs                                                | Likely cause                                                        |
| -------------------------------------------------------------- | ------------------------------------------------------------------- |
| Origin sees the request, returns 5xx                           | App-level bug; debug the origin                                     |
| Traefik logs 502/504, origin sees nothing                      | Origin crashed, not listening, or slow                              |
| Traefik logs 413, origin sees nothing                          | Body size limit (traefik entrypoint or middleware)                  |
| Cloudflared logs `context canceled` for the right host         | Client disconnected, or origin closed connection mid-response       |
| Nothing anywhere local, dashboard shows 524                    | Origin took >100s to respond; consider async processing             |
| Nothing anywhere local, dashboard shows 520–522                | Tunnel dropped or origin unreachable from cloudflared               |
| Nothing anywhere — including the Cloudflare dashboard          | Client-side failure (mobile network blip, browser memory limit, request never sent) |

## Timezone gotcha

When correlating across layers, every layer reports time differently:

| Source                                | `journalctl` headers     | Timestamps inside log lines  |
| ------------------------------------- | ------------------------ | ---------------------------- |
| Cloudflared LXC                       | host-local (CEST)        | UTC                          |
| Traefik LXC                           | host-local (CEST)        | UTC (in access-log lines)    |
| journal-server (app log)              | host-local (CEST)        | host-local                   |
| journal-webapp (nginx in front of SPA)| n/a (Docker)             | UTC                          |

Always note which timezone you're in when comparing. CEST = UTC+2 in summer, UTC+1 in winter — confirm.

## Tools and where they apply

- **`ssh <host>` + `journalctl`** — works everywhere, slowest to type. Always works as a fallback.
- **Loki via `loki_query_logs`** — fastest for traefik and most Docker workloads. Verify the host is shipped with
  `loki_list_label_values(label="service_name", query='{hostname="<host>"}')` before assuming.
- **Cloudflare dashboard** — only place that shows the edge-to-tunnel hop and CF-specific status codes; not scriptable
  from the SRE agent environment.
- **`docker logs <container>`** — **avoid** on hosts using the `journald` log driver (which is the default on this
  homelab). It returns truncated buffers, not real history.

## Related Services

- `traefik-reverse-proxy` (next hop down from cloudflared, where most observable failures land)
- `cloudflared-tunnel` (next hop up; logs only connection-level errors)
- `loki-logging` (log query reference for `loki_query_logs` calls)
