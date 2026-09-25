import threading
import time

import psycopg
import pytest
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

    with pytest.raises(pgforward.MigrationFailed) as failed:
        pgforward.migrate(db, [app.name])

    assert "exactly one statement" in failed.value.message
    assert query(db, "SELECT count(*) FROM public.schema_migrations") == [(1,)]


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


def test_a_second_run_waits_for_the_lock_and_names_its_holder(db, app):
    app.add("20260101000000_checks.sql", CHECKS)
    with psycopg.connect(db, autocommit=True, application_name="deploy") as holder:
        holder.execute(queries.TRY_LOCK, (queries.LOCK_KEY,))
        with pytest.raises(pgforward.LockTimeout) as waited:
            pgforward.apply.migrate(db, [app.name], lock_wait=1)

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
