# Participant login throttling and recovery

Login admission uses two separate limits with different purposes.

- The existing client-IP bucket limits total login attempts in its durable
  hourly window. Successful attempts still count toward this source-level
  abuse budget.
- A hashed account bucket counts only failed password checks for that local
  account. Successful logins clear only that account's failure window in the
  same transaction that creates its session. Unknown usernames share one
  subject-scoped bucket, avoiding a durable row for every attacker-chosen
  string and avoiding a username-existence response difference in ordinary
  credential failures.

After ten account failures in one hour, the existing same-origin, one-use image
challenge is required before further credentials are checked. To avoid making
this threshold a username-state oracle, the endpoint keeps the same generic
invalid-credentials response; the browser proactively offers the challenge
after any failed login. The challenge is bound to the current client IP and
consumed before password verification. The correct
password plus a valid challenge still succeeds and clears that account's
failure window; this is deliberately a step-up challenge, not a permanent
account lock. A failed CAPTCHA or password does not reset the counter. The
browser obtains the image on demand, includes `captcha_id` and
`captcha_answer`, and requests a fresh image after one-use failure.

A challenged request without a valid image performs the same bounded dummy KDF
as an unknown username and then returns generic invalid credentials. The account
throttle never substitutes for the global KDF concurrency bound.
Unknown-user password checks retain the existing dummy hash behavior. Recovery
codes remain high-entropy credentials protected by the client-IP attempt
budget; failed recovery proofs do not increment a username bucket that could
lock the legitimate owner out of account recovery.

`participant_auth_limits` retains its current schema. Expiry cleanup and bucket
capacity checks are scoped to the current subject, so one Noyra instance cannot
purge or consume another subject's quota. The existing 10,000-bucket ceiling
per subject remains fail-closed. Network IP accounting and IPv6 normalization
are unchanged; a shared NAT can still exhaust its existing per-IP attempt
budget, and proxy trust configuration is a separate deployment concern.

This mechanism raises the cost of credential stuffing but does not claim to
prove a human solved a challenge or defeat a distributed CAPTCHA-solving
service. It adds no email, phone verification, operator approval, new account
permission, or migration. CAPTCHA errors remain generic and do not expose
account existence.
