"""Inspect the selected interpreter without importing or starting the product."""

from __future__ import annotations

import json
import sqlite3
import sys


def sqlite_has_wal_reset_fix(version: tuple[int, ...]) -> bool:
    """Official WAL-reset fix and maintained backports; 3.52.0 was withdrawn."""
    return version != (3, 52, 0) and (
        version >= (3, 51, 3)
        or (3, 50, 7) <= version < (3, 51, 0)
        or (3, 44, 6) <= version < (3, 45, 0)
    )


def main() -> int:
    with sqlite3.connect(":memory:") as connection:
        source_id = connection.execute("select sqlite_source_id()").fetchone()[0]
    supported = sys.version_info[:2] == (3, 13) and sqlite_has_wal_reset_fix(
        sqlite3.sqlite_version_info
    )
    print(json.dumps({
        "python": sys.executable,
        "python_version": ".".join(map(str, sys.version_info[:3])),
        "sqlite_version": sqlite3.sqlite_version,
        "sqlite_source_id": source_id,
        "supported": supported,
    }))
    return 0 if supported else 1


if __name__ == "__main__":
    raise SystemExit(main())
