# Contributing to Noyra

Noyra is an experimental continuity-first artificial-subject runtime. Changes
must preserve subject isolation, append-only evidence, explicit capability
boundaries, and the distinction between communication intent and delivery.

Before opening a pull request, run:

```text
python -m pytest -q
python -m ruff check src tests
python -m ruff format --check src tests
python -m mypy src tests
python -m compileall -q src
python -m pip check
git diff --check
```

Security-sensitive changes need a focused regression test, a private audit note,
and a reviewed public summary of the data and capability boundary they affect.
Do not publish unresolved vulnerability details or raw reproduction evidence as
ordinary delivery documentation. Do not
commit API keys, private subject data, generated archives, or local runtime
directories. Report vulnerabilities privately using `SECURITY.md`.

Maintenance scope and response expectations are in [MAINTAINING.md](MAINTAINING.md).

For changes that cross runtime, HTTP, or database boundaries, follow
[the architecture invariant matrix](docs/development/architecture-boundaries.md).
Keep route match priority, authentication, admission leases, transaction scopes,
and shutdown ownership explicit. Historical migration SQL is frozen; schema
changes need a reviewed migration rather than edits to earlier definitions.
Run focused tests first and then the relevant cross-module checks. Record any
failed or skipped checks with their cause; a retry is not a reason to discard
the original result. Update and verify the reviewed public file inventory for
the staged Git snapshot before committing new public files.

## Publication boundary

Keep detailed audit findings, machine-local evidence, logs and repository backups
outside the source checkout. Public summaries must distinguish implementation,
verification, deployment and open work; redaction must never turn an unresolved
finding into a claim that it was fixed. Retain necessary public safety warnings.

Enable the versioned native Git pre-commit hook in this clone:

```sh
git config core.hooksPath .githooks
python scripts/check-publication-boundary.py --staged
python scripts/public-file-manifest.py generate --index
git add docs/release/public-files.sha256
python scripts/public-file-manifest.py check --index
```

Stage the reviewed source changes first. For a new public file, review and update
`docs/release/public-files.txt` before generating checksums. Regenerate after the
last staged edit, including edits to existing source or test files. Generation
reads staged Git blobs and does not stage files automatically.

If this clone already has a custom hook, integrate both checks rather than
overwriting that setup. Python must be available on PATH; hook failure blocks the
commit. The hook checks the index, not unstaged working files. It rejects tracked
output, Git bundles and raw audit evidence, and checks only added audit/roadmap
text for machine-local drive and WSL references. Synthetic source/tests and normal
deployment examples are not subject to that text rule. It does not scan secrets
or certify that a summary is safe to publish. Hooks are local and can be bypassed;
the existing public-source CI matrix runs the same guard as a backstop.
The hook also rejects stale staged public-file hashes without rewriting or
staging anything; the CI check continues to verify the exact committed snapshot.

Keep GitHub Secret scanning and Push protection enabled for supported credential
patterns. They complement, but do not replace, review of personal information and
coordinated disclosure. The curated source export exclusions are not a secrecy
mechanism: any tracked path can still be public through the repository itself.

A normal cleanup commit affects the new snapshot only. Older commits, PR views,
caches and previously downloaded copies may remain accessible. Do not describe
such cleanup as erasing history or recalling public copies.
