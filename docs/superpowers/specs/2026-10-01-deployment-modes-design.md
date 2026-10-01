# Noyra Deployment Modes Design

**Status:** Proposed
**Date:** 2026-10-01

## Goal

Provide one supported setup flow for three Ubuntu deployment modes while preserving the current
release installer, loopback-only service boundary, secret-file policy, and rollback behavior.

## Scope

The feature adds a deployment setup entry point for an already installed Noyra release. It does
not change the subject database schema, cognition behavior, wallet policy, or public HTTP
contracts. The existing manual installer and hand-written reverse-proxy examples remain supported.

The initial implementation targets Ubuntu systemd deployments. Windows and Docker remain covered
by their existing workflows and are not silently changed by this feature.

## User-facing modes

### Local research mode

`noyra setup --mode local` keeps Noyra bound to `127.0.0.1:8765` and writes no public proxy
configuration. It validates the native data mount, service account, operator/read/export tokens,
backup keyring, current release, and live/ready health checks. It prints the SSH forwarding
command and local admin URL without printing any secret values.

The mode is idempotent. Existing environment values are preserved unless the operator explicitly
provides a replacement. It must not enable a public listener or alter firewall rules.

### Public HTTPS mode

`noyra setup --mode public --public-domain <host> --admin-domain <host>` configures a same-host
Caddy reverse proxy for two HTTPS origins. The public and admin hostnames both proxy to
`127.0.0.1:8765`; the service remains loopback-only. Caddy owns certificate acquisition and
renewal. The setup flow validates DNS reachability prerequisites, writes a root-owned proxy
configuration, sets `NOYRA_PUBLIC_SITE_URL` to the public origin, enables trusted loopback proxy
headers, enables secure admin cookies, validates the proxy configuration, and performs live/ready
and HTTPS checks before reporting success.

The flow must refuse to overwrite an existing proxy configuration unless `--replace` is supplied.
Before replacement it stores a mode-specific backup with owner, mode, and checksum metadata.

Nginx remains supported through the checked-in template, but the first setup implementation uses
Caddy because it can obtain and renew certificates without a separate certificate command.

### Cloudflare Tunnel mode

`noyra setup --mode cloudflare --public-domain <host> --admin-domain <host>` installs or validates
`cloudflared`, accepts a Tunnel token through a hidden prompt or a protected file, enables the
`cloudflared` systemd service, and validates connector health. It writes the same Noyra proxy
environment values as public mode and verifies that the configured public hostnames resolve over
HTTPS to the local service.

Cloudflare account operations that require interactive authorization remain in the Cloudflare
dashboard: creating the Tunnel and adding its public hostnames. The setup command must print the
exact origin target (`http://127.0.0.1:8765`) and required hostnames, but must never print or persist
the token in ordinary configuration, logs, exports, or the subject database.

The command must not expose port 8765 or change `NOYRA_HOST` away from loopback. It must support a
dry-run that checks the installed connector and configuration without changing systemd state.

## Shared configuration contract

The setup flow uses these values:

- `NOYRA_PUBLIC_SITE_URL` is set only for public or Cloudflare mode.
- `NOYRA_TRUSTED_PROXY_CIDRS` is `127.0.0.1/32,::1/128` for same-host proxying.
- `NOYRA_ADMIN_SESSION_COOKIE_SECURE=true` for public or Cloudflare mode.
- `NOYRA_ADMIN_SESSION_TTL_SECONDS` defaults to the existing supported twelve-hour value unless
  explicitly supplied.
- `NOYRA_HOST` remains `127.0.0.1` and `NOYRA_PORT` remains `8765` unless the existing native
  deployment has an explicitly supported alternative.

The setup flow edits only known keys in `/etc/noyra/noyra.env`, preserving comments and unrelated
settings. It creates a mode backup before the first mutation and restores that backup if validation
or service restart fails.

## Safety and failure behavior

The setup command runs as root, validates the caller's mode and paths, and refuses unsafe symlinks,
group/world-readable secret files, non-loopback Noyra listeners, and missing encrypted native
storage. It uses temporary root-owned files and atomic renames for configuration publication.

For every mode, failure before service restart leaves the active service and configuration untouched.
Failure after restart restores the previous configuration and starts the known-good service. Public
and Cloudflare modes never claim success until the local readiness endpoint returns `200` and the
configured external URL returns the expected status. External DNS propagation failures are
reported as pending prerequisites rather than silently weakening the listener boundary.

## CLI contract

The setup entry point exposes:

```text
noyra setup --mode local [--dry-run]
noyra setup --mode public --public-domain HOST --admin-domain HOST [--dry-run] [--replace]
noyra setup --mode cloudflare --public-domain HOST --admin-domain HOST [--tunnel-token-file PATH] [--dry-run]
```

Interactive prompts are allowed when required values are omitted. `--non-interactive` requires all
values needed by the selected mode and fails with an actionable message when one is missing.

The command returns zero only after all local checks pass. It returns a non-zero status with a
machine-readable error code prefix for invalid input, missing prerequisite, unsafe configuration,
proxy failure, connector failure, or health-check failure.

## Tests and evidence

The implementation must add tests for:

- mode and hostname validation;
- preservation and atomic update of environment files;
- dry-run having no filesystem or systemd mutation;
- local mode refusing public listeners;
- generated Caddy configuration and replacement refusal;
- Cloudflare token redaction and protected-file handling;
- rollback after failed proxy validation or readiness;
- CLI exit codes and actionable errors.

The deployment checks must include shell syntax, static checks, focused unit tests, generated
configuration validation, and a local fake-systemd/fake-network integration path. Real Cloudflare
and certificate issuance remain external deployment evidence and are not faked as unit-test
success.

## Non-goals

- Automatic Cloudflare account login or unrestricted Cloudflare API access.
- Automatic purchase or registration of domains.
- Changing the Noyra service to listen on a public interface.
- Replacing the existing release installer or database migration process.
- Storing provider keys, Tunnel tokens, or private configuration in SQLite or ordinary exports.
