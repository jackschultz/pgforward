"""A real Postgres. Each test makes its own databases and packages.

`TEST_DATABASE_URL` names the server and must be a database ending in `_test`
that is not `DATABASE_URL`; tests never write to it. They create databases
named `pft_<random>` beside it, and drop exactly those afterwards, so the role
needs CREATEDB.
"""

import os
import pathlib
import secrets

import psycopg
import pytest
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict, make_conninfo

ROOT = pathlib.Path(__file__).resolve().parents[1]


def _load_env() -> None:
    env = ROOT / ".env"
    if not env.exists():
        return
    for line in env.read_text().splitlines():
        name, sep, value = line.partition("=")
        if sep and not line.lstrip().startswith("#"):
            os.environ.setdefault(name.strip(), value.strip())


@pytest.fixture(scope="session")
def server_url() -> str:
    _load_env()
    found = os.environ.get("TEST_DATABASE_URL")
    if not found:
        pytest.exit("TEST_DATABASE_URL is not set", returncode=2)
    if found == os.environ.get("DATABASE_URL"):
        pytest.exit("TEST_DATABASE_URL is DATABASE_URL; refusing", returncode=2)
    name = str(conninfo_to_dict(found).get("dbname", ""))
    if not name.endswith("_test"):
        pytest.exit(f"test database {name!r} does not end in _test", returncode=2)
    return found


@pytest.fixture
def make_database(server_url):
    created: list[str] = []

    def make(suffix: str = "") -> str:
        name = f"pft_{secrets.token_hex(4)}{suffix}"
        with psycopg.connect(server_url, autocommit=True) as conn:
            conn.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
        created.append(name)
        return make_conninfo(server_url, dbname=name)

    yield make
    with psycopg.connect(server_url, autocommit=True) as conn:
        for name in created:
            conn.execute(
                sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(
                    sql.Identifier(name)
                )
            )


@pytest.fixture
def db(make_database) -> str:
    """An empty, unmarked database."""
    return make_database()


@pytest.fixture
def branch_db(db) -> str:
    with psycopg.connect(db, autocommit=True) as conn:
        name = conn.execute("SELECT current_database()").fetchone()[0]
        conn.execute(
            sql.SQL("ALTER DATABASE {} SET pgforward.kind = 'branch'").format(
                sql.Identifier(name)
            )
        )
    return db


class Package:
    """An importable package with a migrations/ folder, on sys.path."""

    def __init__(self, root: pathlib.Path) -> None:
        self.name = f"pkg_{secrets.token_hex(4)}"
        self.folder = root / self.name / "migrations"
        self.folder.mkdir(parents=True)
        (root / self.name / "__init__.py").write_text("")

    def add(self, filename: str, text: str) -> pathlib.Path:
        path = self.folder / filename
        path.write_text(text)
        return path

    def rerun(self, filename: str, text: str) -> pathlib.Path:
        folder = self.folder.parent / "rerun"
        folder.mkdir(exist_ok=True)
        path = folder / filename
        path.write_text(text)
        return path


@pytest.fixture
def make_package(tmp_path, monkeypatch):
    monkeypatch.syspath_prepend(str(tmp_path))
    return lambda: Package(tmp_path)


@pytest.fixture
def app(make_package) -> Package:
    return make_package()


@pytest.fixture
def project(tmp_path, app, monkeypatch) -> pathlib.Path:
    """A project folder whose pyproject.toml names `app`, as the cwd."""
    (tmp_path / "pyproject.toml").write_text(
        f'[project]\nname = "x"\n\n[tool.pgforward]\npackages = ["{app.name}"]\n'
    )
    monkeypatch.chdir(tmp_path)
    return tmp_path


@pytest.fixture
def limited_role(server_url):
    """A login role that is not a superuser, dropped afterwards. Returns a
    function giving an address that connects as it."""
    name = f"pft_role_{secrets.token_hex(4)}"
    password = secrets.token_hex(12)
    with psycopg.connect(server_url, autocommit=True) as conn:
        conn.execute(
            sql.SQL("CREATE ROLE {} LOGIN PASSWORD {}").format(
                sql.Identifier(name), sql.Literal(password)
            )
        )
    yield name, lambda url: make_conninfo(url, user=name, password=password)
    with psycopg.connect(server_url, autocommit=True) as conn:
        rows = conn.execute(
            "SELECT datname FROM pg_database WHERE datdba = %s::regrole", (name,)
        ).fetchall()
        for (database,) in rows:
            conn.execute(
                sql.SQL("ALTER DATABASE {} OWNER TO CURRENT_USER").format(
                    sql.Identifier(database)
                )
            )
        conn.execute(sql.SQL("DROP ROLE {}").format(sql.Identifier(name)))
