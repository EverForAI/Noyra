# Candidate-range publication boundary

The publication guard checks prohibited generated/evidence paths and new
machine-local references in `docs/audit/` and `docs/roadmap/` policy text. It does
not read secrets, scan every source file for credentials or erase public history.
The reviewed public-file manifest separately checks exact Git blob inventory and
checksums. Both checks are required; neither substitutes for the other.

## Local index, single commit and candidate range

The existing local pre-commit hook retains `--staged`, inspecting index bytes
before commit. `--revision COMMIT` remains a narrow single-commit compatibility
check: all prohibited paths in that tree plus relevant additions against its
first parent (or root additions). It cannot attest a multi-commit candidate.

For a complete candidate, supply a reviewed baseline and selected tip:

```sh
python scripts/check-publication-boundary.py --base REVIEWED_BASE_COMMIT --revision CANDIDATE_COMMIT
```

Replace both placeholders with known commits; do not choose the candidate itself
as its own baseline. Local refs are resolved once to immutable object IDs. The
baseline must be a strict ancestor, the repository must be non-shallow, and the
complete range must fit the 4,096-commit work bound. Missing history, an empty
range, unrelated/reversed history or excess work fails instead of silently
falling back to a smaller scope.

Every commit reachable from the selected tip but not the reviewed baseline is
examined, including non-first-parent ancestry. Each introduced snapshot is
compared to the reviewed baseline tree. This catches:

- An early introduction followed by a harmless tip change.
- Prohibited content introduced and then removed before the tip.
- Side-branch history retained by a merge whose final tree is unchanged.
- Previously removed old-parent content resurfacing in a new snapshot.
- Copies or renames into a newly reviewed audit/roadmap path.

Prohibited paths are checked throughout introduced snapshots even if the tip
removes them. Relevant text already present at the same path in the trusted
baseline retains its historical exemption; earlier reachable history is not
rescanned. That exemption is not permission to move the line to another policy
path. Synthetic test source and deployment examples retain their existing scope
rules. No administrator is instructed to rewrite history to satisfy this guard.

Policy diffs force text and disable external diff/textconv/rename shortcuts, so
candidate `.gitattributes` cannot hide policy text as binary. Git replacement
objects are disabled for object and ancestry reads. The guard reads selected Git
objects, not a dirty worktree, and emits locations/categories and short commit
IDs without echoing matching content or event payloads.

## GitHub event scope

The CI and tag-release quality jobs call the same `--github-event` entry point
with full checkout history. Scope is read from bounded platform event JSON and
exact object IDs. PR/body/branch/input text is never interpolated into shell
commands. The checked-out HEAD must match the event SHA.

| Trigger | Reviewed base | Selected candidate |
|---|---|---|
| Pull request | Event `pull_request.base.sha` | Event `GITHUB_SHA`, verified as the exact base/head merge candidate |
| Main branch push | Event `before` | Event `after`, checked against `GITHUB_SHA` |
| Manual quality run | Explicit `publication_base` input, otherwise repository `PUBLICATION_REVIEW_BASE` | Event `GITHUB_SHA` |
| Tag release | Repository `PUBLICATION_REVIEW_BASE` | The pushed tag's commit, checked against `GITHUB_SHA` |

The workflow passes the repository variable through
`NOYRA_PUBLICATION_REVIEW_BASE`. Set `PUBLICATION_REVIEW_BASE` to a full reviewed
ancestor SHA before a tag release; do not substitute an unreviewed tip to make the
gate green. The manual input likewise requires a full immutable SHA. Branch PRs
and ordinary main pushes do not require a per-run baseline input.

Missing manual/tag baselines produce an actionable rejection. A new branch push
with an all-zero `before`, a non-ancestor force push, mismatched event/checkout,
incomplete metadata or unsupported event never degrades to tip-only review.
Bootstrap or history-rewrite recovery requires an explicit maintainer-reviewed
range, not an automatically guessed parent. Annotated tags are resolved to their
commits. Existing release test/build/signing and exact-source archive checks
remain in place.

"Reviewed" is a trust premise, not something a SHA string can prove. Event base
selection assumes main branch protections and prior successful review; manual
inputs and repository variables require maintainer authority. This change does
not verify remote branch-protection settings or resist a repository administrator
who can replace the workflow, checker or trust input. Do not advance a trusted
base merely because a previous main run started; review its result first.

## Failure and rollout boundaries

The new range gate affects source publication only. It does not run Noyra, open
its databases, call providers, change network policies or deploy an instance.
Ordinary CI PR/push events derive their scope automatically. A tag release or
manual run without an explicit reviewed baseline will now stop early; this is
an intentional prerequisite, documented here rather than a hidden fallback.

Tests use isolated Git repositories for intermediate commits, root/merge/rename
history, shallow graphs, malformed event scope and native hook behavior. Linux
and Windows checks use the same stdlib/Git code. Local success does not claim
that remote CI ran, that every pre-existing audit line was rescanned, or that
previously published material was removed. Follow-up publication should use one
reviewed candidate and the normal required checks.
