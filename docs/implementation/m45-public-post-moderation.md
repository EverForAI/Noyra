# M45 Public Post Moderation and Abuse Boundaries

Stage 2 permits a visitor to submit a public post or help request without
turning an unauthenticated Internet request into an immediate publication.
Every accepted submission starts as `pending_review`; only an authenticated
operator can publish or reject it, and published or rejected items may later be
archived. The public projection returns only `published` rows.

## Layered admission controls

A randomized six-character PNG CAPTCHA is required for a new submission. Its
answer is stored only as a salted digest, a challenge is bound to the normalized
client network bucket, and it expires, consumes once, and has a bounded number
of attempts. Operators can choose letters, digits, or alphanumeric characters.
CAPTCHA increases automated-posting cost but is not treated as proof that a
visitor is human; the bounded controls below remain authoritative.

The defaults are:

- 10 accepted posts per client bucket per rolling hour.
- 30 CAPTCHA issues per client bucket per rolling hour and 300 issues per
  minute across the subject.
- Three simultaneously active challenges per client, 5,000 active challenges
  globally, and 100,000 retained one-hour rate or issuance evidence rows.
- 1,000 pending-review posts, 100,000 total post rows, and 250,000,000 content
  bytes. A free-disk check is also performed before admitting evidence or a
  post.

The management page exposes the normal post rate, pending queue cap, CAPTCHA
mode/TTL/attempt and issuance limits, and content-byte cap. Environment values
initialize these settings; an authenticated management update is stored as an
integrity-bound operator control record. Hard row, active-challenge, and free
disk backstops intentionally remain code-owned emergency limits.

## Source identity and privacy

Raw client IP addresses are not persisted. Noyra normalizes IPv4 addresses and
groups IPv6 addresses by `/64`, then stores only a process-keyed HMAC bucket.
The key changes when the service restarts, which intentionally expires old
CAPTCHAs and prevents durable IP correlation; rolling per-client counters also
restart with a new pseudonym. Global, queue, storage, and normal HTTP limits do
not depend on that pseudonym and continue to bound total pressure.

`X-Forwarded-For` is accepted only when the immediate peer belongs to
`NOYRA_TRUSTED_PROXY_CIDRS`. The chain is parsed from the trusted proxy toward
the client and is bounded. With no trusted-proxy setting, forwarded addresses
are ignored. A reverse-proxy deployment must therefore trust only the narrow
proxy network; trusting Internet client ranges would allow source-bucket
spoofing.

## Moderation and evidence

The `/admin` moderation surface requires the independent operator session and
CSRF protection. Each decision has a required reason, an expected current
status, and an idempotency key. The expected status prevents a stale browser
from overwriting another decision, while retries of the same operation return
the original result. Author labels remain unverified visitor text unless the
record explicitly carries another provenance value; the UI displays that
distinction.

Post content, identity/provenance, operator controls, and every moderation
transition are hash-bound. Moderation history is append-only and ordered by a
per-post revision/predecessor chain. The schema upgrade validates the old hash
contract before rewriting legacy evidence; tampered legacy rows stop the
transaction and service admission rather than being silently blessed by a new
hash. Exports include the moderation/control evidence needed for later audit.

## Operational expectations

The public site should still sit behind ordinary transport protection and a
general request-rate limit. CAPTCHA does not defend against sophisticated OCR,
a large distributed botnet, or abusive but technically human content. Queue
review, conservative limits, storage monitoring, and the ability to reject or
archive remain required. CAPTCHA and submission endpoints use same-origin JSON
requests; a cross-site form cannot bypass those request contracts.
