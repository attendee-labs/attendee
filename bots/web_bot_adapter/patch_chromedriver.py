#!/usr/bin/env python3
"""Blank ChromeDriver's cdc_ automation marker so pages cannot detect it.

The replacement is padded to the original block's length so no bytes in the
executable shift position, which would otherwise corrupt it.
"""

import pathlib
import re
import sys

MARKER_PATTERN = rb"\{window\.cdc_[A-Za-z0-9_]+ = window\.Array;[^}]*\}"


def patch_chromedriver(path: pathlib.Path) -> None:
    data = path.read_bytes()
    match = re.search(MARKER_PATTERN, data)
    if match is None:
        sys.exit("chromedriver automation marker block not found")
    blank = b"{}".ljust(len(match.group(0)))
    path.write_bytes(data[: match.start()] + blank + data[match.end() :])


def main() -> None:
    if len(sys.argv) != 2:
        sys.exit(f"usage: {sys.argv[0]} <chromedriver-path>")
    patch_chromedriver(pathlib.Path(sys.argv[1]))


if __name__ == "__main__":
    main()
