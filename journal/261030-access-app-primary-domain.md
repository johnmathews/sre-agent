# Approval links died after sign-in: the Access application's first domain

On 2026-10-30, Upgrade Advisor email links ended on a dead hostname after the Access PIN, on a
phone with no Access session. The link, sre-webapp and sre-agent were all correct; no request
reached the origin.

The Access application in front of the SRE host is its own application, not the homelab's
wildcard one. `docs/architecture.md` had said otherwise; the AUD in `CF_ACCESS_AUD` was always
this application's. Access completes a fresh sign-in at `/cdn-cgi/access/authorized` on the
application's first domain. That was a documentation hostname retired on 2026-10-26, so the
callback hit NXDOMAIN. Removing it from the application, in place so the AUD did not change,
fixed links on desktop and phone.

`docs/architecture.md` now states the dedicated application and the first-domain trap. The
security reasoning is unchanged: the Access JWT proves only a sign-in the policy admitted, and
the approver list and single-use token remain the two further factors. The infra side is in the
home-server repo's journal, `261030-sre-access-signin-ended-on-retired-docs-host.md`.
