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
