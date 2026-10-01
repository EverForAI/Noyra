# Unified Deployment Modes Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add one safe, idempotent `noyra setup` entry point for local research, public HTTPS, and Cloudflare Tunnel deployments while preserving the loopback-only Noyra service boundary.

**Architecture:** Keep deployment orchestration outside the runtime service in a small `deployment_setup` module. Pure validation, environment-file editing, proxy rendering, secret-file handling, and command execution are separate interfaces so tests can use fake runners without mutating a host. The CLI delegates to this module; systemd, Caddy, and cloudflared remain host integration points.

**Tech Stack:** Python 3.11+, `argparse`, `pathlib`, `ipaddress`, `urllib.parse`, `subprocess`, JSON metadata, systemd, Caddy, cloudflared, pytest, Ruff.

**Spec:** `docs/superpowers/specs/2026-10-01-deployment-modes-design.md`

## Global Constraints

- Noyra always listens on `127.0.0.1:8765` for native Ubuntu deployments.
- Provider API keys and Cloudflare Tunnel tokens must never be written to SQLite, ordinary exports, or logs.
- Configuration mutations use root-owned temporary files and atomic renames.
- Every mutation is preceded by a mode-specific backup and failed validation restores the previous configuration.
- `--dry-run` performs validation and rendering only; it does not mutate files or systemd state.
- Existing manual installation, reverse-proxy templates, and environment keys remain compatible.
- A command returns zero only after all selected local checks pass; failures use a stable `NOYRA_SETUP_<CODE>` prefix.

### Task 1: Deployment Setup Domain Layer

**Files:**
- Create: `src/noyra/deployment_setup.py`
- Test: `tests/test_deployment_setup.py`

**Interfaces:**
- Consumes: selected mode, hostname strings, environment-file path, Caddy path, optional tunnel-token path, and an injected `CommandRunner`.
- Produces: `SetupOptions`, `SetupError`, `SetupResult`, `validate_hostname()`, `parse_env_file()`, `update_env_text()`, `render_caddyfile()`, `redact_token()`, and `SetupRunner` methods used by later CLI tasks.

- [ ] **Step 1: Write failing pure validation tests**

```python
def test_validate_hostname_accepts_dns_name() -> None:
    assert validate_hostname("admin.example.com") == "admin.example.com"


@pytest.mark.parametrize("value", ["", "https://example.com", "../admin", "127.0.0.1:443"])
def test_validate_hostname_rejects_non_dns_host(value: str) -> None:
    with pytest.raises(SetupError, match="NOYRA_SETUP_INVALID_HOSTNAME"):
        validate_hostname(value)
```

- [ ] **Step 2: Run the focused tests and verify failure**

Run: `python -m pytest tests/test_deployment_setup.py -q`

Expected: collection fails because `noyra.deployment_setup` does not exist.

- [ ] **Step 3: Implement validation and rendering primitives**

Implement `SetupError(code: str, message: str)`, `SetupOptions` with explicit defaults, and validation that accepts only DNS hostnames (lowercase normalization, no scheme, path, port, wildcard, or control character). Implement `parse_env_file(text) -> list[tuple[str, str | None]]` preserving comments and unknown lines, and `update_env_text(text, updates) -> str` that replaces only known `NOYRA_*` assignments while preserving line endings and appending missing keys once. Implement `render_caddyfile(public_domain, admin_domain)` with two host blocks, `encode zstd gzip`, and `reverse_proxy 127.0.0.1:8765` plus Host/X-Forwarded headers. Implement `redact_token(value)` returning `"<redacted>"` for non-empty secrets.

- [ ] **Step 4: Add backup metadata helpers and command abstraction**

Define `CommandRunner.run(argv: Sequence[str], *, check: bool = True, input_text: str | None = None) -> CompletedProcess[str]` and a real `SubprocessRunner`. Add `BackupRecord` plus `create_backup(path, backup_root, mode)` that refuses symlinks, records SHA-256, owner/mode when available, and writes a `metadata.json` sidecar without secret content. Add `restore_backup(record)` with checksum verification and atomic replacement.

- [ ] **Step 5: Run focused tests and commit**

Run: `python -m pytest tests/test_deployment_setup.py -q` and `python -m ruff check src/noyra/deployment_setup.py tests/test_deployment_setup.py`.

Expected: all pure validation, rendering, redaction, backup, and environment-preservation tests pass.

Commit: `git add src/noyra/deployment_setup.py tests/test_deployment_setup.py && git commit -m "feat: add deployment setup domain layer"`

### Task 2: CLI Entry Point and Local Mode

**Files:**
- Modify: `src/noyra/__main__.py`
- Modify: `pyproject.toml`
- Test: `tests/test_deployment_setup.py`

**Interfaces:**
- Consumes: Task 1 primitives and `SetupRunner`.
- Produces: installed `noyra` console script and `noyra setup --mode local|public|cloudflare` parser with `--dry-run`, `--non-interactive`, `--replace`, `--env-file`, `--caddyfile`, `--backup-root`, and `--tunnel-token-file` options.

- [ ] **Step 1: Write failing CLI and local-mode tests**

```python
def test_setup_local_dry_run_does_not_mutate(tmp_path: Path) -> None:
    env_path = tmp_path / "noyra.env"
    env_path.write_text("NOYRA_HOST=127.0.0.1\n", encoding="utf-8")
    result = invoke_setup(["--mode", "local", "--dry-run", "--env-file", str(env_path)])
    assert result.exit_code == 0
    assert env_path.read_text(encoding="utf-8") == "NOYRA_HOST=127.0.0.1\n"
    assert "SSH" in result.stdout


def test_setup_local_rejects_public_listener(tmp_path: Path) -> None:
    env_path = tmp_path / "noyra.env"
    env_path.write_text("NOYRA_HOST=0.0.0.0\n", encoding="utf-8")
    result = invoke_setup(["--mode", "local", "--env-file", str(env_path)])
    assert result.exit_code != 0
    assert "NOYRA_SETUP_UNSAFE_LISTENER" in result.stderr
```

- [ ] **Step 2: Run the tests and verify failure**

Run: `python -m pytest tests/test_deployment_setup.py -q`

Expected: CLI helper and setup command are undefined.

- [ ] **Step 3: Add the `noyra` console script and parser dispatch**

Add `noyra = "noyra.__main__:main"` to `[project.scripts]`. Add `_setup_command(arguments)` in `__main__.py`; preserve all existing subcommands and return `SystemExit` with the stable error prefix. Parse the mode and common paths, require public/admin domains for public and Cloudflare modes in non-interactive operation, and only prompt with `getpass.getpass()` for a missing Cloudflare token when interactive.

- [ ] **Step 4: Implement local checks**

Implement `SetupRunner.run_local()` to validate root execution, an existing non-symlink environment file, `NOYRA_HOST=127.0.0.1`, `/var/lib/noyra` mount visibility when running on Linux, `systemctl is-active noyra`, and `curl --fail http://127.0.0.1:8765/health/live` plus `/health/ready`. Require at least one configured operator token source without printing its value. In dry-run, report the checks and SSH command `ssh -N -L 8765:127.0.0.1:8765 <server>` without writing files or invoking restart.

- [ ] **Step 5: Run tests and commit**

Run: `python -m pytest tests/test_deployment_setup.py -q` and `python -m ruff check src/noyra/__main__.py src/noyra/deployment_setup.py tests/test_deployment_setup.py`.

Commit: `git add src/noyra/__main__.py pyproject.toml tests/test_deployment_setup.py && git commit -m "feat: add noyra setup local mode"`

### Task 3: Public HTTPS Mode with Caddy

**Files:**
- Modify: `src/noyra/deployment_setup.py`
- Modify: `deploy/caddy/noyra.Caddyfile.example`
- Test: `tests/test_deployment_setup.py`
- Test: `tests/test_deployment_contract.py`

**Interfaces:**
- Consumes: Task 1 environment and backup helpers and Task 2 command runner.
- Produces: `SetupRunner.run_public()` that atomically updates `/etc/noyra/noyra.env`, publishes a Caddyfile, validates Caddy, restarts Noyra/Caddy, and rolls back both files on failure.

- [ ] **Step 1: Write failing public-mode tests**

```python
def test_public_mode_generates_two_https_origins_and_env_updates(tmp_path: Path) -> None:
    runner = FakeRunner(healthy=True)
    result = build_runner(runner, tmp_path).run_public(
        public_domain="hong168.win", admin_domain="admin.hong168.win", dry_run=False
    )
    assert result.exit_code == 0
    assert "https://hong168.win" in (tmp_path / "noyra.env").read_text(encoding="utf-8")
    caddy = (tmp_path / "Caddyfile").read_text(encoding="utf-8")
    assert "hong168.win" in caddy and "admin.hong168.win" in caddy
    assert "127.0.0.1:8765" in caddy


def test_public_mode_refuses_existing_proxy_without_replace(tmp_path: Path) -> None:
    caddy = tmp_path / "Caddyfile"
    caddy.write_text("existing\n", encoding="utf-8")
    result = build_runner(FakeRunner(), tmp_path).run_public(
        public_domain="example.com", admin_domain="admin.example.com", dry_run=False
    )
    assert result.exit_code != 0
    assert "NOYRA_SETUP_PROXY_EXISTS" in result.stderr
```

- [ ] **Step 2: Run tests and verify failure**

Run: `python -m pytest tests/test_deployment_setup.py tests/test_deployment_contract.py -q`

Expected: public-mode runner methods are missing.

- [ ] **Step 3: Implement secure public-mode mutation**

Require `--replace` before overwriting an existing real Caddyfile. Create a backup record for the env file and Caddyfile, render the two HTTPS blocks, update `NOYRA_PUBLIC_SITE_URL`, `NOYRA_TRUSTED_PROXY_CIDRS=127.0.0.1/32,::1/128`, and `NOYRA_ADMIN_SESSION_COOKIE_SECURE=true` using root-owned mode `0640` temporary files, then atomically replace. Run `caddy validate --config <path>`, `systemctl reload caddy`, `systemctl restart noyra`, and local health checks. For non-dry runs with DNS-ready domains, run HTTPS checks against both origins. Any validation, restart, or health failure restores both backups and restarts the known-good service.

- [ ] **Step 4: Update the Caddy example and contract tests**

Add explicit `header_up X-Real-IP {remote_host}`, a comment identifying the generated setup path, and a documented `admin.<domain>` origin without changing the loopback target. Extend contract tests to require the generated security values and both host blocks.

- [ ] **Step 5: Run tests and commit**

Run: `python -m pytest tests/test_deployment_setup.py tests/test_deployment_contract.py -q` and `python -m ruff check src/noyra/deployment_setup.py tests/test_deployment_setup.py`.

Commit: `git add src/noyra/deployment_setup.py deploy/caddy/noyra.Caddyfile.example tests/test_deployment_setup.py tests/test_deployment_contract.py && git commit -m "feat: add public HTTPS setup mode"`

### Task 4: Cloudflare Tunnel Mode

**Files:**
- Modify: `src/noyra/deployment_setup.py`
- Create: `deploy/systemd/cloudflared-noyra.service.example`
- Modify: `deploy/noyra.env.example`
- Test: `tests/test_deployment_setup.py`

**Interfaces:**
- Consumes: Task 3 environment mutation and health checks.
- Produces: `SetupRunner.run_cloudflare()` and a root-owned systemd unit that reads the tunnel token through a systemd credential path rather than an environment file.

- [ ] **Step 1: Write failing Cloudflare tests**

```python
def test_cloudflare_mode_writes_protected_token_file_without_logging_token(tmp_path: Path) -> None:
    secret = "cf-secret-token-value"
    token_path = tmp_path / "cloudflare-tunnel-token"
    result = build_runner(FakeRunner(cloudflared=True), tmp_path).run_cloudflare(
        public_domain="hong168.win", admin_domain="admin.hong168.win",
        tunnel_token=secret, token_path=token_path, dry_run=False
    )
    assert result.exit_code == 0
    assert token_path.read_text(encoding="utf-8") == secret + "\n"
    assert "cf-secret-token-value" not in result.stdout
    assert token_path.stat().st_mode & 0o077 == 0


def test_cloudflare_dry_run_does_not_write_token(tmp_path: Path) -> None:
    token_path = tmp_path / "cloudflare-tunnel-token"
    result = build_runner(FakeRunner(cloudflared=True), tmp_path).run_cloudflare(
        public_domain="example.com", admin_domain="admin.example.com",
        tunnel_token="secret", token_path=token_path, dry_run=True
    )
    assert result.exit_code == 0
    assert not token_path.exists()
```

- [ ] **Step 2: Run tests and verify failure**

Run: `python -m pytest tests/test_deployment_setup.py -q`

Expected: Cloudflare runner method and unit template are missing.

- [ ] **Step 3: Implement protected connector setup**

Require a token from `--tunnel-token-file` or a hidden prompt; reject symlinks, empty values, and group/world-readable files. Validate or install `cloudflared` through the command runner, write the token to the protected credential path with mode `0600`, publish the generated unit, run `systemctl daemon-reload` and `systemctl enable --now cloudflared-noyra`, then perform local readiness and external HTTPS checks. Never include the token in command output, result objects, exceptions, or logs. The public/admin env values match public mode. Roll back env, token file, and unit if any post-mutation check fails.

- [ ] **Step 4: Add the systemd unit and env comments**

Create a unit using `LoadCredential=tunnel-token:/etc/noyra/credentials/cloudflare-tunnel-token` and `ExecStart=/usr/bin/cloudflared tunnel --no-autoupdate run --token-file %d/tunnel-token`, with `After=network-online.target`, `Restart=on-failure`, `NoNewPrivileges=true`, and a read-only filesystem. Add comments to `deploy/noyra.env.example` stating that Cloudflare tokens belong in the credential file only.

- [ ] **Step 5: Run tests and commit**

Run: `python -m pytest tests/test_deployment_setup.py -q` and `python -m ruff check src/noyra/deployment_setup.py tests/test_deployment_setup.py`.

Commit: `git add src/noyra/deployment_setup.py deploy/systemd/cloudflared-noyra.service.example deploy/noyra.env.example tests/test_deployment_setup.py && git commit -m "feat: add cloudflare tunnel setup mode"`

### Task 5: Deployment Documentation and Release Gates

**Files:**
- Modify: `docs/deployment/ubuntu.md`
- Create: `docs/deployment/setup-modes.md`
- Modify: `scripts/audit-deployment.sh`
- Test: `tests/test_deployment_contract.py`

**Interfaces:**
- Consumes: the CLI commands and templates from Tasks 2-4.
- Produces: operator-facing Chinese/English-neutral command documentation, deployment audit checks, and repository contract coverage.

- [ ] **Step 1: Write failing documentation/audit contract tests**

```python
def test_setup_modes_document_all_three_entry_points() -> None:
    text = (ROOT / "docs/deployment/setup-modes.md").read_text(encoding="utf-8")
    for marker in ("noyra setup --mode local", "noyra setup --mode public", "noyra setup --mode cloudflare"):
        assert marker in text
    assert "127.0.0.1:8765" in text
    assert "cloudflare-tunnel-token" in text
```

- [ ] **Step 2: Implement operator documentation**

Document prerequisites, exact commands, `--dry-run`, DNS requirements, Cloudflare dashboard hostnames, rollback behavior, SSH fallback, and the rule that credentials are entered through protected files or hidden prompts. Add the one-command flow to `docs/deployment/ubuntu.md` while retaining the existing manual Caddy/Nginx flow.

- [ ] **Step 3: Add deployment audit checks**

Extend `scripts/audit-deployment.sh` to verify the setup module imports, the `noyra` console script declaration, the Cloudflare unit template, and the Caddy loopback target. Keep checks source-only so the audit still runs before a virtualenv exists.

- [ ] **Step 4: Run the complete focused gate and commit**

Run: `python -m pytest tests/test_deployment_setup.py tests/test_deployment_contract.py -q`, `python -m ruff check src/noyra tests/test_deployment_setup.py`, `bash -n scripts/audit-deployment.sh`, and `git diff --check`.

Commit: `git add docs/deployment/ubuntu.md docs/deployment/setup-modes.md scripts/audit-deployment.sh tests/test_deployment_contract.py && git commit -m "docs: document unified deployment setup"`

### Task 6: Full Verification and Clean Handoff

**Files:**
- Modify only files already listed above if verification exposes a concrete defect.

- [ ] **Step 1: Run the focused and neighboring tests**

Run: `python -m pytest tests/test_deployment_setup.py tests/test_deployment_contract.py tests/test_service_security_profile.py tests/test_web_contract.py -q`.

- [ ] **Step 2: Run static and packaging checks**

Run: `python -m ruff check src tests/test_deployment_setup.py tests/test_deployment_contract.py`, `python -m compileall -q src`, and `git diff --check`.

- [ ] **Step 3: Inspect the final diff and repository state**

Run: `git diff --stat HEAD~5..HEAD`, `git status --short --branch`, and verify no token-shaped test fixture is emitted into tracked files or logs.

- [ ] **Step 4: Commit any required verification-only fix**

Use a focused message such as `fix: correct deployment setup validation` and rerun the failed check before reporting completion.
