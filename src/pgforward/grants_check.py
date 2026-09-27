"""What the application's runtime role may do, table by table. Read-only.

The runtime role owns nothing and holds only the privileges a re-run file
(conventionally rerun/90_grants.sql) gives it. A new table the grants file
forgot is the common gap, so a table on which the role holds nothing is the
"no" answer. Schemas another tool owns in the same database
(`exclude_schemas`) are left out of both lists.
"""

import dataclasses
from collections.abc import Sequence

import psycopg

from pgforward import database, queries
from pgforward.errors import Refused

PRIVILEGES = ("SELECT", "INSERT", "UPDATE", "DELETE")


@dataclasses.dataclass(frozen=True)
class Table:
    name: str
    privileges: tuple[str, ...]


@dataclasses.dataclass(frozen=True)
class Grants:
    target: database.Target
    role: str
    tables: list[Table]
    schemas_without_usage: list[str]
    excluded_schemas: tuple[str, ...] = ()

    @property
    def complete(self) -> bool:
        return not self.schemas_without_usage and all(t.privileges for t in self.tables)


def check(url: str, role: str, exclude_schemas: Sequence[str] = ()) -> Grants:
    with database.connect(url) as conn, conn.transaction():
        conn.execute("SET TRANSACTION READ ONLY")
        return _check(conn, role, tuple(exclude_schemas))


def _check(conn: psycopg.Connection, role: str, excluded: tuple[str, ...]) -> Grants:
    target = database.target(conn)
    if not database.one(conn, queries.ROLE_EXISTS, (role,))[0]:
        raise Refused(
            f"{target.describe()}: there is no role {role!r}",
            "name the role the application connects as: pgforward grants --role <role>",
        )
    tables = [
        Table(name, tuple(p for p, held in zip(PRIVILEGES, row, strict=True) if held))
        for name, *row in conn.execute(
            queries.TABLE_PRIVILEGES, (*(role,) * 4, list(excluded))
        )
    ]
    schemas = [
        row[0]
        for row in conn.execute(queries.SCHEMAS_WITHOUT_USAGE, (list(excluded), role))
    ]
    return Grants(target, role, tables, schemas, excluded)
