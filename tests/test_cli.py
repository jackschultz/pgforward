import json

from support import CHECKS, query

import pgforward
from pgforward import cli


def run(capsys, *argv):
    code = cli.main(list(argv))
    out, err = capsys.readouterr()
    return code, out, err


def test_status_exits_1_while_pending_and_0_once_current(project, app, db, capsys):
    app.add("20260101000000_checks.sql", CHECKS)

    code, out, _ = run(capsys, "status", "--url", db)
    assert code == 1
    assert out.splitlines()[0].endswith("(unmarked, treated as standing)")
    assert "next: pgforward migrate" in out

    code, out, _ = run(capsys, "migrate", "--url", db)
    assert code == 0
    assert "applied  20260101000000_checks.sql" in out
    assert "pgforward mark branch" in out

    assert run(capsys, "status", "--url", db)[0] == 0


def test_status_json_has_a_version_and_the_pending_files(project, app, db, capsys):
    app.add("20260101000000_checks.sql", CHECKS)

    code, out, _ = run(capsys, "status", "--json", "--url", db)
    body = json.loads(out)

    assert code == 1
    assert body["version"] == 1
    assert body["database"]["kind"] == "standing"
    assert body["database"]["marked"] is None
    assert [p["filename"] for p in body["pending"]] == ["20260101000000_checks.sql"]


def test_a_changed_file_exits_2_with_the_fix(project, app, branch_db, capsys):
    path = app.add("20260101000000_checks.sql", CHECKS)
    run(capsys, "migrate", "--url", branch_db)
    path.write_text(CHECKS + "\n-- edited\n")

    code, out, _ = run(capsys, "status", "--url", branch_db)
    assert code == 2
    assert "problem  changed  20260101000000_checks.sql" in out
    assert "pgforward rebuild" in out

    code, _, err = run(capsys, "migrate", "--url", branch_db)
    assert code == 2
    assert "fix:" in err and "pgforward rebuild" in err


def test_migrate_on_a_branch_database_writes_schema_sql(
    project, app, branch_db, capsys
):
    app.add("20260101000000_checks.sql", CHECKS)

    code, out, _ = run(capsys, "migrate", "--url", branch_db)

    assert code == 0
    assert "wrote    schema.sql" in out
    assert "CREATE TABLE public.checks" in (project / "schema.sql").read_text()


def test_errors_are_json_under_json(project, app, capsys, monkeypatch):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.delenv("MIGRATION_DATABASE_URL", raising=False)

    code, out, _ = run(capsys, "migrate", "--json")
    body = json.loads(out)

    assert code == 2
    assert body["error"]["code"] == "config"
    assert "--env-file .env" in body["error"]["fix"]


def test_new_creates_the_file_in_the_first_package(project, app, capsys):
    code, out, _ = run(capsys, "new", "add", "last", "ping", "at")

    assert code == 0
    [created] = list(app.folder.glob("*_add_last_ping_at.sql"))
    assert created.name in out


def test_without_a_pgforward_table_the_error_says_what_to_add(
    tmp_path, monkeypatch, capsys
):
    monkeypatch.chdir(tmp_path)
    code, _, err = run(capsys, "status", "--url", "dbname=x")
    assert code == 2
    assert "[tool.pgforward]" in err


def test_guide_prints(capsys):
    code, out, _ = run(capsys, "guide")
    assert code == 0
    assert out.startswith("# pgforward guide")


def test_a_database_error_exits_2_not_1(project, app, db, capsys):
    app.add("20260101000000_checks.sql", CHECKS)
    query(
        db,
        "CREATE VIEW public.schema_migrations AS"
        " SELECT 'x'::text AS filename, (1 / 0)::text AS checksum",
    )

    code, _, err = run(capsys, "status", "--url", db)

    assert code == 2
    assert "SQLSTATE 22012" in err


def test_a_bug_exits_3_with_its_traceback(project, app, db, capsys, monkeypatch):
    def broken(*args, **kwargs):
        raise RuntimeError("a bug")

    monkeypatch.setattr(pgforward, "status", broken)

    code, _, err = run(capsys, "status", "--url", db)

    assert code == 3
    assert "RuntimeError: a bug" in err


def test_rebuild_names_the_database_before_anything_else(project, app, db, capsys):
    app.add("20260101000000_checks.sql", CHECKS)

    code, out, err = run(capsys, "rebuild", "--url", db)

    assert code == 2
    assert out.splitlines()[0].endswith("(unmarked, treated as standing)")
    assert "drop" not in out


def test_schema_names_its_database(project, app, db, capsys):
    app.add("20260101000000_checks.sql", CHECKS)

    code, out, _ = run(capsys, "schema", "--json", "--url", db)

    assert code == 0
    assert json.loads(out)["database"]["kind"] == "standing"
