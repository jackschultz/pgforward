import pytest
from support import CHECKS, query

import pgforward
from pgforward import schema


def columns(text: str, table: str) -> list[str]:
    body = text.split(f"CREATE TABLE public.{table} (", 1)[1].split(");", 1)[0]
    return [line.split()[0] for line in body.strip().splitlines()]


def test_migrate_on_a_branch_database_writes_the_fresh_build_order(
    branch_db, app, tmp_path
):
    """After a late merge the branch database has `later` before `paused`; a
    fresh build, and so schema.sql, has them the other way round."""
    target = tmp_path / "schema.sql"
    app.add("20260101000000_checks.sql", CHECKS)
    app.add("20260103000000_later.sql", "ALTER TABLE checks ADD COLUMN later int;")
    pgforward.migrate(branch_db, [app.name], schema_file=target)
    app.add("20260102000000_teammate.sql", "ALTER TABLE checks ADD COLUMN paused int;")

    result = pgforward.migrate(branch_db, [app.name], schema_file=target)

    assert result.schema_file == target
    assert query(
        branch_db,
        "SELECT column_name FROM information_schema.columns"
        " WHERE table_name = 'checks' ORDER BY ordinal_position",
    ) == [("id",), ("name",), ("later",), ("paused",)]
    assert columns(target.read_text(), "checks") == ["id", "name", "paused", "later"]


def test_schema_file_is_the_same_on_every_write_and_holds_no_noise(db, app, tmp_path):
    app.add("20260101000000_checks.sql", CHECKS)
    first, second = tmp_path / "a.sql", tmp_path / "b.sql"

    pgforward.write_schema(db, first, [app.name])
    pgforward.write_schema(db, second, [app.name])

    text = first.read_text()
    assert text == second.read_text()
    assert text.startswith(schema.HEADER)
    assert "schema_migrations" not in text
    for noise in ("\\restrict", "SET ", "Dumped by", "set_config"):
        assert noise not in text
    assert query(
        db, "SELECT count(*) FROM pg_database WHERE datname LIKE 'pgforward_scratch_%'"
    ) == [(0,)]


def test_migrate_writes_no_schema_file_on_a_database_that_is_not_a_branch(
    db, app, tmp_path
):
    target = tmp_path / "schema.sql"
    app.add("20260101000000_checks.sql", CHECKS)

    result = pgforward.migrate(db, [app.name], schema_file=target)

    assert result.schema_file is None
    assert not target.exists()


def test_a_missing_pg_dump_says_the_migrations_were_applied(
    branch_db, app, tmp_path, monkeypatch
):
    app.add("20260101000000_checks.sql", CHECKS)
    monkeypatch.setattr(schema.shutil, "which", lambda name: None)

    with pytest.raises(pgforward.SchemaDumpFailed) as failed:
        pgforward.migrate(branch_db, [app.name], schema_file=tmp_path / "schema.sql")

    assert failed.value.message.startswith("1 applied")
    assert "pg_dump" in failed.value.message
    assert query(branch_db, "SELECT count(*) FROM public.schema_migrations") == [(1,)]


def test_function_bodies_come_through_schema_sql_whole(db, app, tmp_path):
    app.add(
        "20260101000000_touch.sql",
        """CREATE TABLE a (x int, touched timestamptz);
CREATE FUNCTION touch() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
UPDATE a
SET touched = now()
WHERE x = NEW.x;
--
RETURN NEW;
END;
$$;
""",
    )
    target = tmp_path / "schema.sql"

    pgforward.write_schema(db, target, [app.name])

    text = target.read_text()
    assert "UPDATE a\nSET touched = now()\nWHERE x = NEW.x;\n--\nRETURN NEW;" in text


def test_a_fresh_build_that_fails_is_reported_as_that(branch_db, app, tmp_path):
    app.add("20260101000000_a.sql", "CREATE TABLE a (x int);")
    app.add("20260103000000_c.sql", "CREATE TABLE c (x int);")
    pgforward.migrate(branch_db, [app.name])
    app.add("20260102000000_b.sql", "ALTER TABLE c ADD COLUMN y int;")

    with pytest.raises(pgforward.SchemaDumpFailed) as failed:
        pgforward.migrate(branch_db, [app.name], schema_file=tmp_path / "schema.sql")

    assert failed.value.message.startswith("1 applied")
    assert "a fresh build of every migration fails" in failed.value.message
    assert "20260102000000_b.sql" in failed.value.message
    assert "pgforward rebuild" in failed.value.fix


def test_the_scratch_database_is_announced(branch_db, app, tmp_path):
    app.add("20260101000000_checks.sql", CHECKS)
    said: list[str] = []

    pgforward.migrate(
        branch_db, [app.name], schema_file=tmp_path / "schema.sql", echo=said.append
    )

    [line] = [x for x in said if x.startswith("scratch")]
    assert "pgforward_scratch_" in line and "dropped" in line
