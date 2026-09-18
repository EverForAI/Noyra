import argparse
import getpass
import json
import os
import sys
from pathlib import Path
from typing import NoReturn

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


class _WalletArgumentParser(argparse.ArgumentParser):
    """Argument parser that never echoes untrusted argument values."""

    def error(self, message: str) -> NoReturn:  # pragma: no cover - exercised through subprocess
        del message
        self.exit(2, "wallet setup arguments are invalid\n")


def _wallet_setup_command(arguments: list[str]) -> None:
    """Create or import an encrypted local wallet from hidden prompts."""
    from noyra.wallet.keystore import create_keystore, read_password_file

    parser = _WalletArgumentParser(prog="python -m noyra wallet-setup", add_help=True)
    parser.add_argument("action", nargs="?", choices=("init", "import"), default="init")
    parser.add_argument("--path", "--keystore", dest="path", required=True, type=Path)
    parser.add_argument("--password-file", type=Path)
    try:
        options = parser.parse_args(arguments)
        if options.password_file is None:
            password = getpass.getpass("Wallet password: ")
        else:
            password = read_password_file(options.password_file)
        private_key: str | None = None
        if options.action == "import":
            private_key = getpass.getpass("Private key (hidden): ")
        address = create_keystore(options.path, password, private_key=private_key)
    except SystemExit:
        raise
    except Exception as exc:
        # Never expose paths, dependency errors, passwords, or key material.
        del exc
        print("wallet setup failed", file=sys.stderr)
        raise SystemExit(2) from None
    print(json.dumps({"address": address}, sort_keys=True))


def _wallet_command(arguments: list[str]) -> None:
    """Compatibility wrapper for ``wallet init|import --keystore PATH``."""
    parser = _WalletArgumentParser(prog="python -m noyra wallet")
    parser.add_argument("action", choices=("init", "import"))
    parser.add_argument("--keystore", required=True, type=Path)
    parser.add_argument("--password-file", type=Path)
    options = parser.parse_args(arguments)
    forwarded = [options.action, "--path", str(options.keystore)]
    if options.password_file is not None:
        forwarded.extend(("--password-file", str(options.password_file)))
    _wallet_setup_command(forwarded)


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
    if len(sys.argv) > 1 and sys.argv[1] == "wallet-setup":
        _wallet_setup_command(sys.argv[2:])
        return
    if len(sys.argv) > 1 and sys.argv[1] == "wallet":
        _wallet_command(sys.argv[2:])
        return
    print(f"Noyra {__version__} - artificial subject runtime")


if __name__ == "__main__":
    main()
