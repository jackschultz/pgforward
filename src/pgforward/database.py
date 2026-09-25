"""Which database a command is about, what kind it is, and whole-database work.

A database's kind is stored in the database itself, as the database-level
setting `pgforward.kind`, so it survives a rebuild and travels with the
database rather than with a name pattern or a file on one machine:

    test        tests may empty it; pgforward rebuilds it freely
    branch      one checkout's development database; rebuilt on request
    standing    the copy someone uses; never rebuilt
    production  never rebuilt; no scratch databases on its server

A database nobody marked is treated as standing.
"""

import contextlib
import dataclasses
import secrets
from collections.abc import Iterator
from typing import Any, Literal, LiteralString, cast

import psycopg
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict, make_conninfo

from pgforward import queries
from pgforward.errors import ConfigError, Refused

Kind = Literal["test", "branch", "standing", "production"]
KINDS: tuple[Kind, ...] = ("test", "branch", "standing", "production")
DISPOSABLE: tuple[Kind, ...] = ("test", "branch")
APPLICATION = "pgforward"


@dataclasses.dataclass(frozen=True)
class Target:
    name: str
    host: str
    port: str
    marked: Kind | None

    @property
    def kind(self) -> Kind:
        return self.marked or "standing"

    def describe(self) -> str:
        kind = self.marked or "unmarked, treated as standing"
        return f"{self.name} on {self.host}:{self.port} ({kind})"

    def as_json(self) -> dict[str, str | None]:
        return {
            "name": self.name,
            "host": self.host,
            "port": self.port,
            "kind": self.kind,
            "marked": self.marked,
        }


def connect(url: str, **kwargs) -> psycopg.Connection:
    return psycopg.connect(url, autocommit=True, application_name=APPLICATION, **kwargs)


def target(conn: psycopg.Connection) -> Target:
    name, marked = one(conn, queries.TARGET)
    if marked is not None and marked not in KINDS:
        raise Refused(
            f"{name} is marked with an unknown kind {marked!r}",
            f"mark it again: pgforward mark <{'|'.join(KINDS)}>",
        )
    host = conn.info.host
    if host.startswith("/"):
        host = f"socket {host}"
    return Target(name, host, str(conn.info.port), cast(Kind | None, marked))


def one(
    conn: psycopg.Connection, query: LiteralString, params: tuple = ()
) -> tuple[Any, ...]:
    """The single row a query always returns."""
    row = conn.execute(query, params or None).fetchone()
    if row is None:
        raise RuntimeError(f"no row from {query.split()[0:4]}")
    return row


def same_database(url: str, other: str) -> bool:
    return _address(url) == _address(other)


def _address(url: str) -> tuple[str, str, str]:
    parts = conninfo_to_dict(url)
    host = str(parts.get("host") or "localhost")
    if host in ("127.0.0.1", "::1"):
        host = "localhost"
    return str(parts.get("dbname")), host, str(parts.get("port") or "5432")


def mark(url: str, kind: Kind) -> tuple[Target, Target]:
    """Set the database's kind; returns it before and after."""
    if kind not in KINDS:
        raise ConfigError(f"unknown kind {kind!r}", f"one of: {', '.join(KINDS)}")
    with connect(url) as conn:
        before = target(conn)
        if before.marked in ("standing", "production") and kind in DISPOSABLE:
            raise Refused(
                f"{before.name} is marked {before.marked}; pgforward will not make "
                "it disposable",
                "if the mark is really wrong, clear it by hand: "
                f"ALTER DATABASE {before.name} RESET pgforward.kind",
            )
        if kind == "test" and not before.name.endswith("_test"):
            raise Refused(
                f"{before.name} cannot be marked test: a test database's name "
                "ends in _test",
                "create one named <name>_test and mark that",
            )
        _set_kind(conn, before.name, kind)
    with connect(url) as conn:
        return before, target(conn)


def recreate(url: str) -> Target:
    """Drop the database and create it empty, keeping its kind. Disposable only."""
    with connect(url) as conn:
        current = target(conn)
    if current.kind not in DISPOSABLE:
        raise Refused(
            f"{current.describe()} is not disposable; rebuild drops the whole database",
            "a database only this checkout uses can be marked: pgforward mark branch",
        )
    with _maintenance(url) as admin:
        _drop(admin, current.name)
        admin.execute(
            sql.SQL("CREATE DATABASE {}").format(sql.Identifier(current.name))
        )
        _set_kind(admin, current.name, current.kind)
    with connect(url) as conn:
        return target(conn)


@contextlib.contextmanager
def scratch(url: str) -> Iterator[str]:
    """A new empty database on the same server, dropped afterwards."""
    with connect(url) as conn:
        current = target(conn)
    if current.kind == "production":
        raise Refused(
            f"{current.describe()}: pgforward creates no scratch database on a "
            "production server",
            "write schema.sql from a development checkout",
        )
    name = f"pgforward_scratch_{secrets.token_hex(4)}"
    with _maintenance(url) as admin:
        admin.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
    try:
        yield make_conninfo(url, dbname=name)
    finally:
        with _maintenance(url) as admin:
            _drop(admin, name)


@contextlib.contextmanager
def _maintenance(url: str) -> Iterator[psycopg.Connection]:
    try:
        with connect(make_conninfo(url, dbname="postgres")) as admin:
            yield admin
    except psycopg.errors.InsufficientPrivilege as problem:
        raise Refused(
            f"the role cannot create or drop databases: {problem}".strip(),
            "locally, give the role CREATEDB (ALTER ROLE <role> CREATEDB)",
        ) from None


def _drop(admin: psycopg.Connection, name: str) -> None:
    admin.execute(
        sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(sql.Identifier(name))
    )


def _set_kind(conn: psycopg.Connection, name: str, kind: Kind) -> None:
    conn.execute(
        sql.SQL("ALTER DATABASE {} SET pgforward.kind = {}").format(
            sql.Identifier(name), sql.Literal(kind)
        )
    )
