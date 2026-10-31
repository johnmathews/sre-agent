# Upgrade Advisor: say what happens after an approval

> Purpose: record why the advisor's email and approval-page wording changed on 2026-10-31.

## 1. Problem

- After approval, the page said "Deploying N update(s); results arrive by email."
- Verification emails only on failure. A passing deploy appears in the next daily email.
- So after a clean approval no email arrived, and the silence looked like a fault.
- The daily email's "Yesterday's deploys" section printed raw outcome codes (`ok`, `digest_mismatch`).

## 2. Change

- `AFTER_APPROVAL` in `src/upgrades/email.py` holds three bullets on what follows an approval. The daily email (under
  the approve link) and the post-approval page both render it, so the two stay in step.
- The results section is now "Results of approved upgrades (last 24 hours)", with each outcome in plain words
  ("upgraded and verified", "upgrade failed", "running a different version than approved", ...).
- The store, logs and metrics keep the raw codes, so dashboards and alerts are unaffected.
- The subject line is unchanged, so existing mail filters keep working.
- The "Loki not configured" warning used "; ", the separator the email splits detail on, so the email dropped its
  second half. It now uses ", ".

## 3. Left out

- A same-day "all upgrades verified" email: one extra email per approval for the common case that needs no action.
