#!/usr/bin/env python3
"""Fixed root entrypoint for target activation requests and crash recovery."""

from __future__ import annotations

import sys

from noyra.migration.activation import (
    recover_incomplete_target_activations,
    run_target_activation_requests,
)


def main(argv: list[str] | None = None) -> int:
    arguments = sys.argv[1:] if argv is None else argv
    if arguments == []:
        run_target_activation_requests()
        return 0
    if arguments == ["--recover"]:
        recover_incomplete_target_activations()
        return 0
    raise SystemExit("unsupported target activation runner arguments")


if __name__ == "__main__":
    raise SystemExit(main())
