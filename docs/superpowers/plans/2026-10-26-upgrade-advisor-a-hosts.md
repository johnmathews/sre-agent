# Upgrade Advisor, Part A: Hosts (home-server repo) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Run WUD in watch-only mode on the Prometheus and Open WebUI LXCs, move both apps to the
tags the advisor watches, and wire the SRE agent on the infra VM to those WUDs.

**Architecture:** A small shared role, `wud_advise`, owns the host-side pieces (secrets file,
store dir, the WUD `rw` user, a firewall unit that admits only the infra VM to WUD's API). Each
app role adds a `wud` service and the opt-in labels to its own compose template, behind a
`<role>_wud_advise` switch. The infra VM role renders the advisor's allowlist, copies the context
files the model reads, and passes the advisor's env to `sre-agent`.

**Tech Stack:** Ansible, Docker Compose v2, WUD 9.6.0 (`sidecar_wud_version`), iptables
`DOCKER-USER`, pytest + Jinja for template tests.

**Spec:** `docs/superpowers/specs/2026-10-26-upgrade-advisor-design.md` (sre-agent repo).
Companion plan: `2026-10-26-upgrade-advisor-b-advisor.md` (the advisor code).

**Repo:** every path below is relative to the home-server repo (`proxmox-setup`), not sre-agent.
Read `documentation/wud.md` and `CLAUDE.md` there first. Fresh worktree: symlink `.venv` and
`.vault_pass.txt` from the main checkout before any make target.

## Global Constraints

- WUD watches **moving tags by digest only** (`wud.watch.digest: "true"`); never a semver tag.
- Deploy trigger is `dockercompose.deploy` with **`WUD_TRIGGER_DOCKERCOMPOSE_DEPLOY_AUTO=false`**.
- WUD's API is published on host port **3010** and reachable from the infra VM only.
- The advisor's WUD user is `sre-advisor` with role **`rw`**; password `vault_wud_sre_advisor_password`.
- Prometheus watches `prom/prometheus:v3`; Open WebUI watches `ghcr.io/open-webui/open-webui:latest`.
- Open WebUI's switch off `main` is a manual step **before** WUD lands on that host (Task 3).
- No secret enters `.env` on the infra VM (apps load it whole); the advisor's secrets go in
  `sre-upgrades.env`, read only by `sre-agent`.
- Every role variable carries its role prefix; task names in sentence case; `mode:` on every file.
- `make ci-offline` passes after every task.

## Interfaces shared with Part B (the contract)

Env read by `sre-agent` (Part B's `Settings`):

```
UPGRADE_ADVISOR_CONFIG_PATH=/app/upgrades/allowlist.yml
UPGRADE_ADVISOR_DB_PATH=/app/upgrades/state/advisor.db
UPGRADE_ADVISOR_CRON=0 7 * * *
UPGRADE_ADVISOR_MIN_AGE_HOURS=72
UPGRADE_ADVISOR_PUBLIC_BASE_URL=https://sre.<primary_domain_name>/api
UPGRADE_ADVISOR_VERIFY_DELAY_SECONDS=300
UPGRADE_ADVISOR_WUD_USERNAME=sre-advisor
UPGRADE_ADVISOR_WUD_PASSWORD=<vault_wud_sre_advisor_password>
CF_ACCESS_TEAM_DOMAIN=<team>.cloudflareaccess.com
CF_ACCESS_AUD=<Access application AUD tag>
UPGRADE_ADVISOR_GITHUB_TOKEN=<optional; empty = anonymous>
```

Allowlist file (`/app/upgrades/allowlist.yml` in the container), one entry per container:

```yaml
containers:
  - name: prometheus                 # WUD/Docker container name
    host: prometheus                 # shown in emails
    wud_url: http://<host ip>:3010
    registry: registry-1.docker.io
    repository: prom/prometheus
    tag: v3
    github_repo: prometheus/prometheus
    release_tag_format: "{release}"  # GitHub release tag -> image tag
    readiness_url: http://<host ip>:9090/-/ready
    pbs_guest: "CT 901"
    context_files:
      - /app/upgrades/context/prometheus/docker-compose.yml.j2
      - /app/upgrades/context/prometheus/prometheus.yml.j2
    auto_deploy: false
```

## Review Focus

- **WUD API reachable from another LAN host:** a request from anywhere but the infra VM to
  `:3010` must time out (Task 1 live check from the Mac).
- **WUD restarted after a compose change loses the `rw` user?** Users live in WUD's DB under
  `/store`; the store dir must be a persistent bind mount (Task 1 test asserts it).
- **Docker restart:** the firewall rule must come back after `systemctl restart docker` and a
  reboot (Task 1 live check).
- **Turning advise mode off** must remove the service, the labels, `wud.env` and the firewall
  unit (Task 1 test + task).
- **A `v3` / `latest` switch that is secretly a downgrade:** verify digests before converging
  (Tasks 2 and 3 steps).

---

### Task 1: `wud_advise` role and Prometheus in advise mode

**Files:**
- Create: `roles/wud_advise/defaults/main.yml`, `roles/wud_advise/tasks/main.yml`,
  `roles/wud_advise/tasks/user.yml`, `roles/wud_advise/templates/wud.env.j2`,
  `roles/wud_advise/templates/wud-api-firewall.sh.j2`,
  `roles/wud_advise/templates/wud-api-firewall.service.j2`
- Modify: `roles/prometheus_lxc/defaults/main.yml`, `roles/prometheus_lxc/tasks/main.yml`,
  `roles/prometheus_lxc/handlers/main.yml`, `roles/prometheus_lxc/templates/docker-compose.yml.j2`
- Modify: `group_vars/all/vault.yml` (encrypted: add `vault_wud_sre_advisor_password`)
- Test: `tests/test_wud_advise.py`

**Interfaces:**
- Produces: role `wud_advise` with inputs `wud_advise_enabled: bool`,
  `wud_advise_compose_dir: str`, `wud_advise_api_port: int` (default `3010`),
  `wud_advise_allowed_source: str` (default `"{{ hostvars['infra-vm'].ansible_host }}"`).
  `tasks/user.yml` is included by app roles **after** their compose converge.
- Produces: handler name `Restart wud` in each app role (WUD service only, `pull: missing`).
- Produces: per-role switch `prometheus_lxc_wud_advise: true`.

- [ ] **Step 1: Write the failing tests** in `tests/test_wud_advise.py`. Reuse the renderer:
  `from tests.test_wud_autodeploy import compose, labels, env, render` (add an empty
  `tests/__init__.py` if the import fails). Parametrize over
  `ADVISE = {"prometheus_lxc": ("prometheus_lxc_wud_advise", "prometheus")}` (Task 3 adds
  open-webui).

```python
def test_advised_service_is_watched_by_digest_without_auto_deploy(role): ...
    # svc labels == {"wud.watch": "true", "wud.watch.digest": "true",
    #                "wud.display.name": <non-empty>, "wud.trigger.include": "dockercompose.deploy"}
    # wud env: WUD_WATCHER_LOCAL_WATCHBYDEFAULT == "false",
    #          WUD_TRIGGER_DOCKERCOMPOSE_DEPLOY_AUTO == "false",
    #          WUD_WATCHER_LOCAL_CRON == "0 * * * *"
def test_wud_api_published_on_3010_with_persistent_store(role): ...
    # "3010:3000" in wud ports; a volume ending ":/store" whose source starts with "/srv/"
    # compose dir mounted at the same path inside and out; docker.sock mounted
def test_nothing_else_on_the_host_is_watched(role): ...
    # no service except the advised one and none of the sidecars carries a wud.* label
def test_advise_switch_off_removes_wud_and_labels(role): ...
    # compose(role, **{switch: False}): no "wud" service, advised service has no wud.* labels
def test_wud_api_firewall_admits_only_the_infra_vm(): ...
    # render roles/wud_advise/templates/wud-api-firewall.sh.j2 with
    # wud_advise_api_port=3010, wud_advise_allowed_source="10.9.9.9":
    # contains "--ctorigdstport 3010", "! -s 10.9.9.9", "-j DROP", "DOCKER-USER", and
    # an "iptables -C" check before "iptables -I" (idempotent)
def test_wud_env_holds_only_the_bootstrap_admin(): ...
    # render wud.env.j2 with vault_wud_admin_password="placeholder-value": keys ==
    # {"WUD_AUTH_ADMIN_USER", "WUD_AUTH_ADMIN_PASSWORD"}
def test_prometheus_watches_v3(): ...
    # roles/prometheus_lxc/defaults/main.yml: prometheus_lxc_prometheus_version == "v3"
```

- [ ] **Step 2: Run them to verify they fail**

Run: `uv run pytest tests/test_wud_advise.py -v`
Expected: FAIL (missing role files, no `wud` service, version `latest`).

- [ ] **Step 3: Write the `wud_advise` role**
  - `tasks/main.yml`, all tagged `wud`: store dir `{{ wud_advise_compose_dir }}/wud` (0750);
    `wud.env` from template (0600, `no_log: true`, notify `Restart wud`); firewall script to
    `/usr/local/sbin/wud-api-firewall` (0755) and unit to
    `/etc/systemd/system/wud-api-firewall.service` (0644), then `systemd: enabled, started,
    daemon_reload`. Each with `when: wud_advise_enabled`, and a matching `state: absent` /
    `enabled: false` task `when: not wud_advise_enabled` (unit stopped first, which removes the
    rule via `ExecStop`).
  - Firewall script: `start` inserts, `stop` deletes, one rule:
    `iptables {-C|-I|-D} DOCKER-USER -p tcp -m conntrack --ctorigdstport {{ port }} --ctdir ORIGINAL ! -s {{ source }} -j DROP`,
    with `-C` guarding `-I`. Loopback traffic (Ansible's own API calls) goes through
    docker-proxy, not FORWARD, so it is not affected.
  - Unit: `Type=oneshot`, `RemainAfterExit=yes`, `After=docker.service`, `PartOf=docker.service`,
    `WantedBy=docker.service multi-user.target`, `ExecStart=... start`, `ExecStop=... stop`.
  - `tasks/user.yml`: with `ansible.builtin.uri` against `http://127.0.0.1:{{ port }}`, wait for
    `/health` (retries 30, delay 2); `GET /api/containers` with `sre-advisor` basic auth; on 401,
    `GET /api/users` as `admin` and either `POST /api/users`
    `{username: sre-advisor, password: ..., role: rw}` (user missing) or
    `PUT /api/users/<id>` `{password: ...}` (password drifted). `no_log: true` on every call with a
    password; `changed_when` true only on POST/PUT.
- [ ] **Step 4: Wire Prometheus.** Defaults: `prometheus_lxc_prometheus_version: "v3"`,
  `prometheus_lxc_wud_advise: true`. Compose: under `{% if prometheus_lxc_wud_advise %}` add the
  four labels to `prometheus` (`wud.display.name: "Prometheus"`) and a `wud` service mirroring
  infra's (`roles/infra_vm/templates/docker-compose.yml.j2`, the `wud:` block) minus the notify
  trigger and its mounts: env `WUD_WATCHER_LOCAL_CRON=0 * * * *`,
  `WUD_WATCHER_LOCAL_WATCHBYDEFAULT=false`, `WUD_TRIGGER_DOCKERCOMPOSE_DEPLOY_AUTO=false`,
  `WUD_TRIGGER_DOCKERCOMPOSE_DEPLOY_PRUNE=true`; `ports: ["3010:3000"]`; volumes docker.sock,
  `{{ docker_compose_dir }}:{{ docker_compose_dir }}`, `{{ docker_compose_dir }}/wud:/store`;
  `mem_limit: 256m`; json-file logging 5m×3. Tasks: `include_role: wud_advise` (vars:
  `wud_advise_enabled: "{{ prometheus_lxc_wud_advise }}"`,
  `wud_advise_compose_dir: "{{ docker_compose_dir }}"`) **before** the compose converge, and
  `include_role: wud_advise, tasks_from: user` after it (`when: prometheus_lxc_wud_advise`).
  Handler `Restart wud` copied from `roles/infra_vm/handlers/main.yml`.
- [ ] **Step 5: Add the vault secret.** `ansible-vault edit group_vars/all/vault.yml`, add
  `vault_wud_sre_advisor_password` (generate: `openssl rand -base64 24`).
- [ ] **Step 6: Run the tests and the offline gate**

Run: `uv run pytest tests/test_wud_advise.py -v && make ci-offline`
Expected: PASS; `check-ports` finds no clash on 3010.

- [ ] **Step 7: Converge and verify live.** `v3` must equal the running digest first:
  `ssh prometheus 'docker image inspect prom/prometheus:latest --format "{{index .RepoDigests 0}}"'`
  vs `docker buildx imagetools inspect prom/prometheus:v3` (or Docker Hub). If they differ, read
  the release notes before continuing. Then `ssh prometheus 'docker pull prom/prometheus:v3 && docker pull getwud/wud:9.6.0'`,
  `make prometheus`. Checks:
  - `ssh prometheus 'docker logs --since 5m wud'` lists `prometheus` as watched.
  - From infra: `ssh infra 'curl -s -u sre-advisor:<pw> http://<prometheus ip>:3010/api/containers' | jq '.[].name'` → `"prometheus"`.
  - From the Mac: `curl -m 5 http://<prometheus ip>:3010/health` times out.
  - `ssh prometheus 'systemctl restart docker && iptables -S DOCKER-USER'` still shows the DROP rule.
- [ ] **Step 8: Commit**

```bash
git add roles/wud_advise roles/prometheus_lxc tests/test_wud_advise.py tests/__init__.py group_vars/all/vault.yml
git commit -m "Prometheus: watch v3 with WUD in advise mode (no auto-deploy)"
```

### Task 2: Docs for advise mode

**Files:**
- Modify: `documentation/wud.md` (new §7 "Advise mode", status stamp `covers:` gains the new files),
  `documentation/prometheus_lxc.md` (tag `v3`, who upgrades it now), `CLAUDE.md` (Deployment
  Model bullet: advised apps), `journal/261026-upgrade-advisor-hosts.md` (new)

- [ ] **Step 1:** Write §7: which hosts run advise mode, `AUTO=false`, port 3010 and the firewall
  unit, the `sre-advisor` user, how to turn it off, and that upgrades now arrive as the SRE
  agent's emailed recommendations (link to the sre-agent spec). Update the §1 table with an
  "advised" column or a second table.
- [ ] **Step 2:** `make check-docs && make ci-offline` → PASS.
- [ ] **Step 3: Commit** `git commit -m "Docs: WUD advise mode on Prometheus"`.

### Task 3: Open WebUI on `latest`, then advise mode

**Files:**
- Modify: `roles/open_webui_lxc/defaults/main.yml`, `roles/open_webui_lxc/tasks/main.yml`,
  `roles/open_webui_lxc/handlers/main.yml`, `roles/open_webui_lxc/templates/docker-compose.yml.j2`,
  `documentation/open_webui_lxc.md`, `documentation/wud.md` (§7 host list)
- Test: `tests/test_wud_advise.py` (add `"open_webui_lxc": ("open_webui_lxc_wud_advise", "open-webui")` to `ADVISE`)

**Interfaces:**
- Consumes: `wud_advise` role and test helpers from Task 1.
- Produces: `open_webui_lxc_open_webui_version: "latest"`, `open_webui_lxc_wud_advise`.

- [ ] **Step 1 (operator gate, not automatable):** John reads the Open WebUI release notes from
  0.11.3 to the current release (v0.12.4 on 2026-10-26) and says go. Stop here until he does.
- [ ] **Step 2: Switch the tag, WUD off.** Defaults: `open_webui_lxc_open_webui_version: "latest"`,
  `open_webui_lxc_wud_advise: false`; template `image: ghcr.io/open-webui/open-webui:{{ open_webui_lxc_open_webui_version }}`.
  Add a test `test_open_webui_runs_a_release_tag` (version == `"latest"`). `make ci-offline`.
- [ ] **Step 3: Converge.** `ssh open-webui 'docker pull ghcr.io/open-webui/open-webui:latest'`,
  `make open-webui`. Verify `curl -s http://<open-webui ip>:3000/api/version` reports the release,
  log in once, and check chats are still there. Commit: `"Open WebUI: run release builds (latest), not main"`.
- [ ] **Step 4: Advise mode.** Set `open_webui_lxc_wud_advise: true`; add the `ADVISE` entry
  (tests fail until the wiring lands); mirror Task 1 Step 4 for `open-webui`
  (`wud.display.name: "Open WebUI"`, include `wud_advise` before the converge and `user.yml` after).
- [ ] **Step 5:** `uv run pytest tests/test_wud_advise.py -v && make ci-offline` → PASS.
- [ ] **Step 6:** `ssh open-webui 'docker pull getwud/wud:9.6.0'`, `make open-webui`, then the
  same four live checks as Task 1 Step 7 for this host.
- [ ] **Step 7:** Update the two docs; commit `"Open WebUI: WUD in advise mode"`.

### Task 4: Wire the advisor into `sre-agent` on the infra VM

Run this after Part B's image is on ghcr; until then `sre-agent` ignores the extra env.

**Files:**
- Modify: `roles/infra_vm/defaults/main.yml`, `roles/infra_vm/tasks/main.yml`,
  `roles/infra_vm/templates/docker-compose.yml.j2` (the `sre-agent` service),
  `roles/infra_vm/templates/.env.j2` (non-secret advisor settings only)
- Create: `roles/infra_vm/templates/sre-upgrades.env.j2`, `roles/infra_vm/templates/sre-upgrades-allowlist.yml.j2`
- Modify: `group_vars/all/vault.yml` (encrypted: `vault_sre_agent_cf_access_aud` if treated as
  secret, `vault_sre_agent_github_token` optional)
- Test: `tests/test_sre_upgrade_advisor_wiring.py`

**Interfaces:**
- Consumes: the env and allowlist contract above; `wud_advise_api_port` (3010).
- Produces: `infra_vm_upgrade_advisor: true` and `infra_vm_upgrade_advisor_containers` (list of
  dicts with `name, host_inventory_name, registry, repository, tag, github_repo,
  release_tag_format, readiness_path, pbs_guest, context_sources`), where `context_sources` are
  repo-relative template paths, e.g. `roles/prometheus_lxc/templates/docker-compose.yml.j2`.

- [ ] **Step 1: Write the failing tests** in `tests/test_sre_upgrade_advisor_wiring.py`:

```python
def test_allowlist_renders_one_entry_per_advised_container(): ...
    # render sre-upgrades-allowlist.yml.j2 with hostvars stub
    # {"prometheus_lxc": {"ansible_host": "10.0.0.15"}, "open_webui_lxc": {"ansible_host": "10.0.0.19"}}:
    # yaml containers[*].name == ["prometheus", "open-webui"];
    # prometheus.wud_url == "http://10.0.0.15:3010"; prometheus.tag == "v3";
    # every auto_deploy is False; context_files all start with "/app/upgrades/context/"
def test_sre_agent_reads_advisor_secrets_from_its_own_env_file(): ...
    # compose("infra_vm")["services"]["sre-agent"]["env_file"] == [".env", "sre-upgrades.env"];
    # a volume "/srv/infra/sre-upgrades:/app/upgrades"
def test_no_advisor_secret_in_shared_env(): ...
    # rendered .env.j2 has no UPGRADE_ADVISOR_WUD_PASSWORD, CF_ACCESS_AUD or UPGRADE_ADVISOR_GITHUB_TOKEN
def test_advisor_env_file_holds_the_secrets(): ...
    # rendered sre-upgrades.env.j2 keys ⊇ {"UPGRADE_ADVISOR_WUD_USERNAME",
    # "UPGRADE_ADVISOR_WUD_PASSWORD", "CF_ACCESS_TEAM_DOMAIN", "CF_ACCESS_AUD", "UPGRADE_ADVISOR_GITHUB_TOKEN"}
```

- [ ] **Step 2:** `uv run pytest tests/test_sre_upgrade_advisor_wiring.py -v` → FAIL.
- [ ] **Step 3: Implement.** Defaults list per the Interfaces block (Prometheus: readiness
  `:9090/-/ready`, `pbs_guest: "CT 901"`, context = its compose and `prometheus/prometheus.yml.j2`
  templates; Open WebUI: readiness `:3000/health`, `"CT 902"`, context = its compose template).
  Tasks (tagged `docker, sre`): dir `/srv/infra/sre-upgrades` and `context/<name>/` (owner
  `1001`, 0750); `ansible.builtin.copy` each `context_sources` file from the controller to
  `context/<name>/<basename>` (0640, owner 1001; these are template sources, so vault values
  never leave the controller); allowlist template (0640, owner 1001); `sre-upgrades.env`
  (0600, owner `docker_user`, `no_log`). Non-secret settings (`UPGRADE_ADVISOR_CONFIG_PATH`,
  `_DB_PATH`, `_CRON`, `_MIN_AGE_HOURS`, `_PUBLIC_BASE_URL`, `_VERIFY_DELAY_SECONDS`) go in
  `.env.j2`'s SRE AGENT block. Compose: `env_file: [.env, sre-upgrades.env]`, volume
  `/srv/infra/sre-upgrades:/app/upgrades`. All of it under `{% if infra_vm_upgrade_advisor %}` /
  `when: infra_vm_upgrade_advisor`, with removal when false.
- [ ] **Step 4:** tests + `make ci-offline` → PASS.
- [ ] **Step 5: Converge** with the image-only start point (a `t=docker` run also apt-upgrades
  Docker): `make infra EXTRA="--start-at-task='infra_vm : Template Docker Compose file'"`, after
  `ssh infra 'docker pull ghcr.io/johnmathews/sre-agent:<version>'`. Verify
  `ssh infra 'docker exec sre-agent ls /app/upgrades /app/upgrades/context/*'`.
- [ ] **Step 6:** Update `documentation/infra_vm.md` (SRE agent section) and `wud.md` §7; commit
  `"SRE agent: wire the Upgrade Advisor (allowlist, context, secrets)"`.
