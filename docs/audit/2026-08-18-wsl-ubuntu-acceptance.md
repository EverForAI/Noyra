# 2026-08-18 WSL2 Ubuntu Acceptance Evidence

The host now has WSL2 Ubuntu 24.04.4 LTS with the locked development environment installed
under `/root/noyra-m42` on the Linux filesystem. The working-tree snapshot was copied from the
Windows project directory without `.git`, caches, or the Windows virtual environment.

## Results

- Full Ubuntu suite: `746 passed, 3 skipped` in 25m25s.
- Focused platform suite: `22 passed, 1 skipped`.
- P2-13 POSIX symlink escape test passed; the Windows reparse-point test is the complementary
  Windows-only case and passed in the Windows run.
- P2-18 keyed-directory symlink rejection passed; Windows subject ingress/path checks passed in
  the complementary Windows run.
- Ubuntu installer `--profile base` completed and provisioned the service, keyring, data and
  configuration paths.
- Ubuntu installer `--profile cloud` completed and imported `boto3==1.43.72` from
  `requirements-cloud.lock`.
- Linux strict Mypy passed after making the platform-only Windows and POSIX APIs explicit to the
  type checker.

## Docker Profile Follow-up

The Docker Hub connection recovered after restarting Docker Desktop. The pinned base and cloud
images were built successfully and their container imports passed; the detailed image digests and
profile evidence are recorded in `docs/audit/2026-08-18-p2-15-docker-acceptance.md`.
