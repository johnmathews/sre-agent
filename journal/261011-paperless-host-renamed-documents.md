# Paperless host renamed to documents (261011)

## What changed in the homelab

- LXC CT 117 was renamed from `paperless` to `documents`. Its Prometheus and
  Loki `hostname` label is now `documents` (data before 2026-10-11 stays under
  `paperless`). SSH aliases are now `documents` / `documentst`.
- Paperless-ngx is gone; the host runs the `library` document app
  (`library-webserver`, `library-worker`, `library-db`, `library-embedder`).
- The TrueNAS `tank/paperless` dataset and its NFS/SMB shares were deleted.
  The library app uses `tank/document-store`.

## Changes here

- `prometheus_instant_query` description: managed host list now says
  `documents` instead of `paperless`, so the agent filters on the live label.
- `truenas_list_shares` description and `docs/tool-reference.md`: example
  question uses the `document-store` share.
- Runbooks: SSH alias table (`systemd-services.md`) and the container-memory
  example question (`grafana-home-server-dashboard.md`) updated.

Historical queries for CT 117 before 2026-10-11 need `hostname="paperless"`.
