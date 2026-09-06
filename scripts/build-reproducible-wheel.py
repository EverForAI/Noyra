from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path


def _build(root: Path, output: Path) -> Path:
    output.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        [
            sys.executable,
            "-m",
            "pip",
            "wheel",
            "--no-deps",
            "--no-build-isolation",
            "--wheel-dir",
            str(output),
            str(root),
        ],
        cwd=root,
        env={**os.environ, "PYTHONHASHSEED": "0"},
        check=True,
    )
    wheels = sorted(output.glob("*.whl"))
    if len(wheels) != 1:
        raise RuntimeError(f"expected one wheel, found {len(wheels)} in {output}")
    return wheels[0]


def main() -> int:
    parser = argparse.ArgumentParser(description="Build and compare two reproducible Noyra wheels.")
    parser.add_argument("--output", type=Path, default=Path("dist"))
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    with tempfile.TemporaryDirectory(prefix="noyra-wheel-") as directory:
        temporary_root = Path(directory)
        environment_epoch = os.environ.get("SOURCE_DATE_EPOCH")
        if environment_epoch is None:
            environment_epoch = subprocess.check_output(
                ["git", "log", "-1", "--format=%ct"], cwd=root, text=True
            ).strip()
            os.environ["SOURCE_DATE_EPOCH"] = environment_epoch
        existing_build = root / "build"
        saved_build = temporary_root / "existing-build"
        if existing_build.exists():
            shutil.move(existing_build, saved_build)
        try:
            first = _build(root, temporary_root / "first")
            shutil.rmtree(existing_build, ignore_errors=True)
            second = _build(root, temporary_root / "second")
        finally:
            shutil.rmtree(existing_build, ignore_errors=True)
            if saved_build.exists():
                shutil.move(saved_build, existing_build)
        if first.read_bytes() != second.read_bytes():
            raise RuntimeError("reproducible wheel comparison failed")
        args.output.mkdir(parents=True, exist_ok=True)
        target = args.output / first.name
        shutil.copy2(first, target)
        print(target)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
