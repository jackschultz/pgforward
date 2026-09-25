"""What the ledger says ran, compared with the files on disk. Read-only."""

import dataclasses
from collections.abc import Sequence
from typing import Literal

import psycopg

from pgforward import database, files, queries
from pgforward.errors import LedgerMismatch


@dataclasses.dataclass(frozen=True)
class Applied:
    filename: str
    checksum: str
    applied_at: str
    package: str | None
    duration_ms: int | None
    out_of_order: bool


@dataclasses.dataclass(frozen=True)
class Pending:
    migration: files.Migration
    # Older than a file already applied: a branch merged after later work ran.
    out_of_order: bool


@dataclasses.dataclass(frozen=True)
class Problem:
    kind: Literal["changed", "missing"]
    filename: str


@dataclasses.dataclass(frozen=True)
class Status:
    target: database.Target
    applied: list[Applied]
    pending: list[Pending]
    problems: list[Problem]

    @property
    def current(self) -> bool:
        return not self.pending and not self.problems


def read(conn: psycopg.Connection) -> list[Applied]:
    if not database.one(conn, queries.LEDGER_EXISTS)[0]:
        return []
    rows = [row[0] for row in conn.execute(queries.READ_LEDGER)]
    return [
        Applied(
            filename=row["filename"],
            checksum=row["checksum"],
            applied_at=row["applied_at"],
            package=row.get("package"),
            duration_ms=row.get("duration_ms"),
            out_of_order=bool(row.get("out_of_order")),
        )
        for row in rows
    ]


def compare(
    target: database.Target,
    migrations: Sequence[files.Migration],
    applied: Sequence[Applied],
) -> Status:
    on_disk = {m.filename: m for m in migrations}
    ran = {a.filename: a for a in applied}
    problems = [
        Problem("missing", a.filename) for a in applied if a.filename not in on_disk
    ] + [
        Problem("changed", a.filename)
        for a in applied
        if a.filename in on_disk and on_disk[a.filename].checksum != a.checksum
    ]
    latest = max(ran, default="")
    pending = [
        Pending(m, out_of_order=m.filename < latest)
        for m in migrations
        if m.filename not in ran
    ]
    return Status(target, list(applied), pending, problems)


def status(conn: psycopg.Connection, migrations: Sequence[files.Migration]) -> Status:
    with conn.transaction():
        conn.execute("SET TRANSACTION READ ONLY")
        return compare(database.target(conn), migrations, read(conn))


def refuse(status: Status) -> None:
    """Raise when the ledger and the files disagree, with the fix for this kind."""
    if not status.problems:
        return
    changed = [p.filename for p in status.problems if p.kind == "changed"]
    missing = [p.filename for p in status.problems if p.kind == "missing"]
    parts = []
    if changed:
        parts.append(f"applied migration changed since it ran: {', '.join(changed)}")
    if missing:
        parts.append(f"applied migration missing from disk: {', '.join(missing)}")
    if status.target.kind in database.DISPOSABLE:
        fix = (
            "this database is disposable: `pgforward rebuild` drops it and applies "
            "every file again"
        )
    else:
        fix = (
            "on a database that is not disposable an applied file is never edited: "
            "restore it from git, and make the change in a new file "
            "(`pgforward new <description>`)"
        )
    raise LedgerMismatch(f"{status.target.describe()}: {'; '.join(parts)}", fix)
