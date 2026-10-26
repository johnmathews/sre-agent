# Upgrade Advisor, Part C: from 2 to 9 watched images — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: superpowers:subagent-driven-development. Steps use `- [ ]`.

**Goal:** The advisor also watches blackbox_exporter, Traefik, Loki, Dozzle, Uptime Kuma, gluetun and
Navidrome, with no change to anything running at rollout: the advisor, not the rollout, proposes the
catch-up upgrades.

**Spec:** `docs/superpowers/specs/2026-10-26-upgrade-advisor-design.md` (unchanged rules); Parts A and B
plans and journal entries for current behaviour.

## Global Constraints

- Digest watching of one moving tag per container; WUD never deploys an advised container on its own.
- Watched tags (decided 2026-10-26 from the registries): blackbox `v0`, traefik `v3`, loki `3`,
  dozzle `v11`, uptime-kuma `2`, gluetun `latest`, navidrome `latest`. Each also gets
  `wud.tag.include: "^<tag>$"` so WUD never offers a tag change.
- Release-tag mapping: `{release}` (release tag as is) for blackbox, traefik, dozzle, gluetun,
  uptime-kuma; `{version}` (release tag without a leading `v`) for loki and navidrome.
- Hosts that already run an auto-deploy WUD (infra, media) get a SECOND compose trigger,
  `dockercompose.advise` with `AUTO=false`; advised containers include only that trigger. The
  advisor triggers `/api/containers/{id}/triggers/dockercompose/{wud_trigger}` with the per-entry
  trigger name (`deploy` on advise-only hosts, `advise` on infra/media).
- Infra: sre-agent and WUD share the compose project, so the advisor uses `http://wud:3000`; WUD's
  API is published on infra only as `127.0.0.1:3010:3000` (for Ansible's user creation), never on
  the LAN, and needs no firewall unit.
- Media, Traefik LXC, Music LXC, Prometheus LXC: API on `0.0.0.0:3010` behind the existing
  `wud_advise` DOCKER-USER firewall (infra VM only).
- PBS guests: infra VM 106, traefik CT 108, music CT 109, media VM 114, prometheus CT 115.
- Public sre-agent repo: placeholders only in code, tests and docs.
- `make check` (sre-agent) and `make ci-offline` (home-server) pass after every task.

## Task C1 (sre-agent): `{version}` placeholder and per-entry trigger name

**Files:** `src/upgrades/config.py`, `src/upgrades/releases.py`, `src/upgrades/wud.py`,
`src/upgrades/deploy.py`, their tests, `docs/architecture.md`, `.env.example` (if it shows the
allowlist), journal entry.

- `release_tag_format` accepts exactly one replacement field, `{release}` or `{version}`; nothing
  else, no format specs (keep the existing nested-spec rejection). `release_for_digest` formats with
  `release=tag, version=tag[1:] if tag.startswith("v") else tag`.
- `AdvisedContainer.wud_trigger: str = "deploy"`, pattern `^[a-z0-9][a-z0-9-]{0,31}$`.
- `wud.trigger_deploy` posts to `.../triggers/dockercompose/{quote(c.wud_trigger, safe="")}`.
- `wud_url` must accept a bare service hostname such as `http://wud:3000` (check the existing
  validator; add a test).
- Tests: `{version}` maps `v3.7.9` → `3.7.9` and leaves `2.5.8` alone; an unknown field / two
  fields / `{version:x}` rejected; trigger path uses `advise` when set and `deploy` by default;
  `http://wud:3000` accepted.

## Task C2 (home-server): seven more advised containers

**Files:** `roles/wud_advise/*`, `roles/prometheus_lxc/*`, `roles/traefik_lxc/*`,
`roles/music_lxc/*`, `roles/infra_vm/*`, `roles/media_vm/*`, `tests/test_wud_advise.py`,
`tests/test_wud_autodeploy.py` (infra/media now also carry advised containers),
`tests/test_sre_upgrade_advisor_wiring.py`, docs (`wud.md` §7, `traefik.md`, `navidrome.md`,
`infra_vm.md`, `media_vm.md`, `prometheus_lxc.md`, `CLAUDE.md` Deployment Model bullet), journal.

- `wud_advise` gains switches so it can serve hosts that already run WUD: e.g.
  `wud_advise_manage_env` (default true; false leaves the host's own `wud.env`/store alone) and
  `wud_advise_firewall` (default true). Keep Part A behaviour unchanged for prometheus/open-webui.
- Prometheus LXC: blackbox gets the advise labels; `prometheus_lxc_blackbox_exporter_version: "v0"`.
- Traefik LXC and Music LXC: watch-only WUD exactly like the Prometheus host (service block,
  role include before the converge, `user.yml` after it, check-mode guard, `| bool` switches);
  `traefik_lxc_traefik_version: "v3"` (introduce the variable; the template hard-codes `v3.1`
  today); `music_lxc_navidrome_version: "latest"`, rewriting the role's "pinned, never latest"
  comment to explain that nothing pulls or deploys without the advisor's approval now.
- Infra VM: the existing WUD gets `WUD_TRIGGER_DOCKERCOMPOSE_ADVISE_AUTO=false` (+ PRUNE=true);
  loki/dozzle/uptime-kuma get labels including only `dockercompose.advise`, versions `3`, `v11`, `2`;
  publish `127.0.0.1:3010:3000`; create the `sre-advisor` rw user via `wud_advise` `user.yml`
  (no firewall, no env from the role). Confirm sre-agent and wud share a compose network.
- Media VM: the existing WUD gets the same advise trigger; gluetun gets labels (`latest`); publish
  `0.0.0.0:3010:3000` with the `wud_advise` firewall; rw user via `user.yml`.
- `infra_vm_upgrade_advisor_containers`: seven new entries (wud_url, wud_trigger,
  release_tag_format, readiness_url reachable FROM the infra VM's sre-agent container, pbs_guest,
  context_sources = compose template + main config template where one exists). Where a container
  publishes no reachable health endpoint, choose the closest reachable signal and say so in the doc.
- Tests extend the existing parametrised suites; the auto-deploy tests must still prove the own
  apps are unchanged and that no advised container includes `dockercompose.deploy` on infra/media.

## Rollout (controller, after both merge)

On each host, alias the running image to the new watch tag (`docker tag <current> <repo>:<tag>`)
so the converge changes nothing that runs; converge; live checks as in Part A; extend the Grafana
stale-image exclusion; journal. The advisor proposes the catch-ups after its 72 h first-seen gate.
