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

## Research-preview limitations

This source preview is not a production-stable release. AUD-12 (directory
replacement races in file tools) and AUD-05 (native transaction-fee admission
for token payments) remain open P1 risks. See the
[current audit correction](docs/audit/2026-09-05-comprehensive-audit-remediation.md).
For the preview, use a new disposable data directory, loopback binding, no wallet
signer, no automatic payments or publishing, no file read/write grants, and no
real funds. Disabling exposure is not a fix. Signing, bounded file operations,
autonomous task bounties, payments and tips remain in the project roadmap;
enable them only after the relevant fixes and deployment acceptance.

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
