# Logins lookback and the label listing window

> Purpose: record why the incident `logins` source and `loki_list_label_values` changed on 2026-10-31.

## 1. Problem

- `logins` decided whether a host ships sshd/sudo lines from Loki `service_name` values over the outage window
  (minutes). An LXC logs 3–6 sudo lines a week, so almost every outage got "host does not ship login logs", which was
  false. A proxmox-setup change is about to ship these lines from 13 hosts.
- `loki_list_label_values` sent only `query`, so Loki used its default range of the last 6h. Five quiet containers
  (portainer, qbittorrent, sabnzbd...) looked like they shipped no logs. 7-day queries show they do. Nothing in the
  output or the description named the 6h.

## 2. Change

- `_logins` reads the label values over `LOGINS_LOOKBACK` (7 days) up to the window end, then reads the lines over the
  window. A host that shipped lines in the week but none in the window gets `ok` with `no logins in the window`.
- The skip detail is now "host shipped no sshd or sudo lines in the 7 days before the outage".
- `loki_list_label_values` takes `start` (default `6h`) and `end` (default `now`), parsed with
  `_parse_relative_time` like `loki_query_logs`, and sends both to Loki as nanoseconds. End before start is rejected.
- The result names the range: "Found 31 values for label 'container' in the last 6h:", or "from <start> to <end>"
  for other ranges. The empty result and the description say to widen `start` (e.g. `7d`) before concluding
  something ships no logs.
- Both MCP wrappers (`src/api/mcp_server.py`, `src/agent/mcp_tools.py`) pass the new parameters.
- `docs/incident-explainer.md` §5, `docs/tool-reference.md`, the system prompt and the `loki-logging` and
  `request-failure-investigation` runbooks state the lookback or say to widen `start`.
- `runbooks/quiet-hours.md` no longer lists the Uptime Kuma integration as current. It was removed on 2026-10-31
  with Kuma's retirement.

## 3. Red first

- W2, on the old `_logins`:
  - the new test, with `sudo` present only when the lookup starts before the window: `assert 'skipped' == 'ok'`
  - `test_window_override_respected`, label lookup start: `assert '1793091600000000000' == '1792492200000000000'`
    (window start, not window end minus 7 days)
  - the skip test: `assert 'host does no...ip login logs' == 'host shipped...re the outage'`
- W3, on the old tool: 10 failures. `KeyError: 'start'` / `KeyError: 'end'` on the Loki request params, the
  rejection tests got `Cannot connect to Loki` (the arguments were ignored), `_format_label_values() takes 2
  positional arguments but 3 were given`, the FastMCP wrapper raised `Unexpected keyword argument` for `start` and
  `end`, and the SDK schema lacked them (`{'end', 'start'} <= {'label', 'query'}`).

## 4. Tests

- 1691 on `origin/main`, then 1692 after the logins change, then 1699 after the label window change. `make check`
  passes.
