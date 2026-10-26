# Upgrade Advisor: more images (task C1)

Two allowlist additions so the advisor can cover images beyond the first one:

- `release_tag_format` now accepts `{version}` (release tag without a leading `v`) as well as `{release}`. Exactly one
  field is allowed; format specs and conversions (`{version:x}`, `{release!r}`) are rejected, as are nested specs.
- `AdvisedContainer.wud_trigger` (default `deploy`) selects the WUD docker-compose trigger the deploy posts to. Hosts
  that already run an auto-deploying WUD get a second advise-only trigger, and the advisor names it per entry.

`wud_url` already accepted a bare service hostname (`http://wud:3000`); a test now pins that. Tests were written first
and failed before the implementation.
