from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_base_and_cloud_profiles_are_separate_exact_locks() -> None:
    base = (ROOT / "requirements.lock").read_text(encoding="utf-8")
    cloud = (ROOT / "requirements-cloud.lock").read_text(encoding="utf-8")
    assert "boto3" not in base.casefold()
    assert "boto3==1.43.72" in cloud
    assert "botocore==1.43.72" in cloud
    assert "s3transfer==0.19.2" in cloud
    requirements = [
        line.strip().removesuffix("\\").strip()
        for line in cloud.splitlines()
        if line.strip() and not line.lstrip().startswith(("#", "--hash="))
    ]
    assert requirements
    assert all(re.fullmatch(r"[A-Za-z0-9_.-]+==[^\s;]+", line) for line in requirements)
    assert "--hash=sha256:" in cloud


def test_ubuntu_docker_and_ci_select_and_verify_both_profiles() -> None:
    installer = (ROOT / "scripts" / "install-ubuntu.sh").read_text(encoding="utf-8")
    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    compose = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
    workflow = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    assert "base|cloud" in installer
    assert "requirements-cloud.lock" in installer
    assert "--require-hashes" in installer
    assert "NOYRA_INSTALL_PROFILE=base" in dockerfile
    assert "requirements-cloud.lock" in dockerfile
    assert "--require-hashes" in dockerfile
    assert "NOYRA_INSTALL_PROFILE: ${NOYRA_INSTALL_PROFILE:-base}" in compose
    assert "NOYRA_INSTALL_PROFILE=base" in workflow
    assert "NOYRA_INSTALL_PROFILE=cloud" in workflow
    assert "requirements-cloud.lock" in workflow
    assert "--require-hashes" in workflow


def test_ubuntu_installer_uses_atomic_release_lifecycle_and_rollback() -> None:
    installer = (ROOT / "scripts" / "install-ubuntu.sh").read_text(encoding="utf-8")
    unit = (ROOT / "deploy" / "systemd" / "noyra.service").read_text(encoding="utf-8")
    deployment = (ROOT / "docs" / "deployment" / "ubuntu.md").read_text(encoding="utf-8")

    assert 'RELEASES_DIR="$INSTALL_DIR/releases"' in installer
    assert 'CURRENT_LINK="$INSTALL_DIR/current"' in installer
    assert 'PREVIOUS_LINK="$INSTALL_DIR/previous"' in installer
    assert "flock -n 9" in installer
    assert 'python3 -m venv "$staging/.venv"' in installer
    assert 'mv -- "$staging" "$release_root"' in installer
    assert '"$staging/.venv/bin/python" -m pip check' in installer
    assert "mv -Tf" in installer
    assert "health/ready" in installer
    assert "--rollback" in installer
    assert "requirements-cloud.lock" in installer
    assert "cold encrypted backup" in installer
    assert "NOYRA_UPGRADE_BACKUP_DIR" in installer
    assert "Backup directory path must be root-owned and not group/other writable" in installer
    assert "--backup-dir must be root:root and not group/other writable" in installer
    assert "backup_dir_default" in installer
    assert "assert_backup_dir_identity" in installer
    assert "Failed to remove stale backup staging" in installer
    assert "Backup keyring must not be hard-linked" in installer
    assert "runuser --user=noyra --group=noyra" in installer
    assert 'mktemp -d "$backup_dir/.noyra-staging.XXXXXX"' in installer
    assert 'backup_lock_path="$backup_staging/.noyra-staging.lock"' in installer
    assert 'flock --exclusive -- "$backup_lock_path" runuser' in installer
    assert 'flock -n -- "$lock_path" /usr/bin/true' in installer
    assert 'chown root:root "$backup_staging"' in installer
    assert 'chown "root:$noyra_gid" "$backup_staging"' in installer
    assert 'chmod 1770 "$backup_staging"' in installer
    assert 'chmod 0600 "$staged_backup"' in installer
    assert "backup_dir_exposed=true" in installer
    assert 'for candidate in "$backup_dir"/.noyra-staging.*' in installer
    assert 'candidate_metadata="$(stat -c' in installer
    assert 'marker_metadata="$(stat -c' in installer
    assert "cleanup_failed=false" in installer
    assert "Upgrade cleanup did not restore a safe backup boundary" in installer
    assert "backup_dir_exposed=false" in installer
    assert "runuser --user=noyra --group=noyra -- /usr/bin/env" in installer
    assert 'rm -f -- "$backup_staging/.noyra-staging-marker"' in installer
    assert 'rmdir -- "$backup_staging"' in installer
    assert "profile.conf" in installer
    assert "S3 archive is configured; install with --profile cloud instead of base." in installer
    assert "/opt/noyra/current/.venv/bin/python" in unit
    assert "WorkingDirectory=/opt/noyra/current" in unit
    assert "ExecStartPre=/usr/bin/test -x /opt/noyra/current/.venv/bin/python" in unit
    assert "Code rollback is not a data rollback" in deployment
