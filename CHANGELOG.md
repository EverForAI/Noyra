# Changelog

## Unreleased

- Added bounded asynchronous runtime and training export jobs with status,
  cancellation, and authenticated download endpoints.
- Added independent embedding resource configuration with secret isolation.
- Added versioned `/api/v1` aliases, OpenAPI documentation, diagnostics, UTC
  budget reset visibility, and common-knowledge review/revocation views.
- Hardened legacy sparse-database migration and cold event payload restoration
  in runtime exports.

## preview-2026.09.07-r1

- Research source preview revision; Python package metadata remains `0.1.0`.
  This is not a stable or production release.
- Retain unresolved payment reservations, transaction identities and earlier
  attempt receipts across rejection, retry and restart; block ambiguous legacy
  histories and unknown-payment refunds.
- Share native-fee settlement freshness across native/token sends and retries.
- Bind file I/O to no-follow handles; revalidate the exact charged capability
  before and after reads without charging twice; support equivalent grant roots.
- Reject untrusted POSIX writable directory configurations. Dedicated-account
  isolation remains required; hostile same-UID actors are outside this boundary.
- Correct Windows training-export race-test path normalization without weakening
  its foreign-byte isolation assertion. Add regression and pressure coverage.
- Preserve the original preview/tag and sanitized public history; add updated
  file checksums, citation metadata and explicit release limitations.

## 0.1.0

- Initial continuity-first artificial-subject runtime release.
