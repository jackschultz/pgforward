"""Postgres migrations in plain SQL files, forward only.

`pgforward guide` prints how to use it. From Python:

    pgforward.migrate(url)            apply what is pending
    pgforward.pending(url)            file names not yet applied, for /health
    pgforward.status(url)             applied, pending, and any disagreement
    pgforward.testing.prepare(url)    bring a test database up to date, guarded
    pgforward.plan(url)               what migrate would run, read-only
    pgforward.grants(url, role)       what the runtime role may do, read-only
    pgforward.check_schema(url)       differences from a fresh build

`packages` defaults to [tool.pgforward] packages in the nearest pyproject.toml.
"""

import dataclasses
import pathlib
from collections.abc import Sequence

from pgforward import (
    apply,
    config,
    database,
    files,
    grants_check,
    ledger,
    schema,
    testing,
)
from pgforward.apply import Echo, Plan, Ran, Result, Step
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
    "Plan",
    "Ran",
    "Refused",
    "Result",
    "SchemaDumpFailed",
    "Status",
    "Step",
    "Target",
    "check_schema",
    "grants",
    "mark",
    "migrate",
    "pending",
    "plan",
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
            schema.write(url, names, schema_file, echo)
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
        current = database.target(conn)
    say(current.describe())
    with database.serialized(url, current.name):
        rebuilt = database.recreate(url)
    say(
        f"rebuilt  {rebuilt.name}: dropped (ending its other sessions), created "
        f"empty, still {rebuilt.kind}"
    )
    named = False

    def without_the_name_again(line: str) -> None:
        nonlocal named
        if named:
            say(line)
        named = True

    return migrate(url, names, schema_file=schema_file, echo=without_the_name_again)


def status(url: str, packages: Sequence[str] | None = None) -> Status:
    """What the ledger says ran, against the files. Read-only."""
    names = _packages(packages)
    migrations, reruns = files.find(names), files.reruns(names)
    with database.connect(url) as conn:
        return ledger.status(conn, migrations, reruns)


def pending(url: str, packages: Sequence[str] | None = None) -> list[str]:
    """File names not yet applied, in the order they would run. Read-only.

    Raises LedgerMismatch when an applied file changed or went missing. A
    database that has run a newer release's migrations is not an error here:
    `status(url).ahead` names them."""
    current = status(url, packages)
    ledger.refuse(current)
    return current.pending_names


def plan(url: str, packages: Sequence[str] | None = None) -> Plan:
    """What `migrate` would run, in order, with each file's settings; nothing
    is written."""
    return apply.plan(url, _packages(packages))


def grants(url: str, role: str) -> grants_check.Grants:
    """What `role` may do on each of the application's tables. Read-only."""
    return grants_check.check(url, role)


def check_schema(
    url: str, packages: Sequence[str] | None = None, echo: Echo | None = None
) -> tuple[Target, list[str]]:
    """How this database's schema differs from a fresh build of every file, as
    unified diff lines; empty when they match."""
    return schema.check(url, _packages(packages), echo)


def mark(url: str, kind: Kind) -> tuple[Target, Target]:
    """Set the database's kind; returns the target before and after."""
    return database.mark(url, kind)


def write_schema(
    url: str,
    path: pathlib.Path,
    packages: Sequence[str] | None = None,
    echo: Echo | None = None,
) -> Target:
    """Write the schema a fresh build of every file produces, built in a
    scratch database on `url`'s server; returns that server's database."""
    return schema.write(url, _packages(packages), path, echo)


def _packages(packages: Sequence[str] | None) -> tuple[str, ...]:
    return tuple(packages) if packages is not None else config.project().packages
