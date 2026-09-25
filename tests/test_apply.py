import threading
import time

import psycopg
import pytest
from psycopg.conninfo import make_conninfo
from support import CHECKS, query

import pgforward
from pgforward import queries


def test_files_from_several_packages_run_in_name_order_into_one_ledger(
    db, app, make_package
):
    library = make_package()
    library.add("20260101000000_library_table.sql", "CREATE TABLE lib (id int);")
    app.add("20260102000000_checks.sql", CHECKS)
    app.add(
        "20260103000000_uses_library.sql",
        "CREATE TABLE uses (lib_id int, check_id bigint REFERENCES checks);",
    )

    result = pgforward.migrate(db, [app.name, library.name])

    assert [r.filename for r in result.applied] == [
        "20260101000000_library_table.sql",
        "20260102000000_checks.sql",
        "20260103000000_uses_library.sql",
    ]
    rows = query(
        db, "SELECT filename, package FROM public.schema_migrations ORDER BY 1"
    )
    assert [r[1] for r in rows] == [library.name, app.name, app.name]
    assert pgforward.migrate(db, [app.name, library.name]).applied == []


def test_a_failing_file_leaves_nothing_behind_and_names_its_line(db, app):
    app.add("20260101000000_checks.sql", CHECKS)
    app.add(
        "20260102000000_broken.sql",
        "CREATE TABLE half (id int);\n\n"
        "ALTER TABLE checks\n  ADD COLUMN x nosuchtype;\n",
    )

    with pytest.raises(pgforward.MigrationFailed) as failed:
        pgforward.migrate(db, [app.name])

    assert "20260102000000_broken.sql line 4" in failed.value.message
    assert "nothing from this file was applied" in failed.value.fix
    assert query(db, "SELECT to_regclass('half')") == [(None,)]
    assert query(db, "SELECT filename FROM public.schema_migrations") == [
        ("20260101000000_checks.sql",)
    ]


def test_create_index_concurrently_runs_in_a_no_transaction_file(db, app):
    app.add("20260101000000_checks.sql", CHECKS)
    app.add(
        "20260102000000_index.sql",
        "-- pgforward: no-transaction\n"
        "CREATE INDEX CONCURRENTLY checks_name_lower ON checks (lower(name));\n",
    )

    result = pgforward.migrate(db, [app.name])

    assert [r.transaction for r in result.applied] == [True, False]
    assert query(
        db,
        "SELECT indisvalid FROM pg_index"
        " WHERE indexrelid = 'checks_name_lower'::regclass",
    ) == [(True,)]


def test_a_no_transaction_file_with_two_statements_is_refused(db, app):
    app.add("20260101000000_checks.sql", CHECKS)
    app.add(
        "20260102000000_two.sql",
        "-- pgforward: no-transaction\n"
        "SET lock_timeout = '1s';\n"
        "CREATE INDEX CONCURRENTLY checks_name_lower ON checks (lower(name));\n",
    )

    with pytest.raises(pgforward.ConfigError) as refused:
        pgforward.migrate(db, [app.name])

    assert "exactly one statement" in refused.value.message
    assert "lines 2, 3" in refused.value.message
    assert query(db, "SELECT to_regclass('public.schema_migrations')") == [(None,)]


def test_a_failed_concurrent_index_names_the_invalid_index_to_drop(db, app):
    app.add(
        "20260101000000_dupes.sql",
        "CREATE TABLE dupes (v int); INSERT INTO dupes VALUES (1), (1);",
    )
    app.add(
        "20260102000000_unique.sql",
        "-- pgforward: no-transaction\n"
        "CREATE UNIQUE INDEX CONCURRENTLY dupes_v ON dupes (v);\n",
    )

    with pytest.raises(pgforward.MigrationFailed) as failed:
        pgforward.migrate(db, [app.name])

    assert "DROP INDEX CONCURRENTLY dupes_v" in failed.value.fix


def test_timeouts_are_set_while_migrating_and_a_file_can_change_them(db, app):
    app.add(
        "20260101000000_defaults.sql",
        "CREATE TABLE seen AS SELECT 'defaults' AS file,"
        " current_setting('lock_timeout') AS lock,"
        " current_setting('statement_timeout') AS statement;",
    )
    app.add(
        "20260102000000_own.sql",
        "-- pgforward: statement-timeout=20min\n"
        "-- pgforward: lock-timeout=0\n"
        "INSERT INTO seen SELECT 'own', current_setting('lock_timeout'),"
        " current_setting('statement_timeout');",
    )

    pgforward.migrate(db, [app.name])

    assert query(db, "SELECT file, lock, statement FROM seen ORDER BY 1") == [
        ("defaults", "5s", "1min"),
        ("own", "0", "20min"),
    ]


def test_a_statement_timeout_names_the_directive_that_raises_it(db, app):
    app.add(
        "20260101000000_slow.sql",
        "-- pgforward: statement-timeout=100ms\nSELECT pg_sleep(2);\n",
    )

    with pytest.raises(pgforward.MigrationFailed) as failed:
        pgforward.migrate(db, [app.name])

    assert "statement timeout (100ms)" in failed.value.message
    assert "statement-timeout=" in failed.value.fix


def test_a_lock_timeout_is_retried_and_succeeds_once_the_lock_is_free(db, app):
    query(db, CHECKS)
    app.add(
        "20260101000000_alter.sql",
        "-- pgforward: lock-timeout=100ms\n"
        "ALTER TABLE checks ADD COLUMN paused boolean;",
    )
    said: list[str] = []
    holder = psycopg.connect(db)
    holder.execute("LOCK TABLE checks IN ACCESS EXCLUSIVE MODE")
    threading.Timer(0.5, holder.rollback).start()
    try:
        result = pgforward.migrate(db, [app.name], echo=said.append)
    finally:
        holder.close()

    assert [r.filename for r in result.applied] == ["20260101000000_alter.sql"]
    assert any(line.startswith("retry") for line in said)


def test_a_lock_timeout_says_the_file_is_not_at_fault(db, app):
    query(db, CHECKS)
    app.add(
        "20260101000000_alter.sql",
        "-- pgforward: lock-timeout=100ms\n"
        "ALTER TABLE checks ADD COLUMN paused boolean;",
    )
    holder = psycopg.connect(db)
    holder.execute("LOCK TABLE checks IN ACCESS EXCLUSIVE MODE")
    try:
        with pytest.raises(pgforward.MigrationFailed) as failed:
            pgforward.migrate(db, [app.name])
    finally:
        holder.close()

    assert "nothing in it is wrong" in failed.value.fix


def test_a_second_run_waits_for_the_lock_and_names_its_holder(db, app):
    app.add("20260101000000_checks.sql", CHECKS)
    # A lock_timeout on the waiting run, so a lock that blocks instead of
    # polling fails this test rather than hanging it.
    waiting = make_conninfo(db, options="-c lock_timeout=3s")
    with psycopg.connect(db, autocommit=True, application_name="deploy") as holder:
        holder.execute(queries.TRY_LOCK, (queries.LOCK_KEY,))
        with pytest.raises(pgforward.LockTimeout) as waited:
            pgforward.apply.migrate(waiting, [app.name], lock_wait=1)

    assert "deploy" in waited.value.message
    assert query(db, "SELECT to_regclass('checks')") == [(None,)]


def test_a_waiting_run_does_not_block_a_concurrent_index_build(db, app):
    """The waiting run polls in autocommit, so it holds no snapshot that
    CREATE INDEX CONCURRENTLY in the lock holder's session must wait out."""
    query(db, CHECKS)
    app.add("20260101000000_noop.sql", "SELECT 1;")
    with psycopg.connect(db, autocommit=True) as holder:
        holder.execute(queries.TRY_LOCK, (queries.LOCK_KEY,))
        waiting = threading.Thread(
            target=pgforward.apply.migrate, args=(db, [app.name]), daemon=True
        )
        waiting.start()
        time.sleep(0.3)
        holder.execute("SET statement_timeout = '5s'")
        holder.execute("CREATE INDEX CONCURRENTLY checks_lower ON checks (lower(name))")
        holder.execute(queries.UNLOCK, (queries.LOCK_KEY,))
        waiting.join(timeout=10)

    assert not waiting.is_alive()
    assert query(db, "SELECT count(*) FROM public.schema_migrations") == [(1,)]


def test_an_older_file_merged_late_is_applied_and_reported(db, app):
    app.add("20260101000000_checks.sql", CHECKS)
    app.add("20260103000000_later.sql", "ALTER TABLE checks ADD COLUMN later int;")
    pgforward.migrate(db, [app.name])
    app.add("20260102000000_teammate.sql", "ALTER TABLE checks ADD COLUMN paused int;")
    said: list[str] = []

    result = pgforward.migrate(db, [app.name], echo=said.append)

    assert [(r.filename, r.out_of_order) for r in result.applied] == [
        ("20260102000000_teammate.sql", True)
    ]
    assert any("out of order" in line and "rebuild" in line for line in said)
    assert query(
        db,
        "SELECT out_of_order FROM public.schema_migrations"
        " WHERE filename = '20260102000000_teammate.sql'",
    ) == [(True,)]


def test_an_applied_file_that_changed_is_refused(db, app):
    path = app.add("20260101000000_checks.sql", CHECKS)
    pgforward.migrate(db, [app.name])
    path.write_text(CHECKS + "\nCREATE INDEX ON checks (name);\n")

    with pytest.raises(pgforward.LedgerMismatch) as refused:
        pgforward.migrate(db, [app.name])

    assert "20260101000000_checks.sql" in refused.value.message
    assert "new file" in refused.value.fix


def test_an_earlier_three_column_ledger_is_kept_and_extended(db, app):
    app.add("20260101000000_checks.sql", CHECKS)
    app.add("20260102000000_more.sql", "ALTER TABLE checks ADD COLUMN more int;")
    first = pgforward.files.find([app.name])[0]
    query(
        db,
        "CREATE TABLE public.schema_migrations (filename TEXT PRIMARY KEY,"
        " checksum TEXT NOT NULL, applied_at TIMESTAMPTZ NOT NULL DEFAULT NOW())",
    )
    query(db, CHECKS)
    query(
        db,
        "INSERT INTO public.schema_migrations (filename, checksum) VALUES (%s, %s)",
        first.filename,
        first.checksum,
    )

    result = pgforward.migrate(db, [app.name])

    assert [r.filename for r in result.applied] == ["20260102000000_more.sql"]
    assert query(
        db, "SELECT filename, package FROM public.schema_migrations ORDER BY 1"
    ) == [("20260101000000_checks.sql", None), ("20260102000000_more.sql", app.name)]


def test_a_file_and_its_ledger_row_commit_together(db, app, monkeypatch):
    app.add("20260101000000_checks.sql", CHECKS)

    def ledger_write_fails(*args):
        raise psycopg.errors.DiskFull("no space for the ledger row")

    monkeypatch.setattr(pgforward.apply, "_record", ledger_write_fails)
    with pytest.raises(pgforward.MigrationFailed):
        pgforward.migrate(db, [app.name])

    assert query(db, "SELECT to_regclass('checks')") == [(None,)]


def _snapshot_holder(url: str, seconds: float) -> psycopg.Connection:
    """A session holding a snapshot, which CREATE INDEX CONCURRENTLY waits
    out, released after `seconds`."""
    holder = psycopg.connect(url)
    holder.execute("BEGIN ISOLATION LEVEL REPEATABLE READ")
    holder.execute("SELECT 1")
    threading.Timer(seconds, holder.rollback).start()
    return holder


def test_a_timed_out_concurrent_build_names_its_invalid_index_and_blocks_rerun(db, app):
    query(db, "CREATE TABLE t (y int)")
    path = app.add(
        "20260101000000_index.sql",
        "-- pgforward: no-transaction\n"
        "-- pgforward: statement-timeout=300ms\n"
        "CREATE INDEX CONCURRENTLY IF NOT EXISTS t_y ON t (y);\n",
    )
    holder = _snapshot_holder(db, 2)
    try:
        with pytest.raises(pgforward.MigrationFailed) as failed:
            pgforward.migrate(db, [app.name])
    finally:
        time.sleep(2.2)
        holder.close()
    assert "DROP INDEX CONCURRENTLY t_y" in failed.value.fix

    path.write_text(path.read_text().replace("300ms", "10min"))
    with pytest.raises(pgforward.MigrationFailed) as refused:
        pgforward.migrate(db, [app.name])

    assert "before it could run" in refused.value.message
    assert pgforward.pending(db, [app.name]) == ["20260101000000_index.sql"]


def test_a_concurrent_build_waits_out_long_queries_by_default(db, app, monkeypatch):
    monkeypatch.setitem(pgforward.apply.DEFAULTS, "lock-timeout", "100ms")
    query(db, "CREATE TABLE t (y int)")
    app.add(
        "20260101000000_index.sql",
        "-- pgforward: no-transaction\nCREATE INDEX CONCURRENTLY t_y ON t (y);\n",
    )
    holder = _snapshot_holder(db, 0.7)
    try:
        result = pgforward.migrate(db, [app.name])
    finally:
        holder.close()

    assert [r.filename for r in result.applied] == ["20260101000000_index.sql"]


def test_a_statement_already_run_but_not_recorded_says_how_to_record_it(db, app):
    query(db, "CREATE TABLE t (y int)")
    query(db, "CREATE INDEX t_y ON t (y)")
    app.add(
        "20260101000000_index.sql",
        "-- pgforward: no-transaction\nCREATE INDEX CONCURRENTLY t_y ON t (y);\n",
    )

    with pytest.raises(pgforward.MigrationFailed) as failed:
        pgforward.migrate(db, [app.name])

    assert "INSERT INTO public.schema_migrations" in failed.value.fix
    assert "20260101000000_index.sql" in failed.value.fix


def test_a_set_in_one_file_does_not_carry_into_the_next(db, app):
    app.add(
        "20260101000000_app.sql",
        "CREATE SCHEMA app; SET search_path = app; CREATE TABLE one (x int);",
    )
    app.add("20260102000000_two.sql", "CREATE TABLE two (x int);")

    pgforward.migrate(db, [app.name])

    assert query(db, "SELECT to_regclass('public.two')") == [("two",)]


def test_a_file_that_ends_the_transaction_itself_is_caught_while_running(
    db, app, monkeypatch
):
    """The scanner refuses COMMIT in a file before anything runs; this is the
    check behind it, for a form the scanner misses."""
    monkeypatch.setattr(
        pgforward.statements, "transaction_control", lambda statement: False
    )
    app.add("20260101000000_commits.sql", "CREATE TABLE a (id int); COMMIT;")

    with pytest.raises(pgforward.MigrationFailed) as failed:
        pgforward.migrate(db, [app.name])

    assert "ended pgforward's transaction" in failed.value.message


def test_the_lock_holder_named_is_on_this_database(db, make_database, app):
    other = make_database()
    app.add("20260101000000_checks.sql", CHECKS)
    with (
        psycopg.connect(other, autocommit=True, application_name="elsewhere") as a,
        psycopg.connect(db, autocommit=True, application_name="real-holder") as b,
    ):
        a.execute(queries.TRY_LOCK, (queries.LOCK_KEY,))
        b.execute(queries.TRY_LOCK, (queries.LOCK_KEY,))
        with pytest.raises(pgforward.LockTimeout) as waited:
            pgforward.apply.migrate(db, [app.name], lock_wait=0.5)

    assert "real-holder" in waited.value.message


def test_a_lost_connection_names_the_file_that_was_running(db, app):
    app.add("20260101000000_slow.sql", "SELECT pg_sleep(5);")

    def kill() -> None:
        query(
            db,
            "SELECT pg_terminate_backend(pid) FROM pg_stat_activity"
            " WHERE application_name = 'pgforward' AND datname = current_database()"
            " AND pid <> pg_backend_pid()",
        )

    threading.Timer(0.5, kill).start()
    with pytest.raises(pgforward.MigrationFailed) as failed:
        pgforward.migrate(db, [app.name])

    assert "20260101000000_slow.sql" in failed.value.message
    assert "connection was lost" in failed.value.message


def test_a_cancel_is_not_called_a_statement_timeout(db, app):
    app.add("20260101000000_slow.sql", "SELECT pg_sleep(5);")

    def cancel() -> None:
        query(
            db,
            "SELECT pg_cancel_backend(pid) FROM pg_stat_activity"
            " WHERE application_name = 'pgforward' AND datname = current_database()"
            " AND pid <> pg_backend_pid()",
        )

    threading.Timer(0.5, cancel).start()
    with pytest.raises(pgforward.MigrationFailed) as failed:
        pgforward.migrate(db, [app.name])

    assert "statement timeout" not in failed.value.message
    assert "canceling statement" in failed.value.message
