"""schema.sql: the schema a fresh build of every migration produces.

Built in a scratch database rather than dumped from the working one. A
migration merged late, older than files already applied, adds its columns
after theirs on the working database but before them on a fresh build, so a
dump of the working database carries an order nobody chose.
"""

import pathlib
import re
import shutil
import subprocess
from collections.abc import Callable, Sequence

from psycopg.conninfo import conninfo_to_dict

from pgforward import apply, database, queries
from pgforward.errors import MigrationFailed, SchemaDumpFailed

HEADER = (
    "-- Written by pgforward from a fresh build of every migration; do not edit.\n"
    "-- `pgforward schema` rewrites it.\n"
)
# Top-level lines that differ between machines or runs with the same schema
# (versions, session settings, and the \restrict lines pg_dump 17.6 and 18.0
# began writing with a random key in every dump), and pg_dump's per-object
# comment banners, which repeat the statement under them. Lines inside a
# dollar-quoted body are never touched: a function may hold any of these.
NOISE = re.compile(
    r"^(-- Dumped (from|by) |-- PostgreSQL database dump|SET |"
    r"SELECT pg_catalog\.set_config\('search_path'|\\restrict |\\unrestrict |"
    r"-- Name: .*; Type: .*; Schema: |--$)"
)
DOLLAR = re.compile(r"\$([A-Za-z_][A-Za-z_0-9]*)?\$")


def write(
    url: str,
    packages: Sequence[str],
    path: pathlib.Path,
    echo: Callable[[str], None] | None = None,
) -> database.Target:
    pg_dump, target = _pg_dump(url)
    with database.scratch(url) as scratch:
        name = conninfo_to_dict(scratch)["dbname"]
        try:
            apply.migrate(scratch, packages)
        except MigrationFailed as problem:
            raise SchemaDumpFailed(
                f"a fresh build of every migration fails (built in the scratch "
                f"database {name}, now dropped): {problem.message}. New databases "
                "and tests will fail the same way",
                f"on a branch database, `pgforward rebuild` shows it here. "
                f"{problem.fix}",
            ) from None
        dumped = subprocess.run(
            [
                pg_dump,
                "--schema-only",
                "--no-owner",
                "--no-privileges",
                "--exclude-table=public.schema_migrations",
                "--dbname",
                scratch,
            ],
            capture_output=True,
            text=True,
        )
    if dumped.returncode != 0:
        raise SchemaDumpFailed(
            f"pg_dump failed: {dumped.stderr.strip()}",
            "fix what it names, then run `pgforward schema`",
        )
    path.write_text(normalize(dumped.stdout))
    if echo:
        echo(
            f"scratch  built schema.sql in {name}, a database created on this "
            "server for it and dropped again"
        )
    return target


def normalize(dump: str) -> str:
    kept = []
    body = None  # the dollar-quote tag of a body the current line is inside
    for line in dump.splitlines():
        if body is None and NOISE.match(line):
            continue
        kept.append(line)
        for match in DOLLAR.finditer(line):
            if body is None:
                body = match[0]
            elif match[0] == body:
                body = None
    text = re.sub(r"\n{3,}", "\n\n", "\n".join(kept)).strip()
    return f"{HEADER}\n{text}\n"


def _pg_dump(url: str) -> tuple[str, database.Target]:
    found = shutil.which("pg_dump")
    with database.connect(url) as conn:
        server = database.one(conn, queries.SERVER_VERSION)[0]
        target = database.target(conn)
    if found is None:
        raise SchemaDumpFailed(
            "pg_dump is not on PATH; schema.sql is written with it",
            f"install the Postgres {server} client tools (on macOS: brew install "
            "libpq, then put its bin/ on PATH), then run `pgforward schema`",
        )
    version = subprocess.run([found, "--version"], capture_output=True, text=True)
    match = re.search(r"(\d+)\.", version.stdout)
    client = int(match[1]) if match else 0
    if client < server:
        raise SchemaDumpFailed(
            f"pg_dump is version {client} and the server {server}; pg_dump must be "
            "at least the server's version",
            f"install the Postgres {server} client tools, then run `pgforward schema`",
        )
    return found, target
