# Current Source Status and Evidence Index

**Snapshot date:** 2026-11-25
**Code baseline checked for this index:** `f5e834d0107e7d095aa7dd6536a7172f60bc3fce`

This page is a navigation and scope note, not a release, security certification, or claim that every
historical check was rerun against the current source. It distinguishes package metadata, source tags,
database migration targets, and deployment state.

## Version identifiers

- **Python package version:** `0.1.0`, from `pyproject.toml` and `src/noyra/__init__.py`.
- **Latest preview tag present in the checked-out repository:** `preview-2026.09.09-r2`, at commit
  `f094666ae321576fe84448231f847a70bcad9c47`. This is a source tag, not proof of a signed/published
  GitHub release or of deployment to an instance.
- **Current source schema target:** `119`, from `src/noyra/core/schema/base.py`. It is the runtime's
  migration target; an installed database may be older and must be inspected independently.
- The checked source baseline is later than the preview tag and contains unreleased development work.
  The package version `0.1.0` and the preview tag identify different things; neither should be presented
  as a newly released version of the current checkout.

## Evidence and acceptance index

| Record | What it covers | What it does not establish |
| --- | --- | --- |
| [`CHANGELOG.md` — Unreleased](../../CHANGELOG.md#unreleased) | Development changes recorded in the current source tree | Test success, release acceptance, or deployment |
| [`preview-2026.09.07-r1` notes](2026-09-07-preview-r1.md) | The specifically named r1 preview revision | r2 or the current development checkout |
| [2026-10-27 runtime remediation summary](../audit/2026-10-27-overnight-health-remediation.md) | A bounded summary of the runtime issues and checks named in that report | A full audit or acceptance of later source commits |
| [M42 remediation status](../implementation/m42-remediation-status.md) | A historical status record, explicitly dated 2026-08-18 | Current project-wide verification |
| [Release gate guide](external-gates.md) | Required release/evidence workflow and external gates | A completed run of those gates for the current checkout |

For the OPS-03 code baseline (`f5e834d0`), the local permission-monitor, readiness-boundary, and Windows
ACL targeted checks recorded **21 passed**; Ruff and Mypy passed for the changed source and tests. This is
focused evidence only: it is not a full-repository test result, GitHub Actions result, server upgrade, or
long-duration production observation. This index does not claim project-wide acceptance; consult an
exact-source verification record before making that claim.

## Reading deployed state

The repository's schema target, package version, or Git tag must not be used to infer the version or
schema of a running Noyra instance. Confirm deployed source SHA, database schema, configuration, and
runtime health on that instance through a separately scoped operational check. No instance state is
asserted by this index.
