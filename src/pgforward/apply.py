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
from psycopg.pq import TransactionStatus

from pgforward import database, files, ledger, queries
from pgforward.errors import LockTimeout, MigrationFailed

LOCK_WAIT_SECONDS = 60
POLL_SECONDS = 0.5
DEFAULTS = {"lock-timeout": "5s", "statement-timeout": "1min"}
# A no-transaction file is for statements like CREATE INDEX CONCURRENTLY that
# block no reads or writes while they run, but wait for every older
# transaction in the database, which lock_timeout also bounds. A 5 s limit
# there fails the build whenever any query runs long, and leaves an invalid
# index behind; the file sets its own limits when it wants them.
NO_TRANSACTION_DEFAULTS = {"lock-timeout": "0", "statement-timeout": "0"}
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
    reruns: list[Ran] = dataclasses.field(default_factory=list)


@dataclasses.dataclass(frozen=True)
class Step:
    migration: files.Migration
    settings: dict[str, str]


@dataclasses.dataclass(frozen=True)
class Plan:
    target: database.Target
    steps: list[Step]


def migrate(
    url: str,
    packages: Sequence[str],
    *,
    echo: Echo | None = None,
    lock_wait: float = LOCK_WAIT_SECONDS,
) -> Result:
    say = echo or (lambda _: None)
    migrations = files.find(packages)
    reruns = files.reruns(packages)
    applied: list[Ran] = []
    reran: list[Ran] = []
    with database.connect(url) as conn:
        target = database.target(conn)
        say(target.describe())
        _lock(conn, lock_wait, say)
        try:
            recorded = ledger.read(conn)
            with conn.transaction():
                conn.execute(queries.ENSURE_LEDGER)
            _baseline(conn, migrations, recorded, say)
            status = ledger.compare(target, migrations, ledger.read(conn))
            ledger.refuse(status)
            ledger.refuse_ahead(status)
            latest = max((a.filename for a in status.applied), default="")
            for pending in status.pending:
                ran = _run(conn, pending, latest, say)
                applied.append(ran)
                latest = max(latest, ran.filename)
            with conn.transaction():
                conn.execute(queries.ENSURE_RERUNS)
            for rerun in ledger.due(reruns, ledger.read_reruns(conn)):
                reran.append(_rerun(conn, rerun, say))
        finally:
            if not conn.closed:
                conn.execute(queries.UNLOCK, (queries.LOCK_KEY,))
    return Result(target, applied, reruns=reran)


def plan(url: str, packages: Sequence[str]) -> Plan:
    """What `migrate` would run, in order, with the settings each file gets.
    Read-only; refuses what `migrate` would refuse."""
    migrations = files.find(packages)
    reruns = files.reruns(packages)
    with database.connect(url) as conn:
        status = ledger.status(conn, migrations, reruns)
    ledger.refuse(status)
    ledger.refuse_ahead(status)
    return Plan(
        status.target,
        [Step(p.migration, _settings(p.migration)) for p in status.pending]
        + [Step(r, _settings(r)) for r in status.reruns_due],
    )


def _settings(migration: files.Migration) -> dict[str, str]:
    defaults = DEFAULTS if migration.transaction else NO_TRANSACTION_DEFAULTS
    return defaults | migration.settings


def _rerun(conn: psycopg.Connection, rerun: files.Migration, say: Echo) -> Ran:
    settings = _settings(rerun)
    conn.execute("RESET ALL")
    conn.execute("RESET ROLE")
    for name, value in settings.items():
        conn.execute(queries.SET, (name.replace("-", "_"), value))
    started = time.perf_counter()
    try:
        with conn.transaction():
            conn.execute(rerun.text.encode())
            conn.execute(
                queries.RECORD_RERUN, (rerun.package, rerun.filename, rerun.checksum)
            )
    except psycopg.Error as problem:
        raise _failed(conn, rerun, problem, settings) from None
    ran = Ran(f"rerun/{rerun.filename}", rerun.package, _ms(started), False, True)
    say(f"rerun    {rerun.where}  {ran.duration_ms} ms")
    return ran


def _baseline(
    conn: psycopg.Connection,
    migrations: Sequence[files.Migration],
    recorded: Sequence[ledger.Applied],
    say: Echo,
) -> None:
    on_disk = {m.filename: m for m in migrations}
    unverified = [
        a.filename for a in recorded if a.checksum is None and a.filename in on_disk
    ]
    with conn.transaction():
        for filename in unverified:
            conn.execute(queries.BASELINE, (on_disk[filename].checksum, filename))
    if unverified:
        say(
            f"baseline {len(unverified)} applied files had no checksum; recorded "
            "from the files on disk, which are trusted to be what ran"
        )


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
    settings = _settings(migration)
    out_of_order = migration.filename < latest
    if not migration.transaction:
        _refuse_invalid_indexes(conn, migration)
    for attempt in range(1, LOCK_ATTEMPTS + 1):
        # A SET in an earlier file would otherwise carry into this one, and
        # the same files would build differently in one run than in several.
        conn.execute("RESET ALL")
        conn.execute("RESET ROLE")
        for name, value in settings.items():
            conn.execute(queries.SET, (name.replace("-", "_"), value))
        started = time.perf_counter()
        try:
            if migration.transaction:
                with conn.transaction():
                    conn.execute(migration.text.encode())
                    if conn.info.transaction_status != TransactionStatus.INTRANS:
                        raise MigrationFailed(
                            f"{migration.where} ended pgforward's transaction "
                            "itself; part of it may be applied",
                            "remove the COMMIT, ROLLBACK or BEGIN from it, and "
                            "check what it left in the database",
                        )
                    duration = _ms(started)
                    _record(conn, migration, duration, out_of_order)
            else:
                conn.execute(migration.text.encode(), prepare=True)
                duration = _ms(started)
                _refuse_invalid_indexes(conn, migration, after=True)
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


def _refuse_invalid_indexes(
    conn: psycopg.Connection, migration: files.Migration, after: bool = False
) -> None:
    """An invalid index is one a concurrent build left half made. Before a
    no-transaction file runs, one already there could be skipped by its
    IF NOT EXISTS and recorded as done; after, one it left means it failed."""
    invalid = [row[0] for row in conn.execute(queries.INVALID_INDEXES)]
    if not invalid:
        return
    when = "after it ran" if after else "before it could run"
    raise MigrationFailed(
        f"{migration.where}: invalid index {', '.join(invalid)} in the "
        f"database {when}; a concurrent index build left it half made",
        _drop_invalid(invalid),
    )


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
    where = migration.where
    if conn.closed:
        after = (
            "it rolled back"
            if migration.transaction
            else "run `pgforward status`, and look for an invalid index it may "
            "have left"
        )
        return MigrationFailed(
            f"{where}: the connection was lost while it ran ({str(problem).strip()})",
            f"nothing of it was recorded; {after}. Then run `pgforward migrate`",
        )
    diag = problem.diag
    if diag.statement_position:
        line = migration.text.count("\n", 0, int(diag.statement_position) - 1) + 1
        where += f" line {line}"
    message = diag.message_primary or str(problem).strip()
    extra = [part for part in (diag.message_detail, diag.message_hint) if part]
    if extra:
        message += f" ({'; '.join(extra)})"
    invalid = (
        []
        if migration.transaction
        else [row[0] for row in conn.execute(queries.INVALID_INDEXES)]
    )
    if isinstance(problem, psycopg.errors.QueryCanceled) and "timeout" in message:
        fix = (
            "if it needs longer, put `-- pgforward: statement-timeout=10min` at "
            "the top of the file"
        )
        if invalid:
            fix = f"{_drop_invalid(invalid)}; and {fix}"
        return MigrationFailed(
            f"{where}: hit the statement timeout ({settings['statement-timeout']})",
            fix,
        )
    if isinstance(problem, psycopg.errors.LockNotAvailable):
        message = (
            f"could not get a lock within lock_timeout ({settings['lock-timeout']}): "
            "a running query or open transaction holds the table"
        )
    if migration.transaction and isinstance(problem, psycopg.errors.LockNotAvailable):
        return MigrationFailed(
            f"{where}: {message}",
            "nothing from this file was applied, and nothing in it is wrong: wait "
            "for the query or transaction holding the table to finish "
            "(pg_stat_activity shows it), then run `pgforward migrate` again",
        )
    if migration.transaction:
        return MigrationFailed(
            f"{where}: {message}",
            "nothing from this file was applied; fix it and run "
            "`pgforward migrate` again",
        )
    if invalid:
        return MigrationFailed(f"{where}: {message}", _drop_invalid(invalid))
    if isinstance(
        problem, psycopg.errors.DuplicateTable | psycopg.errors.DuplicateObject
    ):
        return MigrationFailed(
            f"{where}: {message}",
            "if this file made it, the statement already ran and was never "
            "recorded (a run stopped between the two). Record it: "
            "INSERT INTO public.schema_migrations (filename, checksum, package) "
            f"VALUES ('{migration.filename}', '{migration.checksum}', "
            f"'{migration.package}'); or write it with IF NOT EXISTS",
        )
    return MigrationFailed(
        f"{where}: {message}", "fix it and run `pgforward migrate` again"
    )


def _drop_invalid(names: list[str]) -> str:
    drops = " ".join(f"DROP INDEX CONCURRENTLY {name};" for name in names)
    return f"drop it, then run `pgforward migrate` again: {drops}"


def _line(ran: Ran) -> str:
    extra = "" if ran.transaction else "  no transaction"
    return f"applied  {ran.filename}  {ran.package}  {ran.duration_ms} ms{extra}"


def _ms(started: float) -> int:
    return round((time.perf_counter() - started) * 1000)
