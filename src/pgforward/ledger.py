"""What the ledger says ran, compared with the files on disk. Read-only."""

import dataclasses
from collections.abc import Sequence
from typing import Literal

import psycopg

from pgforward import database, files, queries
from pgforward.errors import ConfigError, LedgerMismatch


@dataclasses.dataclass(frozen=True)
class Applied:
    filename: str
    checksum: str | None  # None: an earlier runner's row, not yet baselined
    applied_at: str | None
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
    # Applied here, missing from disk, and newer than every file on disk: the
    # database has run a newer release's migrations, as it does while old
    # instances still serve during a rolling deploy. Not a problem for them.
    ahead: list[str] = dataclasses.field(default_factory=list)

    @property
    def current(self) -> bool:
        return not self.pending and not self.problems


def read(conn: psycopg.Connection) -> list[Applied]:
    columns = {row[0] for row in conn.execute(queries.LEDGER_COLUMNS)}
    if not columns:
        return []
    if "filename" not in columns:
        raise ConfigError(
            f"public.schema_migrations has columns {', '.join(sorted(columns))}; "
            "pgforward reads a ledger with filename and checksum",
            _conversion(columns),
        )
    rows = [row[0] for row in conn.execute(queries.READ_LEDGER)]
    return [
        Applied(
            filename=row["filename"],
            checksum=row.get("checksum"),
            applied_at=row.get("applied_at"),
            package=row.get("package"),
            duration_ms=row.get("duration_ms"),
            out_of_order=bool(row.get("out_of_order")),
        )
        for row in rows
    ]


def _conversion(columns: set[str]) -> str:
    renames = []
    for old, new in (("name", "filename"), ("sha256", "checksum")):
        if old in columns and new not in columns:
            renames.append(
                f"ALTER TABLE public.schema_migrations RENAME COLUMN {old} TO {new};"
            )
    if renames:
        return f"rename its columns, then run pgforward again: {' '.join(renames)}"
    return (
        "rename the column holding each file's name to filename "
        "(ALTER TABLE public.schema_migrations RENAME COLUMN <column> TO filename), "
        "then run pgforward again"
    )


def compare(
    target: database.Target,
    migrations: Sequence[files.Migration],
    applied: Sequence[Applied],
) -> Status:
    on_disk = {m.filename: m for m in migrations}
    ran = {a.filename: a for a in applied}
    newest_on_disk = max(on_disk, default="")
    ahead = sorted(
        a.filename
        for a in applied
        if a.filename not in on_disk and a.filename > newest_on_disk
    )
    problems = [
        Problem("missing", a.filename)
        for a in applied
        if a.filename not in on_disk and a.filename not in ahead
    ] + [
        Problem("changed", a.filename)
        for a in applied
        if a.filename in on_disk
        and a.checksum is not None
        and on_disk[a.filename].checksum != a.checksum
    ]
    latest = max((name for name in ran if name in on_disk), default="")
    pending = [
        Pending(m, out_of_order=m.filename < latest)
        for m in migrations
        if m.filename not in ran
    ]
    return Status(target, list(applied), pending, problems, ahead)


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


def refuse_ahead(status: Status) -> None:
    """Refuse to migrate from code older than the database."""
    if status.ahead:
        raise LedgerMismatch(
            f"{status.target.describe()} has run migrations this code does not "
            f"have: {', '.join(status.ahead)}. It is ahead of this code",
            "migrate from the release that has them; an older release never "
            "migrates a newer database",
        )
