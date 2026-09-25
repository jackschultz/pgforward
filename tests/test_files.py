import datetime as dt

import pytest
from support import CHECKS

import pgforward
from pgforward import files


def test_a_name_in_two_packages_is_an_error(app, make_package):
    other = make_package()
    app.add("20260101000000_checks.sql", CHECKS)
    other.add("20260101000000_checks.sql", CHECKS)

    with pytest.raises(pgforward.ConfigError) as refused:
        files.find([app.name, other.name])
    assert app.name in refused.value.message and other.name in refused.value.message


def test_a_badly_named_file_is_an_error(app):
    app.add("001_checks.sql", CHECKS)
    with pytest.raises(pgforward.ConfigError) as refused:
        files.find([app.name])
    assert "001_checks.sql" in refused.value.message


@pytest.mark.parametrize(
    "text",
    [
        "-- pgforward: no-transation\nSELECT 1;",
        "-- pgforward: statement-timeout=soon\nSELECT 1;",
        "SELECT 1;\n-- pgforward: no-transaction\n",
    ],
)
def test_an_unknown_malformed_or_misplaced_directive_is_an_error(app, text):
    app.add("20260101000000_x.sql", text)
    with pytest.raises(pgforward.ConfigError) as refused:
        files.find([app.name])
    assert "line" in refused.value.message


def test_directives_may_follow_other_opening_comments(app):
    app.add(
        "20260101000000_x.sql",
        "-- Why this index: the late-ping query.\n\n-- pgforward: no-transaction\n"
        "CREATE INDEX CONCURRENTLY x ON checks (name);\n",
    )
    [migration] = files.find([app.name])
    assert migration.transaction is False


def test_a_package_without_migrations_names_the_folder_to_create(make_package):
    package = make_package()
    package.folder.rmdir()
    with pytest.raises(pgforward.ConfigError) as refused:
        files.find([package.name])
    assert f"src/{package.name}/migrations/" in refused.value.fix


def test_new_names_the_file_and_never_reuses_a_timestamp(app):
    moment = dt.datetime(2026, 9, 25, 12, 0, 0, tzinfo=dt.UTC)

    first = files.new(app.name, "Add last ping at!", now=moment)
    second = files.new(app.name, "add paused", now=moment)

    assert first.name == "20260925120000_add_last_ping_at.sql"
    assert second.name == "20260925120001_add_paused.sql"
    assert sorted(p.name for p in app.folder.iterdir()) == [first.name, second.name]


def test_new_refuses_to_write_into_an_installed_package(app, monkeypatch, tmp_path):
    installed = tmp_path / "lib" / "site-packages" / app.name / "migrations"
    installed.mkdir(parents=True)
    monkeypatch.setattr(files, "folder", lambda package: installed)

    with pytest.raises(pgforward.ConfigError) as refused:
        files.new(app.name, "add paused")

    assert "site-packages" in refused.value.message
    assert list(installed.iterdir()) == []


@pytest.mark.parametrize("text", ["", "\n  \n", "-- to do\n"])
def test_a_file_with_no_sql_is_an_error(app, text):
    app.add("20260101000000_empty.sql", text)
    with pytest.raises(pgforward.ConfigError) as refused:
        files.find([app.name])
    assert "holds no SQL" in refused.value.message
