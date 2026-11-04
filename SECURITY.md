# Security Policy

Noyra handles private psychological state, credentials, external messages and
operator capabilities. Treat a deployment as a personal data system.

## Reporting

Do not open a public issue for a vulnerability. Report privately to
`EverForAI/Noyra`, maintained by Jaxon Grey, using
[GitHub Private Vulnerability Reporting](https://github.com/EverForAI/Noyra/security/advisories/new).
The feature was enabled and verified on September 7, 2026. Sign in to GitHub,
then use Security > Advisories > Report a vulnerability. Reports are not
automatically published. If the private form is unavailable, do not substitute
a public issue or disclose vulnerability details publicly.
Include a minimal reproduction, affected commit, deployment mode
(Windows or Ubuntu), and impact in a private report.
Do not include real API keys, private messages, wallet material, or personal
data in a report.

The maintainer will acknowledge a report within seven days, provide a severity
assessment, and coordinate a fix or mitigation. Security fixes should include a
regression test and a changelog entry.

## Maintainer disclosure boundary

Keep detailed unresolved findings, reproduction material and deployment evidence
in private maintenance records. Publish reviewed summaries and necessary user
mitigations without machine-local identifiers or private operational details.
Redaction is not remediation: retain open work honestly and distinguish source
fixes, checks, rollout and real-environment acceptance. Ordinary file cleanup does
not remove older public history or previously downloaded copies.

## Research-preview limitations

The current source is an experimental research preview, not a production-stable release.
The historical tag `preview-2026.09.07-r1` addressed the AUD-12 file-tool and AUD-05
native-fee admission findings. Its [revision boundaries](docs/release/2026-09-07-preview-r1.md)
and [repair contract](docs/audit/2026-09-07-second-freeze-remediation.md) describe that
revision; they are not acceptance evidence for later source or a running deployment.
POSIX writes require trusted directory ownership/permissions and a dedicated
service account; hostile same-UID or privileged actors are not isolated.
Legacy ambiguous payment histories require reviewed reconciliation, not
automatic release of funds or rewriting historical journals.

### Cognition, expression and public reading

Both environment templates set `NOYRA_COGNITION_ENABLED=false`. Enabling cognition
spends configured model quota and enables Noyra's autonomous workflow. Within that
workflow, `NOYRA_AUTONOMOUS_EXPRESSION_ENABLED=true` is the default: Noyra may draft,
self-review and publish its own writing or send messages through configured active
transports using sustained authorization, without per-item human approval. A transport
must have usable credentials and routing; a new template does not supply them.
`NOYRA_WEB_READ_PUBLIC_BY_DEFAULT=true` permits public HTTPS reading through the
validated reader. Host and recipient allowlists are optional and off by default;
SSRF checks, untrusted-data handling, budgets and capability/lease checks still apply.

For an isolated evaluation, use a new disposable data directory, loopback binding,
no signer or real funds, and no file read/write grants. Keep cognition off, or set
`NOYRA_AUTONOMOUS_EXPRESSION_ENABLED=false` and restart to disable new autonomous
expression work while retaining cognition. To stop already queued external messages,
disable the relevant transport or revoke its sending grant; lifecycle pause is also
available. Disabling a feature does not undo a completed external effect. Dedicated
Noyra sending identities should be used for enabled channels.
See [autonomous expression and help](docs/implementation/c0-autonomous-expression.md)
for configuration and delivery semantics.

Economic capabilities have separate configuration and acceptance gates. The templates
keep wallet automation and automatic bounty publication off; enabling expression does
not enable payments or grant a signer. Source checks do not establish real-chain safety
or sustained real-world acceptance. Reusing a data directory retains its durable grants,
orders and history, so environment defaults alone do not revoke existing authority.

Never upload private runtime data, model transcripts, raw logs, exports, keys,
backups or unreviewed screenshots with the source. A secret scan cannot certify
the absence of every credential or piece of personal information.

## Deployment baseline

- Bind the service to loopback or put it behind a TLS reverse proxy.
- Use separate read, operator, export and break-glass tokens; rotate them on
  every suspected disclosure.
- Keep `NOYRA_ARCHIVE_ENCRYPTION_KEY` outside the database and back it up
  separately.
- Grant only the narrow capability scope required by a project.
- Do not enable a provider or channel with credentials embedded in a URL that
  is logged or shared publicly.

## Threat model

The model provider, world data, channel payloads and project artifacts are
untrusted inputs. The runtime must validate structured output locally, preserve
subject boundaries, quarantine unknown side effects, and keep private state out
of public projections and opt-in training exports.
