# Triaging webapp stream failures

> **Use when:** A user reports the chat threw an error mid-reply —
> "Error: Load failed", "Connection dropped mid-reply", "the agent
> stopped halfway", or any variant. Also when the SRE agent appears to
> answer correctly but the client shows an error.

The procedure below is what produced the original triage on 2026-05-05
([journal](../../journal/260505-mobile-ux-overhaul.md)) — codified so
the next investigation takes ~5 minutes instead of half an hour.

## Step 1 — Get the timestamp + context

Ask the user for:

1. **Wall-clock time** they saw the error, with timezone (usually their
   phone's local time).
2. **What the error said** — the heading on the `ErrorBubble`, not the
   "Details" disclosure. The heading maps to a `StreamError.category`
   (see [`api-integration.md`](../api-integration.md) §"Error categories
   and contracts").
3. **What they were doing when it happened** — typing a long reply,
   walking out of the house (Wi-Fi → cellular handoff), phone went to
   sleep, etc. The behavioural context is often the diagnostic clue.

Convert the wall-clock time to UTC. The conversation API records
`user_timezone` per turn, which helps disambiguate later.

## Step 2 — Locate the conversation

```text
mcp__sre-agent__sre_agent_list_conversations  (limit ≥ how many
                                                 turns ago the error was)
```

Match by `updated_at` near the error timestamp, the first user message
if recognisable, and turn count. Note the **session id** (8-char hex).

## Step 3 — Read the conversation

```text
mcp__sre-agent__sre_agent_get_conversation(session_id="...")
```

Three patterns. Each implies a different cause:

| Conversation history | Implication |
|---|---|
| **Failed turn missing.** Last user message has no assistant reply, OR the user typed `Try again` and the *retry* answer is present but the original failed turn is gone. | Server-side persistence happened, client missed the bytes. Canonical handoff / mid-stream drop. |
| **Failed turn present** with the same user question and a complete assistant answer. | Server completed AND persisted, client *also* surfaced an error. Likely an `ErrorBubble` rendering bug or an SSE error event delivered alongside an answer. Investigate the recovery layer. |
| **No matching conversation.** | Request never reached the backend at all. Proxy / Cloudflare / auth-cookie expiry. Skip to Step 5's "no backend log" branch. |

## Step 4 — Pull Loki logs around the timestamp

Window: −2 to +2 minutes from the reported error.

```text
mcp__sre-agent__loki_query_logs(
  query='{hostname="infra"} |~ "(?i)(sre-agent|sre-webapp|/ask)"',
  start="<utc-2min>",
  end="<utc+2min>",
  limit=200,
)
```

Three signals to extract:

1. **`service_name=sre-agent` (FastAPI)** — `POST /ask/stream` lines
   show whether the backend received the request and what status it
   returned. FastAPI logs the *start* of the request, not its end.
2. **`service_name=sre-webapp` (nginx access log)** — combined-format
   line per request: client IP (often IPv6), status code, response
   body size. Logged at request *completion*. Body size = 0 or very
   small means the client got nothing.
3. **`service_name=traefik`** — combined-format access log with an
   extra **duration in ms** at the end. This is the source of truth
   for "how long was this request alive". Anything > 60 s is in iOS
   Safari's danger zone and > 100 s is in Cloudflare-free-plan
   timeout territory.

## Step 5 — Compare IPv6 source prefixes

This is the step everyone skips. **The most common cause of mid-stream
drops on the user's iPhone PWA is a network handoff** (Wi-Fi reconnect,
Wi-Fi ↔ cellular, SLAAC re-assignment), and the **smoking gun is the
IPv6 source prefix changing between consecutive requests for the same
session**.

In the `sre-webapp` and `traefik` access logs, look at the source IP
across consecutive turns. Different `/48` prefixes → different network
connection.

**Worked example (2026-05-05 incident):**

| Turn | Time (UTC) | Source prefix | What happened |
|------|------------|---------------|---------------|
| 2 | 07:30:22 | `2a09:bac2:4d85:c8::` | OK |
| 3 | 07:33:21 | `2a09:bac2:4d85:c8::` | OK |
| 4 | 07:36:23 | `2a09:bac2:4d85:c8::` | 200 OK, 6285 bytes saved |
| **retry** | **07:37:41** | **`2a09:bac2:4d80:2719::`** | **Different /48 — handoff** |

Turn 4's stream completed server-side; the client's read died because
the socket was on a network connection that disappeared. The user's
"Try again" came back on a different network. **That's a handoff, not
an agent failure.**

## Step 6 — Decision tree

| Backend FastAPI log | nginx response code | Conversation persisted | Cause | Action |
|---|---|---|---|---|
| `200 OK` | `200`, full body size | Yes | Mid-stream socket close (client missed bytes — see Step 5) | Recovery layer should have caught this silently. If user saw `Connection dropped mid-reply`, check `streamWithRecovery.ts:101` `tryRecoverPersistedAnswer` — recovery may have failed for a real reason. If user saw silent answer, no further action; system worked as designed. |
| `5xx` + exception trace | `5xx` | Partial / no | Agent / LLM / upstream failure | Investigate the FastAPI stack trace. Webapp behaved correctly by surfacing `http-5xx`. |
| `4xx` | `4xx` | No | Validation / auth / rate-limit | Confirm `ErrorBubble` category matches (`http-4xx` / `Session expired` for 401, etc.). |
| No FastAPI entry, but traefik shows the request | `502`/`504` | No | Backend container down / restarting | Check `docker compose ps` and `sre-agent` container health. |
| No traefik entry | — | No | Cloudflare-side or DNS / TLS at the user's edge | Check Cloudflare logs / Access cookie expiry. Likely outside the homelab. |

## Step 7 — Validate the rendered category

Cross-check what the `ErrorBubble` told the user against the actual
failure mode. The mapping should be:

| Failure mode | Expected category |
|---|---|
| Total offline / DNS / TLS-fail before any byte | `network-before-headers` |
| Some bytes received, then socket dropped (handoff) | `network-midstream` |
| HTTP 4xx | `http-4xx` (with status; 401 → "Session expired", 429 → "Rate limited") |
| HTTP 5xx | `http-5xx` |
| Backend emitted `{type: "error"}` cleanly | `sse-error-event` |
| 200 OK with empty body | `no-body` |
| User clicked Stop | `aborted` (no bubble; just leaves turn in place) |

Mismatch → bug in `src/api/stream.ts` `StreamError.from()` or in
`src/api/streamWithRecovery.ts`. Open an issue with the conversation id,
the timestamp, and the category that surfaced.

## Common pitfalls

1. **FastAPI logs request start, not end.** A "200 OK" line with no
   error trace doesn't mean the response made it to the client — it
   means the request was accepted. Use traefik's duration column for
   end-to-end timing.
2. **iOS Safari masks every `fetch()` failure as `TypeError: Load
   failed`.** Don't try to read meaning into the message text — the
   classification comes from `receivedAnyBytes`, not the string.
3. **Cloudflare free-plan idle timeout is 100s.** A stream that's
   silent for >100s gets closed from above. The 15s heartbeat in
   `streamAsk` is the mitigation; if you see a 100s+ traefik duration
   followed by an unexpected close, suspect the heartbeat path.
4. **"The retry worked" is consistent with both a transient blip AND
   a recovery-layer success.** Don't conclude the stack is healthy
   just because the user got an answer eventually — read the logs to
   distinguish.
5. **The `sre-webapp` access log's response body size is the
   *complete* SSE wire size, including all events, not just the
   `answer` event payload.** A 6285-byte body for a 1500-character
   answer is normal (status, tool_start, tool_end, heartbeats,
   answer all add up).

## Related

- [`docs/api-integration.md`](../api-integration.md) — error categories
  and the resilience contract
- [`src/api/stream.ts`](../../src/api/stream.ts) — `StreamError`
  classification
- [`src/api/streamWithRecovery.ts`](../../src/api/streamWithRecovery.ts) —
  retry + recovery layer
- [`src/components/ErrorBubble.vue`](../../src/components/ErrorBubble.vue) —
  what the user actually sees
- [`journal/260505-mobile-ux-overhaul.md`](../../journal/260505-mobile-ux-overhaul.md) —
  the original triage this runbook is distilled from
