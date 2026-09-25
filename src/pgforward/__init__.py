"""Postgres migrations in plain SQL files, forward only.

`pgforward guide` prints how to use it. From Python:

    pgforward.migrate(url)            apply what is pending
    pgforward.pending(url)            file names not yet applied, for /health
    pgforward.status(url)             applied, pending, and any disagreement
    pgforward.testing.prepare(url)    bring a test database up to date, guarded

`packages` defaults to [tool.pgforward] packages in the nearest pyproject.toml.
"""

import dataclasses
import pathlib
from collections.abc import Sequence

from pgforward import apply, config, database, files, ledger, schema, testing
from pgforward.apply import Echo, Ran, Result
from pgforward.database import Kind, Target
from pgforward.errors import (
    ConfigError,
    LedgerMismatch,
    LockTimeout,
    MigrationFailed,
    PgforwardError,
    Refused,
    SchemaDumpFailed,
)
from pgforward.ledger import Status

__all__ = [
    "ConfigError",
    "Echo",
    "Kind",
    "LedgerMismatch",
    "LockTimeout",
    "MigrationFailed",
    "PgforwardError",
    "Ran",
    "Refused",
    "Result",
    "SchemaDumpFailed",
    "Status",
    "Target",
    "mark",
    "migrate",
    "pending",
    "rebuild",
    "status",
    "testing",
    "write_schema",
]


def migrate(
    url: str,
    packages: Sequence[str] | None = None,
    *,
    schema_file: pathlib.Path | None = None,
    echo: Echo | None = None,
) -> Result:
    """Apply what is pending. On a branch database, when anything was applied
    and `schema_file` is given, rewrite it from a fresh build."""
    names = _packages(packages)
    result = apply.migrate(url, names, echo=echo)
    if result.applied and schema_file is not None and result.target.kind == "branch":
        try:
            schema.write(url, names, schema_file)
        except SchemaDumpFailed as problem:
            raise SchemaDumpFailed(
                f"{len(result.applied)} applied, but schema.sql was not written: "
                f"{problem.message}",
                problem.fix,
            ) from None
        result = dataclasses.replace(result, schema_file=schema_file)
    return result


def rebuild(
    url: str,
    packages: Sequence[str] | None = None,
    *,
    schema_file: pathlib.Path | None = None,
    echo: Echo | None = None,
) -> Result:
    """Drop a test or branch database, create it empty, and apply every file."""
    names = _packages(packages)
    say = echo or (lambda _: None)
    files.find(names)
    with database.connect(url) as conn:
        say(database.target(conn).describe())
    rebuilt = database.recreate(url)
    say(f"rebuilt  {rebuilt.name}: dropped, created empty, still {rebuilt.kind}")
    named = False

    def without_the_name_again(line: str) -> None:
        nonlocal named
        if named:
            say(line)
        named = True

    return migrate(url, names, schema_file=schema_file, echo=without_the_name_again)


def status(url: str, packages: Sequence[str] | None = None) -> Status:
    """What the ledger says ran, against the files. Read-only."""
    migrations = files.find(_packages(packages))
    with database.connect(url) as conn:
        return ledger.status(conn, migrations)


def pending(url: str, packages: Sequence[str] | None = None) -> list[str]:
    """File names not yet applied, in the order they would run. Read-only.

    Raises LedgerMismatch when an applied file changed or went missing."""
    current = status(url, packages)
    ledger.refuse(current)
    return [p.migration.filename for p in current.pending]


def mark(url: str, kind: Kind) -> tuple[Target, Target]:
    """Set the database's kind; returns the target before and after."""
    return database.mark(url, kind)


def write_schema(
    url: str, path: pathlib.Path, packages: Sequence[str] | None = None
) -> Target:
    """Write the schema a fresh build of every file produces, built in a
    scratch database on `url`'s server; returns that server's database."""
    return schema.write(url, _packages(packages), path)


def _packages(packages: Sequence[str] | None) -> tuple[str, ...]:
    return tuple(packages) if packages is not None else config.project().packages
