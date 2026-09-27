"""[tool.pgforward] exclude_schemas: schemas another tool owns in the same
database (pgtrail keeps its tables in schema `pgtrail` and runs its own
migrations), left out of `schema`, `schema --check` and `grants`."""

import json

import pytest
from support import CHECKS, query

import pgforward
from pgforward import cli, config

NOTE = "note     leaves out schemas pgtrail, as exclude_schemas says"


def run(capsys, *argv):
    code = cli.main(list(argv))
    out, err = capsys.readouterr()
    return code, out, err


@pytest.fixture
def excluding(project, app):
    """The project's pyproject.toml, with exclude_schemas = ["pgtrail"]."""
    path = project / "pyproject.toml"
    path.write_text(path.read_text() + 'exclude_schemas = ["pgtrail"]\n')
    return project


@pytest.fixture
def shared_db(db, app):
    """The app's own table from its migrations, and a schema `pgtrail` with a
    table that no migration of the app's made."""
    app.add("20260101000000_checks.sql", CHECKS)
    pgforward.migrate(db, [app.name])
    query(db, "CREATE SCHEMA pgtrail; CREATE TABLE pgtrail.logs (line text)")
    return db


def test_schema_check_leaves_out_an_excluded_schema(excluding, shared_db, capsys):
    code, out, _ = run(capsys, "schema", "--check", "--url", shared_db)

    assert code == 0, out
    assert out.count(NOTE) == 1, out
    assert "pgtrail" not in out.replace(NOTE, "")


def test_schema_check_without_the_setting_reports_the_other_schema(
    project, shared_db, capsys
):
    code, out, _ = run(capsys, "schema", "--check", "--url", shared_db)

    assert code == 1
    assert "+CREATE TABLE pgtrail.logs (" in out
    assert "leaves out" not in out


def test_schema_check_still_finds_a_difference_in_the_apps_own_schema(
    excluding, shared_db, capsys
):
    query(shared_db, "ALTER TABLE public.checks ADD COLUMN stray int")

    code, out, _ = run(capsys, "schema", "--check", "--json", "--url", shared_db)
    body = json.loads(out)

    assert code == 1
    assert body["excluded_schemas"] == ["pgtrail"]
    assert "+    stray integer" in body["diff"]
    assert not [line for line in body["diff"] if "pgtrail" in line]


def test_schema_file_leaves_out_an_excluded_schema(excluding, app, db, capsys):
    """The fresh build's dump is filtered too: a migration of the app's that
    writes into the excluded schema does not reach schema.sql."""
    app.add(
        "20260101000000_checks.sql",
        CHECKS + "CREATE SCHEMA pgtrail; CREATE TABLE pgtrail.logs (line text);",
    )

    code, out, _ = run(capsys, "schema", "--url", db)

    text = (excluding / "schema.sql").read_text()
    assert code == 0
    assert out.count(NOTE) == 1, out
    assert "CREATE TABLE public.checks" in text
    assert "pgtrail" not in text


def grant_the_apps_table(app, db, role):
    app.rerun("grants.sql", f'GRANT SELECT, INSERT ON checks TO "{role}";')
    pgforward.migrate(db, [app.name])


def test_grants_leave_out_an_excluded_schema(
    excluding, app, shared_db, limited_role, capsys
):
    role, _ = limited_role
    grant_the_apps_table(app, shared_db, role)

    code, out, _ = run(capsys, "grants", "--role", role, "--json", "--url", shared_db)
    body = json.loads(out)

    assert code == 0, out
    assert body["complete"] is True
    assert body["excluded_schemas"] == ["pgtrail"]
    assert body["schemas_without_usage"] == []
    assert body["tables"] == [
        {"name": "public.checks", "privileges": ["SELECT", "INSERT"]}
    ]


def test_grants_say_once_which_schemas_they_leave_out(
    excluding, app, shared_db, limited_role, capsys
):
    role, _ = limited_role
    grant_the_apps_table(app, shared_db, role)

    code, out, _ = run(capsys, "grants", "--role", role, "--url", shared_db)

    assert code == 0, out
    assert out.count(NOTE) == 1, out


def test_grants_without_the_setting_count_the_other_schema(
    project, app, shared_db, limited_role, capsys
):
    role, _ = limited_role
    grant_the_apps_table(app, shared_db, role)

    code, out, _ = run(capsys, "grants", "--role", role, "--json", "--url", shared_db)
    body = json.loads(out)

    assert code == 1
    assert body["complete"] is False
    assert body["excluded_schemas"] == []
    assert body["schemas_without_usage"] == ["pgtrail"]
    assert {"name": "pgtrail.logs", "privileges": []} in body["tables"]


@pytest.mark.parametrize("value", ['"pgtrail"', '[""]', "[1]", '["pgtrail", 2]', "{}"])
def test_a_bad_exclude_schemas_is_refused_naming_the_key(tmp_path, value):
    (tmp_path / "pyproject.toml").write_text(
        f'[tool.pgforward]\npackages = ["app"]\nexclude_schemas = {value}\n'
    )

    with pytest.raises(pgforward.ConfigError, match="exclude_schemas must be a list"):
        config.project(tmp_path)
