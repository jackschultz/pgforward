import psycopg
import pytest
from support import CHECKS, query

import pgforward
from pgforward import files

VIEW = "CREATE OR REPLACE VIEW check_names AS SELECT name FROM checks;"


def test_a_rerun_file_runs_after_the_migrations_and_again_only_when_it_changes(db, app):
    app.add("20260101000000_checks.sql", CHECKS)
    view = app.rerun("views.sql", VIEW)

    first = pgforward.migrate(db, [app.name])
    again = pgforward.migrate(db, [app.name])
    view.write_text(VIEW.replace("SELECT name", "SELECT name, id"))
    changed = pgforward.migrate(db, [app.name])

    assert [r.filename for r in first.reruns] == ["rerun/views.sql"]
    assert again.reruns == []
    assert [r.filename for r in changed.reruns] == ["rerun/views.sql"]
    assert query(
        db,
        "SELECT column_name FROM information_schema.columns"
        " WHERE table_name = 'check_names' ORDER BY ordinal_position",
    ) == [("name",), ("id",)]


def test_libraries_rerun_files_run_before_the_apps(db, app, make_package):
    library = make_package()
    library.add("20260101000000_lib.sql", "CREATE TABLE lib (x int);")
    library.rerun("views.sql", "CREATE OR REPLACE VIEW lib_view AS SELECT x FROM lib;")
    app.rerun("views.sql", "CREATE OR REPLACE VIEW app_view AS SELECT x FROM lib_view;")

    result = pgforward.migrate(db, [app.name, library.name])

    assert [r.package for r in result.reruns] == [library.name, app.name]


def test_a_due_rerun_file_is_pending(db, app):
    app.add("20260101000000_checks.sql", CHECKS)
    pgforward.migrate(db, [app.name])
    app.rerun("views.sql", VIEW)

    assert pgforward.pending(db, [app.name]) == [f"{app.name}/rerun/views.sql"]
    assert not pgforward.status(db, [app.name]).current


def test_a_failing_rerun_file_leaves_nothing_and_is_named(db, app):
    app.add("20260101000000_checks.sql", CHECKS)
    app.rerun("views.sql", VIEW + "\nCREATE VIEW broken AS SELECT nope FROM checks;")

    with pytest.raises(pgforward.MigrationFailed) as failed:
        pgforward.migrate(db, [app.name])

    assert f"{app.name}/rerun/views.sql line 2" in failed.value.message
    assert query(db, "SELECT to_regclass('check_names')") == [(None,)]
    assert pgforward.pending(db, [app.name]) == [f"{app.name}/rerun/views.sql"]


@pytest.mark.parametrize(
    ("name", "text"),
    [
        ("Views.sql", VIEW),
        ("views.sql", "-- pgforward: no-transaction\n" + VIEW),
        ("views.sql", "BEGIN;\n" + VIEW + "\nCOMMIT;"),
    ],
)
def test_a_rerun_file_follows_the_same_rules(app, name, text):
    app.rerun(name, text)
    with pytest.raises(pgforward.ConfigError):
        files.reruns([app.name])


def test_the_plan_shows_what_would_run_and_writes_nothing(db, app):
    app.add("20260101000000_checks.sql", CHECKS)
    app.add(
        "20260102000000_index.sql",
        "-- pgforward: no-transaction\nCREATE INDEX CONCURRENTLY c ON checks (name);",
    )
    app.rerun("views.sql", VIEW)

    plan = pgforward.plan(db, [app.name])

    assert [s.migration.where for s in plan.steps] == [
        f"{app.name}/migrations/20260101000000_checks.sql",
        f"{app.name}/migrations/20260102000000_index.sql",
        f"{app.name}/rerun/views.sql",
    ]
    assert plan.steps[0].settings == {"lock-timeout": "5s", "statement-timeout": "1min"}
    assert plan.steps[1].settings == {"lock-timeout": "0", "statement-timeout": "0"}
    assert query(db, "SELECT to_regclass('public.schema_migrations')") == [(None,)]


def test_grants_name_a_table_the_role_holds_nothing_on(db, app, limited_role):
    role, _ = limited_role
    app.add("20260101000000_checks.sql", CHECKS + "CREATE TABLE secrets (x int);")
    app.rerun("grants.sql", f'GRANT SELECT, INSERT ON checks TO "{role}";')
    pgforward.migrate(db, [app.name])

    found = pgforward.grants(db, role)

    assert {t.name: t.privileges for t in found.tables} == {
        "public.checks": ("SELECT", "INSERT"),
        "public.secrets": (),
    }
    assert not found.complete


def test_grants_refuse_a_role_that_does_not_exist(db):
    with pytest.raises(pgforward.Refused):
        pgforward.grants(db, "no_such_role_anywhere")


def test_schema_check_finds_a_database_that_differs_from_a_fresh_build(db, app):
    app.add("20260101000000_checks.sql", CHECKS)
    app.add("20260103000000_later.sql", "ALTER TABLE checks ADD COLUMN later int;")
    pgforward.migrate(db, [app.name])
    _, same = pgforward.check_schema(db, [app.name])
    app.add("20260102000000_teammate.sql", "ALTER TABLE checks ADD COLUMN paused int;")
    pgforward.migrate(db, [app.name])

    _, diff = pgforward.check_schema(db, [app.name])

    assert same == []
    assert any(line.startswith("-") and "paused" in line for line in diff)
    assert any(line.startswith("+") and "paused" in line for line in diff)


def test_a_role_cannot_read_the_reruns_table_either_without_a_grant(
    db, app, limited_role
):
    role, as_role = limited_role
    app.add("20260101000000_checks.sql", CHECKS)
    app.rerun("views.sql", VIEW)
    pgforward.migrate(db, [app.name])
    query(db, f'GRANT SELECT ON public.schema_migrations TO "{role}"')

    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        pgforward.status(as_role(db), [app.name])
