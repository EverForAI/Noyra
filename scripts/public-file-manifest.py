#!/usr/bin/env python3
"""Generate, check and export a reviewed public source inventory from exact Git blobs."""

from __future__ import annotations

import argparse
import hashlib
import re
import stat
import subprocess
import sys
import zipfile
from dataclasses import dataclass
from pathlib import Path

ALLOWLIST = "docs/release/public-files.txt"
CHECKSUMS = "docs/release/public-files.sha256"
EXCLUSIONS = "docs/release/public-files-excluded.txt"
REGULAR_MODES = {"100644", "100755"}


class ManifestError(Exception):
    """An inventory or source archive does not describe the selected Git snapshot."""


def git(repository: Path, *arguments: str, data: bytes | None = None) -> bytes:
    result = subprocess.run(
        ["git", *arguments], cwd=repository, input=data, capture_output=True, check=False
    )
    if result.returncode:
        raise ManifestError(result.stderr.decode("utf-8", errors="replace").strip())
    return result.stdout


def portable_path(value: str) -> str:
    parts = value.split("/")
    if (
        not value
        or any(ord(character) < 32 or ord(character) == 127 for character in value)
        or "\\" in value
        or ":" in value
        or any(part in {"", ".", ".."} or part.endswith((".", " ")) for part in parts)
        or any(part.casefold() == ".git" for part in parts)
        or any(re.match(r"(?i)^(con|prn|aux|nul|com[1-9]|lpt[1-9])(?:\.|$)", p) for p in parts)
    ):
        raise ManifestError(f"Unsafe or nonportable inventory path: {value!r}")
    return value


def lines(content: bytes, name: str) -> list[str]:
    try:
        text = content.decode("utf-8")
    except UnicodeDecodeError as error:
        raise ManifestError(f"{name} must contain UTF-8 metadata") from error
    if "\r" in text or text.startswith("\ufeff") or (text and not text.endswith("\n")):
        raise ManifestError(f"{name} must use LF, no BOM, and a final newline")
    return [line for line in text.splitlines() if line and not line.startswith("#")]


def unique_paths(paths: list[str], name: str) -> list[str]:
    seen: set[str] = set()
    for item in paths:
        portable_path(item)
        if item.casefold() in seen:
            raise ManifestError(f"Duplicate or case-colliding path in {name}: {item}")
        seen.add(item.casefold())
    directories: dict[str, str] = {}
    for item in paths:
        parts = item.split("/")
        for length in range(1, len(parts)):
            directory = "/".join(parts[:length])
            key = directory.casefold()
            if key in seen or directories.get(key, directory) != directory:
                raise ManifestError(f"File/directory or directory-case collision in {name}: {item}")
            directories[key] = directory
    if paths != sorted(paths):
        raise ManifestError(f"{name} paths must be sorted")
    return paths


@dataclass(frozen=True)
class Entry:
    mode: str
    oid: str


@dataclass
class Snapshot:
    repository: Path
    label: str
    entries: dict[str, Entry]

    def read(self, paths: list[str]) -> dict[str, bytes]:
        missing = sorted(set(paths) - self.entries.keys())
        if missing:
            raise ManifestError(f"Paths missing from {self.label}: {', '.join(missing[:12])}")
        for item in paths:
            if self.entries[item].mode not in REGULAR_MODES:
                raise ManifestError(f"Public file must be a regular Git blob: {item}")
        oids = list(dict.fromkeys(self.entries[item].oid for item in paths))
        if not oids:
            return {}
        response = git(
            self.repository, "cat-file", "--batch", data=("\n".join(oids) + "\n").encode("ascii")
        )
        offset = 0
        blobs: dict[str, bytes] = {}
        for oid in oids:
            end = response.find(b"\n", offset)
            header = response[offset:end].decode("ascii").split()
            if len(header) != 3 or header[:2] != [oid, "blob"]:
                raise ManifestError(f"Unable to read Git blob {oid}")
            size = int(header[2])
            offset = end + 1
            content = response[offset : offset + size]
            offset += size
            if len(content) != size or response[offset : offset + 1] != b"\n":
                raise ManifestError(f"Truncated Git blob {oid}")
            offset += 1
            blobs[oid] = content
        if offset != len(response):
            raise ManifestError("Unexpected Git batch output")
        return {item: blobs[self.entries[item].oid] for item in paths}


def snapshot(repository: Path, revision: str | None, index: bool) -> Snapshot:
    # Freeze object IDs first: subsequent reads never consult mutable worktree files.
    if index:
        label = "Git index"
        records = git(repository, "ls-files", "--stage", "-z").split(b"\0")
    else:
        label = (
            git(
                repository,
                "rev-parse",
                "--verify",
                "--end-of-options",
                (revision or "HEAD") + "^{commit}",
            )
            .decode("ascii")
            .strip()
        )
        records = git(repository, "ls-tree", "-r", "-z", "--full-tree", label).split(b"\0")
    entries: dict[str, Entry] = {}
    for record in filter(None, records):
        metadata, raw_path = record.split(b"\t", 1)
        fields = metadata.decode("ascii").split()
        item = raw_path.decode("utf-8")
        if index and fields[2] != "0":
            raise ManifestError(f"Resolve index conflicts before generating a manifest: {item}")
        entries[item] = Entry(fields[0], fields[1] if index else fields[2])
    return Snapshot(repository, label, entries)


@dataclass
class Inventory:
    source: Snapshot
    paths: list[str]
    excluded: list[str]
    blobs: dict[str, bytes]

    def checksums(self) -> bytes:
        return "".join(
            f"{hashlib.sha256(self.blobs[item]).hexdigest()}  {item}\n"
            for item in self.paths
            if item != CHECKSUMS
        ).encode("utf-8")

    def check(self) -> None:
        recorded: dict[str, str] = {}
        for row in lines(self.blobs[CHECKSUMS], CHECKSUMS):
            match = re.fullmatch(r"([0-9a-f]{64})  (.+)", row)
            if match is None:
                raise ManifestError(f"Invalid checksum entry: {row!r}")
            digest, item = match.groups()
            if item in recorded:
                raise ManifestError(f"Duplicate checksum entry: {item}")
            recorded[item] = digest
        unique_paths(list(recorded), CHECKSUMS)
        expected_paths = set(self.paths) - {CHECKSUMS}
        if set(recorded) != expected_paths:
            missing = sorted(expected_paths - recorded.keys())
            extra = sorted(recorded.keys() - expected_paths)
            raise ManifestError(
                f"Checksum coverage mismatch; missing={missing[:12]}, extra={extra[:12]}"
            )
        mismatches = [
            item
            for item, digest in recorded.items()
            if hashlib.sha256(self.blobs[item]).hexdigest() != digest
        ]
        if mismatches:
            raise ManifestError(
                f"{len(mismatches)} checksum mismatch(es): {', '.join(mismatches[:20])}"
            )
        # Reject extra whitespace/comments as well as wrong bytes, ensuring deterministic output.
        if self.blobs[CHECKSUMS] != self.checksums():
            raise ManifestError("Checksum manifest is not in canonical generated form")


def inventory(source: Snapshot) -> Inventory:
    metadata = source.read([ALLOWLIST, EXCLUSIONS])
    paths = unique_paths(lines(metadata[ALLOWLIST], ALLOWLIST), ALLOWLIST)
    excluded: list[str] = []
    for row in lines(metadata[EXCLUSIONS], EXCLUSIONS):
        item, separator, reason = row.partition("\t")
        if not separator or not reason.strip():
            raise ManifestError("Each exclusion needs an exact path, a tab, and a review reason")
        excluded.append(item)
    unique_paths(excluded, EXCLUSIONS)
    if not {ALLOWLIST, CHECKSUMS, EXCLUSIONS}.issubset(paths):
        raise ManifestError("The allowlist, checksums and exclusions must themselves be public")
    overlap = sorted(set(paths) & set(excluded))
    if overlap:
        raise ManifestError(f"Paths both public and excluded: {', '.join(overlap[:12])}")
    classified = set(paths) | set(excluded)
    unreviewed = sorted(source.entries.keys() - classified)
    absent = sorted(classified - source.entries.keys())
    if unreviewed or absent:
        raise ManifestError(
            f"Inventory coverage mismatch; unreviewed={unreviewed[:12]}, missing={absent[:12]}"
        )
    # A source archive must work on case-insensitive filesystems as well.
    unique_paths(sorted(classified), "combined inventory")
    blobs = source.read(paths)
    return Inventory(source, paths, excluded, blobs)


def verify_archive(reviewed: Inventory, archive: Path) -> None:
    with zipfile.ZipFile(archive) as package:
        infos = package.infolist()
        names = [portable_path(info.filename) for info in infos]
        unique_paths(names, "source archive")
        if set(names) != set(reviewed.paths):
            raise ManifestError("Source archive paths differ from the reviewed allowlist")
        for info in infos:
            expected = reviewed.blobs[info.filename]
            if (
                info.file_size != len(expected)
                or not stat.S_ISREG(info.external_attr >> 16)
                or stat.S_IMODE(info.external_attr >> 16)
                != stat.S_IMODE(int(reviewed.source.entries[info.filename].mode, 8))
                or package.read(info) != expected
            ):
                raise ManifestError(f"Source archive content or mode mismatch: {info.filename}")


def export_archive(reviewed: Inventory, output: Path) -> None:
    # Use exclusive creation; never replace an existing release artifact.
    with (
        output.open("xb") as stream,
        zipfile.ZipFile(stream, "w", compression=zipfile.ZIP_DEFLATED) as package,
    ):
        for item in reviewed.paths:
            info = zipfile.ZipInfo(item, date_time=(1980, 1, 1, 0, 0, 0))
            info.create_system = 3
            info.external_attr = int(reviewed.source.entries[item].mode, 8) << 16
            info.compress_type = zipfile.ZIP_DEFLATED
            package.writestr(info, reviewed.blobs[item])
    verify_archive(reviewed, output)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("check", "generate", "export", "verify-export"):
        command = commands.add_parser(name)
        command.add_argument("--repository", type=Path, default=Path(__file__).resolve().parents[1])
        selection = command.add_mutually_exclusive_group()
        selection.add_argument("--revision", help="Exact commit or ref; defaults to HEAD")
        selection.add_argument("--index", action="store_true", help="Read staged Git blob bytes")
        if name == "export":
            command.add_argument("--output", type=Path, required=True)
        if name == "verify-export":
            command.add_argument("--archive", type=Path, required=True)
    arguments = parser.parse_args()
    try:
        root = arguments.repository.resolve()
        if git(root, "rev-parse", "--show-prefix").strip():
            raise ManifestError("--repository must be the Git repository root")
        source = snapshot(root, arguments.revision, arguments.index)
        reviewed = inventory(source)
        if arguments.command == "generate":
            target = root / CHECKSUMS
            if target.resolve() != target:
                raise ManifestError("Refusing to write checksums through a filesystem link")
            target.write_bytes(reviewed.checksums())
        else:
            reviewed.check()
            if arguments.command == "export":
                export_archive(reviewed, arguments.output)
            elif arguments.command == "verify-export":
                verify_archive(reviewed, arguments.archive)
        print(
            f"{arguments.command}: {len(reviewed.paths)} public files, "
            f"{len(reviewed.paths) - 1} checksums, {len(reviewed.excluded)} excluded; "
            f"source={source.label}"
        )
    except (ManifestError, OSError, ValueError, zipfile.BadZipFile, RuntimeError) as error:
        print(f"Public source validation failed: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
