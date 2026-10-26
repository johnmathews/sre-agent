# Upgrade Advisor: more images (task C1)

Two allowlist additions so the advisor can cover images beyond the first one:

- `release_tag_format` now accepts `{version}` (release tag without a leading `v`) as well as `{release}`. Exactly one
  field is allowed; format specs and conversions (`{version:x}`, `{release!r}`) are rejected, as are nested specs.
- `AdvisedContainer.wud_trigger` (default `deploy`) selects the WUD docker-compose trigger the deploy posts to. Hosts
  that already run an auto-deploying WUD get a second advise-only trigger, and the advisor names it per entry.

`wud_url` already accepted a bare service hostname (`http://wud:3000`); a test now pins that. Tests were written first
and failed before the implementation.

## Follow-up: releases scoped to the watched major

`release_for_digest` looked at the 10 newest releases only. Uptime Kuma is watched on tag `1`, but its newest releases
are all 2.x, so a 1.x candidate never matched and was held as an "unreleased build" forever. When the container's
watched tag is major-only (`^v?\d+$`) the releases are now filtered to that major before the cap and ordering, for both
the running and candidate lookups. Other tags are unchanged. `list_releases` now asks GitHub for 100 per page (its max)
instead of 30, with no pagination. Tests first, failing before the change.
