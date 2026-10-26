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

Security-sensitive changes need a focused regression test, an audit note, and
an explicit statement of the data and capability boundary they affect. Do not
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
