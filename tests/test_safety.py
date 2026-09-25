"""The guards: which database, what kind, and what may be done to it."""

import psycopg
import pytest
from psycopg.conninfo import make_conninfo
from support import CHECKS, query

import pgforward
from pgforward import config, testing


def kind(url: str) -> str | None:
    return query(url, "SELECT current_setting('pgforward.kind', true)")[0][0]


def test_rebuild_refuses_a_database_nobody_marked(db, app):
    app.add("20260101000000_checks.sql", CHECKS)
    pgforward.migrate(db, [app.name])
    query(db, "INSERT INTO checks (name) VALUES ('kept')")

    with pytest.raises(pgforward.Refused) as refused:
        pgforward.rebuild(db, [app.name])

    assert "unmarked, treated as standing" in refused.value.message
    assert query(db, "SELECT name FROM checks") == [("kept",)]


def test_rebuild_of_a_branch_database_starts_from_nothing_and_keeps_its_kind(
    branch_db, app
):
    path = app.add("20260101000000_checks.sql", CHECKS)
    pgforward.migrate(branch_db, [app.name])
    query(branch_db, "INSERT INTO checks (name) VALUES ('gone')")
    path.write_text(CHECKS + "\nALTER TABLE checks ADD COLUMN paused boolean;\n")
    with pytest.raises(pgforward.LedgerMismatch) as refused:
        pgforward.migrate(branch_db, [app.name])
    assert "pgforward rebuild" in refused.value.fix

    result = pgforward.rebuild(branch_db, [app.name])

    assert [r.filename for r in result.applied] == ["20260101000000_checks.sql"]
    assert query(branch_db, "SELECT count(*) FROM checks") == [(0,)]
    assert query(
        branch_db,
        "SELECT column_name FROM information_schema.columns"
        " WHERE table_name = 'checks' ORDER BY ordinal_position",
    ) == [("id",), ("name",), ("paused",)]
    assert kind(branch_db) == "branch"


def test_mark_test_needs_a_test_name(db):
    with pytest.raises(pgforward.Refused):
        pgforward.mark(db, "test")
    assert kind(db) is None


def test_a_standing_or_production_database_is_never_made_disposable(db):
    pgforward.mark(db, "production")

    for disposable in ("branch", "test"):
        with pytest.raises(pgforward.Refused) as refused:
            pgforward.mark(db, disposable)
        assert "RESET pgforward.kind" in refused.value.fix
    assert kind(db) == "production"


def test_prepare_marks_an_unmarked_test_database_and_migrates_it(make_database, app):
    url = make_database("_test")
    app.add("20260101000000_checks.sql", CHECKS)

    assert testing.prepare(url, [app.name]) == ["20260101000000_checks.sql"]
    assert kind(url) == "test"
    assert testing.prepare(url, [app.name]) == []


def test_prepare_refuses_a_database_whose_name_does_not_end_in_test(db, app):
    with pytest.raises(pgforward.Refused):
        testing.prepare(db, [app.name])
    assert kind(db) is None

    name = query(db, "SELECT current_database()")[0][0]
    query(db, f"ALTER DATABASE {name} SET pgforward.kind = 'test'")
    with pytest.raises(pgforward.Refused):
        testing.prepare(db, [app.name])
    assert query(db, "SELECT to_regclass('public.schema_migrations')") == [(None,)]


def test_prepare_refuses_the_development_database(make_database, app, monkeypatch):
    url = make_database("_test")
    monkeypatch.setenv("DATABASE_URL", url.replace("host=127.0.0.1", "host=localhost"))

    with pytest.raises(pgforward.Refused) as refused:
        testing.prepare(url, [app.name])

    assert "DATABASE_URL" in refused.value.message
    assert kind(url) is None


def test_prepare_refuses_a_test_named_database_marked_otherwise(make_database, app):
    url = make_database("_test")
    pgforward.mark(url, "standing")

    with pytest.raises(pgforward.Refused):
        testing.prepare(url, [app.name])


def test_prepare_rebuilds_a_test_database_whose_draft_changed(make_database, app):
    url = make_database("_test")
    path = app.add("20260101000000_checks.sql", CHECKS)
    testing.prepare(url, [app.name])
    query(url, "INSERT INTO checks (name) VALUES ('left over')")
    path.write_text(CHECKS + "\nALTER TABLE checks ADD COLUMN paused boolean;\n")

    assert testing.prepare(url, [app.name]) == ["20260101000000_checks.sql"]
    assert query(url, "SELECT count(*) FROM checks") == [(0,)]


def test_status_and_pending_never_write(db, app):
    app.add("20260101000000_checks.sql", CHECKS)

    current = pgforward.status(db, [app.name])

    assert [p.migration.filename for p in current.pending] == [
        "20260101000000_checks.sql"
    ]
    assert pgforward.pending(db, [app.name]) == ["20260101000000_checks.sql"]
    assert query(db, "SELECT to_regclass('public.schema_migrations')") == [(None,)]


def test_pending_raises_when_an_applied_file_is_missing(db, app):
    path = app.add("20260101000000_checks.sql", CHECKS)
    pgforward.migrate(db, [app.name])
    path.unlink()

    with pytest.raises(pgforward.LedgerMismatch) as refused:
        pgforward.pending(db, [app.name])
    assert "missing" in refused.value.message


@pytest.mark.parametrize(
    ("environment", "explicit", "said"),
    [
        ({}, None, "--env-file .env"),
        ({"DATABASE_URL": ""}, None, "DATABASE_URL is set but empty"),
        ({"MIGRATION_DATABASE_URL": " ", "DATABASE_URL": "x"}, None, "MIGRATION"),
        ({"DATABASE_URL": "x"}, "", "--url is empty"),
    ],
)
def test_an_empty_or_missing_address_is_an_error_never_a_fallback(
    monkeypatch, environment, explicit, said
):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.delenv("MIGRATION_DATABASE_URL", raising=False)
    for name, value in environment.items():
        monkeypatch.setenv(name, value)

    with pytest.raises(pgforward.ConfigError) as refused:
        config.database_url(explicit)

    assert said in f"{refused.value.message} {refused.value.fix}"


def test_the_migration_address_comes_before_the_runtime_one(monkeypatch):
    monkeypatch.setenv("MIGRATION_DATABASE_URL", "owner")
    monkeypatch.setenv("DATABASE_URL", "runtime")
    assert config.database_url() == "owner"


def test_status_runs_in_a_transaction_postgres_keeps_read_only(db, app, monkeypatch):
    def write_instead(conn):
        conn.execute("CREATE TABLE sneaky (id int)")
        return []

    monkeypatch.setattr(pgforward.ledger, "read", write_instead)

    with pytest.raises(psycopg.errors.ReadOnlySqlTransaction):
        pgforward.status(db, [app.name])


def test_prepare_rebuilds_a_test_database_when_a_file_would_run_out_of_order(
    make_database, app
):
    url = make_database("_test")
    app.add("20260101000000_checks.sql", CHECKS)
    app.add("20260103000000_later.sql", "ALTER TABLE checks ADD COLUMN later int;")
    testing.prepare(url, [app.name])
    app.add("20260102000000_teammate.sql", "ALTER TABLE checks ADD COLUMN paused int;")

    assert len(testing.prepare(url, [app.name])) == 3
    assert query(
        url,
        "SELECT column_name FROM information_schema.columns"
        " WHERE table_name = 'checks' ORDER BY ordinal_position",
    ) == [("id",), ("name",), ("paused",), ("later",)]


def test_a_session_setting_cannot_stand_in_for_the_database_mark(db, app):
    pgforward.mark(db, "production")
    app.add("20260101000000_checks.sql", CHECKS)
    pgforward.migrate(db, [app.name])
    overridden = make_conninfo(db, options="-c pgforward.kind=branch")

    with pytest.raises(pgforward.Refused) as refused:
        pgforward.rebuild(overridden, [app.name])

    assert "only the database's own mark counts" in refused.value.fix
    assert kind(db) == "production"
    assert query(db, "SELECT to_regclass('checks')") == [("checks",)]


def test_a_filename_only_ledger_is_baselined_from_the_files(db, app):
    app.add("20260101000000_checks.sql", CHECKS)
    app.add("20260102000000_more.sql", "ALTER TABLE checks ADD COLUMN more int;")
    query(db, CHECKS)
    query(
        db,
        "CREATE TABLE public.schema_migrations (filename TEXT PRIMARY KEY,"
        " applied_at TIMESTAMPTZ NOT NULL DEFAULT now())",
    )
    query(
        db,
        "INSERT INTO public.schema_migrations (filename)"
        " VALUES ('20260101000000_checks.sql')",
    )
    assert pgforward.pending(db, [app.name]) == ["20260102000000_more.sql"]
    said: list[str] = []

    result = pgforward.migrate(db, [app.name], echo=said.append)

    assert [r.filename for r in result.applied] == ["20260102000000_more.sql"]
    assert any(line.startswith("baseline") for line in said)
    assert query(
        db, "SELECT count(*) FROM public.schema_migrations WHERE checksum IS NULL"
    ) == [(0,)]


def test_a_ledger_with_other_column_names_is_refused_before_it_is_altered(db, app):
    app.add("20260101000000_checks.sql", CHECKS)
    query(
        db,
        "CREATE TABLE public.schema_migrations (name TEXT PRIMARY KEY,"
        " sha256 TEXT NOT NULL, applied_at TIMESTAMPTZ NOT NULL DEFAULT now())",
    )

    with pytest.raises(pgforward.ConfigError) as refused:
        pgforward.migrate(db, [app.name])

    assert "RENAME COLUMN name TO filename" in refused.value.fix
    assert "RENAME COLUMN sha256 TO checksum" in refused.value.fix
    assert query(
        db,
        "SELECT count(*) FROM information_schema.columns"
        " WHERE table_name = 'schema_migrations'",
    ) == [(3,)]
    with pytest.raises(pgforward.ConfigError):
        pgforward.status(db, [app.name])


def test_a_null_checksum_is_unrecorded_not_changed(db, app):
    app.add("20260101000000_checks.sql", CHECKS)
    pgforward.migrate(db, [app.name])
    query(
        db, "ALTER TABLE public.schema_migrations ALTER COLUMN checksum DROP NOT NULL"
    )
    query(db, "UPDATE public.schema_migrations SET checksum = NULL")

    assert pgforward.status(db, [app.name]).problems == []
