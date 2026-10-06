from __future__ import annotations

import configparser
import re
import shlex
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_release_manifest_covers_every_systemd_python_entrypoint() -> None:
    helper = (ROOT / "scripts" / "lib" / "release-scripts.sh").read_text(encoding="utf-8")
    manifest = dict(
        (name, int(mode, 8))
        for mode, name in re.findall(r"'([0-7]{4}):([A-Za-z0-9_.-]+\.py)'", helper)
    )
    assert manifest["preflight-production.py"] == 0o644
    referenced = set()
    prefix = "/opt/noyra/current/scripts/"
    for path in (ROOT / "deploy" / "systemd").glob("*.service"):
        unit = configparser.ConfigParser(interpolation=None, strict=False)
        unit.read(path, encoding="utf-8")
        for key, command in unit.items("Service"):
            if not key.startswith("exec"):
                continue
            for argument in shlex.split(command):
                if not argument.startswith(prefix):
                    continue
                name = argument.removeprefix(prefix)
                assert name in manifest, f"{path.name} references an unpackaged script: {name}"
                assert (ROOT / "scripts" / name).is_file()
                if unit.get("Service", "User", fallback="root") != "root":
                    assert manifest[name] & 0o004, f"{path.name} cannot read {name}"
                assert manifest[name] & 0o022 == 0, f"{name} is writable by a non-root user"
                referenced.add(name)
    assert referenced == {"noyra-target-activation-runner.py", "noyra-migration-agent.py"}


def test_installer_checks_packaged_entrypoints_before_stopping_old_service() -> None:
    installer = (ROOT / "scripts" / "install-ubuntu.sh").read_text(encoding="utf-8")
    assert 'source "$SOURCE_DIR/scripts/lib/release-scripts.sh"' in installer
    prepare = installer.index('noyra_install_release_scripts "$SOURCE_DIR" "$staging"')
    stop = installer.index("\nstop_old_service\n", prepare)
    publish = installer.index('mv -- "$staging" "$release_root"', stop)
    switch = installer.index('atomic_pointer "$CURRENT_LINK" "$release_id"', publish)
    assert prepare < stop < publish < switch


def test_native_installer_provisions_root_runner_and_source_sha_marker() -> None:
    installer = (ROOT / "scripts" / "install-ubuntu.sh").read_text(encoding="utf-8")
    assert 'UPGRADE_INSTALL_DIR="$INSTALL_DIR/upgrade"' in installer
    assert 'install -d -o root -g noyra -m 0750 "$upgrade_state_dir"' in installer
    assert 'install -d -o noyra -g noyra -m 0700 "$upgrade_request_dir"' in installer
    assert 'install -d -o root -g root -m 0700 "$upgrade_processing_dir"' in installer
    assert (
        'install -o root -g root -m 0750 "$SOURCE_DIR/scripts/upgrade-ubuntu-runner.sh"'
        in installer
    )
    assert "systemctl enable --now noyra-upgrade.path" in installer
    assert "systemctl enable noyra-upgrade-recover.service" in installer
    assert 'printf \'%s\\n\' "$source_sha" > "$release_root/.noyra-source-sha"' in installer
    assert 'install -o root -g noyra -m 0660 /dev/null "$UPGRADE_HANDOFF_LOCK"' in installer


def test_installer_rolls_back_root_upgrade_components_with_failed_release() -> None:
    installer = (ROOT / "scripts" / "install-ubuntu.sh").read_text(encoding="utf-8")
    helper = ROOT / "scripts" / "lib" / "upgrade-components.sh"
    assert helper.is_file()
    assert 'source "$SOURCE_DIR/scripts/lib/upgrade-components.sh"' in installer
    snapshot = installer.index("noyra_upgrade_components_snapshot ")
    mark_changed = installer.index("noyra_upgrade_components_mark_changed")
    install_runner = installer.index('"$SOURCE_DIR/scripts/upgrade-ubuntu-runner.sh"')
    rollback = installer.index("noyra_upgrade_components_restore", installer.index("on_error()"))
    commit = installer.index("noyra_upgrade_components_commit")
    readiness = installer.index("if start_and_check; then")
    assert snapshot < mark_changed < install_runner
    assert rollback < readiness < commit


def test_manager_and_runner_support_fresh_install_checkout_permissions() -> None:
    manager = (ROOT / "src" / "noyra" / "core" / "upgrade.py").read_text(encoding="utf-8")
    runner = (ROOT / "scripts" / "upgrade-ubuntu-runner.sh").read_text(encoding="utf-8")
    assert "if not self.source_path.exists():" in manager
    assert 'f"safe.directory={repository}"' in manager
    assert 'chmod -R a+rX "$SOURCE_DIR"' in runner


def test_privileged_runner_is_fixed_official_and_does_not_load_application_secrets() -> None:
    runner = (ROOT / "scripts" / "upgrade-ubuntu-runner.sh").read_text(encoding="utf-8")
    manager = (ROOT / "src" / "noyra" / "core" / "upgrade.py").read_text(encoding="utf-8")
    service = (ROOT / "deploy" / "systemd" / "noyra-upgrade.service").read_text(encoding="utf-8")
    path = (ROOT / "deploy" / "systemd" / "noyra-upgrade.path").read_text(encoding="utf-8")
    recovery = (ROOT / "deploy" / "systemd" / "noyra-upgrade-recover.service").read_text(
        encoding="utf-8"
    )
    assert "REMOTE_URL=https://github.com/EverForAI/Noyra.git" in runner
    assert 'target_sha" == "$latest_sha"' in runner
    assert 'bash "$SOURCE_DIR/scripts/install-ubuntu.sh"' in runner
    assert "EnvironmentFile=" not in service
    assert "PathExists=/var/lib/noyra/upgrade/requests/pending.json" in path
    assert "--recover-only" in recovery
    assert "--recover-only" in (
        ROOT / "deploy" / "systemd" / "noyra-upgrade-recover.service"
    ).read_text(encoding="utf-8")
    assert 'ProcessLock(self.status_path.parent / "manager.lock")' in manager
    assert 'HANDOFF_LOCK_PATH="$UPGRADE_ROOT/manager.lock"' in runner
    assert "flock -x 8" in runner


def test_upgrade_management_documentation_describes_scope_and_recovery() -> None:
    documentation = (ROOT / "docs" / "deployment" / "ubuntu.md").read_text(encoding="utf-8")
    assert "### Upgrade from the management page" in documentation
    assert "closing the tab" in documentation
    assert "Docker and other deployment" in documentation
    assert "noyra-upgrade.service" in documentation
