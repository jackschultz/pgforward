"""Applying migrations: one lock per run, one transaction per file.

The lock is a session-level advisory lock, taken by polling
`pg_try_advisory_lock` on an autocommit connection. A run that blocked in
`pg_advisory_lock` instead would hold a snapshot while it waited, and a
`CREATE INDEX CONCURRENTLY` in the run holding the lock waits for every older
snapshot: the two would wait on each other forever.
"""

import dataclasses
import importlib.metadata
import pathlib
import time
from collections.abc import Callable, Sequence

import psycopg

from pgforward import database, files, ledger, queries
from pgforward.errors import LockTimeout, MigrationFailed

LOCK_WAIT_SECONDS = 60
POLL_SECONDS = 0.5
DEFAULTS = {"lock-timeout": "5s", "statement-timeout": "1min"}
LOCK_ATTEMPTS = 3

Echo = Callable[[str], None]


@dataclasses.dataclass(frozen=True)
class Ran:
    filename: str
    package: str
    duration_ms: int
    out_of_order: bool
    transaction: bool


@dataclasses.dataclass(frozen=True)
class Result:
    target: database.Target
    applied: list[Ran]
    schema_file: pathlib.Path | None = None


def migrate(
    url: str,
    packages: Sequence[str],
    *,
    echo: Echo | None = None,
    lock_wait: float = LOCK_WAIT_SECONDS,
) -> Result:
    say = echo or (lambda _: None)
    migrations = files.find(packages)
    applied: list[Ran] = []
    with database.connect(url) as conn:
        target = database.target(conn)
        say(target.describe())
        _lock(conn, lock_wait, say)
        try:
            with conn.transaction():
                conn.execute(queries.ENSURE_LEDGER)
            status = ledger.compare(target, migrations, ledger.read(conn))
            ledger.refuse(status)
            latest = max((a.filename for a in status.applied), default="")
            for pending in status.pending:
                ran = _run(conn, pending, latest, say)
                applied.append(ran)
                latest = max(latest, ran.filename)
        finally:
            conn.execute(queries.UNLOCK, (queries.LOCK_KEY,))
    return Result(target, applied)


def _lock(conn: psycopg.Connection, wait: float, say: Echo) -> None:
    deadline = time.monotonic() + wait
    announced = False
    while not database.one(conn, queries.TRY_LOCK, (queries.LOCK_KEY,))[0]:
        if time.monotonic() >= deadline:
            raise LockTimeout(
                f"another run has held the migration lock for over {wait:.0f} s: "
                f"{_holder(conn)}",
                "wait for it to finish, then run the command again; "
                "`pgforward status` shows what it has applied so far",
            )
        if not announced:
            say(f"waiting  for the migration lock, held by {_holder(conn)}")
            announced = True
        time.sleep(POLL_SECONDS)


def _holder(conn: psycopg.Connection) -> str:
    row = conn.execute(
        queries.LOCK_HOLDER, (queries.LOCK_KEY, queries.LOCK_KEY)
    ).fetchone()
    if row is None:
        return "a session that has just released it"
    pid, application, seconds, state = row
    return f"pid {pid} ({application}, {state}, {seconds} s)"


def _run(
    conn: psycopg.Connection, pending: ledger.Pending, latest: str, say: Echo
) -> Ran:
    migration = pending.migration
    settings = DEFAULTS | migration.settings
    for name, value in settings.items():
        conn.execute(queries.SET, (name.replace("-", "_"), value))
    out_of_order = migration.filename < latest
    for attempt in range(1, LOCK_ATTEMPTS + 1):
        started = time.perf_counter()
        try:
            if migration.transaction:
                with conn.transaction():
                    conn.execute(migration.text.encode())
                    duration = _ms(started)
                    _record(conn, migration, duration, out_of_order)
            else:
                conn.execute(migration.text.encode(), prepare=True)
                duration = _ms(started)
                _record(conn, migration, duration, out_of_order)
            break
        except psycopg.errors.LockNotAvailable as problem:
            if not migration.transaction or attempt == LOCK_ATTEMPTS:
                raise _failed(conn, migration, problem, settings) from None
            say(
                f"retry    {migration.filename}: lock_timeout "
                f"({settings['lock-timeout']}), attempt {attempt + 1} of "
                f"{LOCK_ATTEMPTS}"
            )
            time.sleep(attempt)
        except psycopg.Error as problem:
            raise _failed(conn, migration, problem, settings) from None
    ran = Ran(
        migration.filename,
        migration.package,
        duration,
        out_of_order,
        migration.transaction,
    )
    say(_line(ran))
    if out_of_order:
        say(
            f"note     {ran.filename} ran out of order: it is older than "
            f"{latest}, which was already applied. A fresh build orders columns "
            "differently; on a branch database `pgforward rebuild` makes this "
            "database match"
        )
    return ran


def _record(
    conn: psycopg.Connection,
    migration: files.Migration,
    duration: int,
    out_of_order: bool,
) -> None:
    conn.execute(
        queries.RECORD,
        (
            migration.filename,
            migration.checksum,
            migration.package,
            duration,
            out_of_order,
            importlib.metadata.version("pgforward"),
        ),
    )


def _failed(
    conn: psycopg.Connection,
    migration: files.Migration,
    problem: psycopg.Error,
    settings: dict[str, str],
) -> MigrationFailed:
    where = f"{migration.package}/migrations/{migration.filename}"
    diag = problem.diag
    if diag.statement_position:
        line = migration.text.count("\n", 0, int(diag.statement_position) - 1) + 1
        where += f" line {line}"
    message = diag.message_primary or str(problem).strip()
    extra = [part for part in (diag.message_detail, diag.message_hint) if part]
    if extra:
        message += f" ({'; '.join(extra)})"
    if isinstance(problem, psycopg.errors.QueryCanceled):
        return MigrationFailed(
            f"{where}: hit the statement timeout ({settings['statement-timeout']})",
            "if it needs longer, put `-- pgforward: statement-timeout=10min` at "
            "the top of the file",
        )
    if isinstance(problem, psycopg.errors.LockNotAvailable):
        message = (
            f"could not get a lock within lock_timeout ({settings['lock-timeout']}): "
            "a running query or open transaction holds the table"
        )
    if migration.transaction:
        return MigrationFailed(
            f"{where}: {message}",
            "nothing from this file was applied; fix it and run "
            "`pgforward migrate` again",
        )
    if "multiple commands" in message:
        return MigrationFailed(
            f"{where}: a `-- pgforward: no-transaction` file must hold exactly one "
            "statement",
            "split it: one file per statement that cannot run in a transaction",
        )
    invalid = [row[0] for row in conn.execute(queries.INVALID_INDEXES)]
    fix = "fix it and run `pgforward migrate` again"
    if invalid:
        drops = " ".join(f"DROP INDEX CONCURRENTLY {name};" for name in invalid)
        fix = f"invalid indexes are left behind; drop them first: {drops} Then {fix}"
    return MigrationFailed(f"{where}: {message}", fix)


def _line(ran: Ran) -> str:
    extra = "" if ran.transaction else "  no transaction"
    return f"applied  {ran.filename}  {ran.package}  {ran.duration_ms} ms{extra}"


def _ms(started: float) -> int:
    return round((time.perf_counter() - started) * 1000)
