# Web UI follow-ups: npm alerts, ESLint, runbook in RAG

> Purpose
>
> Records the follow-ups to the web UI joining this repo (journal `261112-web-ui-joins-the-repo.md`): the npm security
> alerts, a linter for `web/` (#56) and the stream-failure runbook moved into the RAG store (#55).

## 1. npm security alerts

- 26 open Dependabot alerts sat on `web/package-lock.json` once the UI joined the repo: dompurify, vite, postcss,
  nanoid, source-map-js and `@vue/server-renderer`.
- Floors raised to the first patched versions (`dompurify ^3.4.17`, `vite ^8.1.6`, `vue ^3.5.42`); the lock now holds
  vite 8.3.8, dompurify 3.4.17, vue 3.5.45, postcss 8.5.32, nanoid 3.3.20, source-map-js 1.2.2.
- Confirmed: `npm audit` reports 0 vulnerabilities; build, typecheck and Playwright (210 passed, 8 viewport skips)
  pass; the image builds.

## 2. ESLint (#56)

- Flat config in `web/eslint.config.js`: Vue's `essential` rules plus typescript-eslint's `recommended`, via
  `@vue/eslint-config-typescript` 14.9 (`withVueTs`). Runs as `npm run lint` with `--max-warnings 0`, in
  `make web-lint`, `make web-check` and CI's `web-test`.
- `flat/recommended` was tried first and dropped: its template formatting rules raised 81 warnings in a project with
  no formatter.
- `vue/no-v-html` is an error. It found the search-snippet `v-html` in `ConversationSearch.vue`. That one is safe:
  `highlightSnippet` HTML-escapes the snippet and the query before inserting `<mark>`. It now carries a disable comment
  saying so, like the two markdown sites in `ChatMessage.vue`.
- Also fixed: an unused import in a Playwright spec and a stale `no-console` disable comment.
- ESLint 10's tree pulled in `minimatch` 10.2.6 (GHSA-g3hv-75wg-m249, moderate). `overrides.minimatch: ^11.0.8` in
  `web/package.json` keeps `npm audit` at 0. ESLint ran clean on 11.0.8. The override can go once ESLint's own
  dependencies reach `minimatch >= 11.0.1`.
- Confirmed: a planted unused variable makes `npm run lint` exit 1; the clean tree exits 0.

## 3. Runbook into RAG (#55)

- `docs/web/runbooks/triaging-webapp-stream-failures.md` moved to `runbooks/`, links fixed. `load_runbooks()` returns
  20 files (19 before), this one as 14 chunks. The smoke-tested image holds 20 runbooks.
- It reaches the live agent only after ingest runs on infra (`sre-ingest`, then a restart of `sre-agent`).

## 4. Handed over

The 6 Python alerts (claude-agent-sdk 0.1.51, fixed in 0.2.121; chromadb 1.5.0, no fix) went to a separate session
with its own run. Lead noted for it (suspected, not verified): `src/agent/sdk_agent.py` never passes `resume`, so the
claude-agent-sdk advisory's vector may not be reachable here.

## 5. What is deliberately not done

- Vue's formatting rules and a formatter (Prettier). Restyling every template is its own decision.
- Renaming `Sidebar.vue` to satisfy `vue/multi-word-component-names`; it is exempted in the config instead.
- Typed linting (`recommendedTypeChecked`): slower, and the typecheck already runs in CI.
