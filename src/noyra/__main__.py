import argparse
import json
import os
import sys
from pathlib import Path

from noyra import __version__


def _backup_key_command(arguments: list[str]) -> None:
    from noyra.core.at_rest import BackupKeyring

    parser = argparse.ArgumentParser(prog="python -m noyra backup-key")
    parser.add_argument("action", choices=("init", "rotate"))
    parser.add_argument("--path", required=True, type=Path)
    options = parser.parse_args(arguments)
    if options.action == "init":
        keyring = BackupKeyring.initialize(options.path)
    else:
        keyring = BackupKeyring.rotate(options.path)
    print(json.dumps(keyring.metadata(), sort_keys=True))


def _backup_command(arguments: list[str], *, restore: bool) -> None:
    from noyra.core.at_rest import AtRestConfig, AtRestGuard, EncryptedBackupManager

    parser = argparse.ArgumentParser(
        prog="python -m noyra restore-backup" if restore else "python -m noyra backup"
    )
    if restore:
        parser.add_argument("--input", required=True, type=Path)
        parser.add_argument(
            "--target",
            type=Path,
            default=Path(os.getenv("NOYRA_DATA_DIR", ".runtime/data")),
        )
        options = parser.parse_args(arguments)
        data_root = options.target
    else:
        parser.add_argument("--output", required=True, type=Path)
        options = parser.parse_args(arguments)
        data_root = Path(os.getenv("NOYRA_DATA_DIR", ".runtime/data"))
    config = AtRestConfig.from_env(data_root)
    if config.backup_keyring_path is None:
        parser.error("NOYRA_BACKUP_KEYRING_PATH is required")
    guard = AtRestGuard(config)
    guard.prepare()
    guard.require_ready()
    manager = EncryptedBackupManager(data_root, config.backup_keyring_path)
    if restore:
        restored = manager.restore(options.input, data_root)
        guard.post_initialize()
        guard.require_ready()
        print(json.dumps({"restored": str(restored)}, sort_keys=True))
    else:
        artifact = manager.create(options.output)
        print(json.dumps(artifact.public(), sort_keys=True))


def main() -> None:
    if len(sys.argv) > 1 and sys.argv[1] == "serve":
        del sys.argv[1]
        from noyra.service import main as service_main

        service_main()
        return
    if len(sys.argv) > 1 and sys.argv[1] == "backup-key":
        _backup_key_command(sys.argv[2:])
        return
    if len(sys.argv) > 1 and sys.argv[1] == "backup":
        _backup_command(sys.argv[2:], restore=False)
        return
    if len(sys.argv) > 1 and sys.argv[1] == "restore-backup":
        _backup_command(sys.argv[2:], restore=True)
        return
    print(f"Noyra {__version__} - artificial subject runtime")


if __name__ == "__main__":
    main()
