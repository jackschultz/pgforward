"""The `pgforward` command. Parses, calls the library, renders; no SQL here.

Exit codes: 0 done or current, 1 migrations pending (`status`), 2 refused or
failed. `--json` output carries "version": 1 and keeps its shape.
"""

import argparse
import importlib.metadata
import importlib.resources
import io
import json
import os
import pathlib
import sys
from collections.abc import Sequence
from typing import Any

import psycopg

import pgforward
from pgforward import config, database, files
from pgforward.errors import PgforwardError

JSON_VERSION = 1


def main(argv: Sequence[str] | None = None) -> int:
    # Agents read this piped; keep stdout lines in order with stderr's.
    if isinstance(sys.stdout, io.TextIOWrapper):
        sys.stdout.reconfigure(line_buffering=True)
    args = _parser().parse_args(argv)
    try:
        return args.run(args)
    except PgforwardError as problem:
        return _error(args, problem.code, problem.message, problem.fix)
    except psycopg.OperationalError as problem:
        return _error(
            args,
            "connect",
            f"cannot connect: {str(problem).strip()}",
            "check the address, and that Postgres is running",
        )


def _parser() -> argparse.ArgumentParser:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument(
        "--url",
        help="database address; default MIGRATION_DATABASE_URL, else DATABASE_URL",
    )
    common.add_argument("--json", action="store_true", help="machine-readable output")
    parser = argparse.ArgumentParser(
        prog="pgforward",
        description="Postgres migrations in plain SQL files, forward only. "
        "`pgforward guide` explains how to use it.",
    )
    parser.add_argument(
        "--version", action="version", version=importlib.metadata.version("pgforward")
    )
    commands = parser.add_subparsers(required=True, metavar="command")

    def add(name: str, run, text: str) -> argparse.ArgumentParser:
        sub = commands.add_parser(name, parents=[common], help=text, description=text)
        sub.set_defaults(run=run)
        return sub

    add("status", _status, "what is applied and pending; exits 1 when pending")
    add("migrate", _migrate, "apply what is pending")
    new = add("new", _new, "create an empty, correctly named migration file")
    new.add_argument("description", nargs="+", help="what the migration does")
    add("rebuild", _rebuild, "drop a test or branch database and apply every file")
    mark = add("mark", _mark, "record what kind of database this is")
    mark.add_argument("kind", choices=database.KINDS)
    add("schema", _schema, "rewrite schema.sql from a fresh build of every file")
    add("guide", _guide, "print the guide for this version")
    return parser


# --- commands ---------------------------------------------------------------


def _status(args: argparse.Namespace) -> int:
    project = config.project()
    current = pgforward.status(config.database_url(args.url), project.packages)
    code = 2 if current.problems else 1 if current.pending else 0
    fix = ""
    try:
        pgforward.ledger.refuse(current)
    except PgforwardError as problem:
        fix = problem.fix
    if args.json:
        _print_json(
            {
                "command": "status",
                "database": current.target.as_json(),
                "current": current.current,
                "applied": [vars(a) for a in current.applied],
                "pending": [
                    {
                        "filename": p.migration.filename,
                        "package": p.migration.package,
                        "out_of_order": p.out_of_order,
                    }
                    for p in current.pending
                ],
                "problems": [vars(p) for p in current.problems],
                "fix": fix,
            }
        )
        return code
    print(current.target.describe())
    for p in current.pending:
        late = (
            "  older than applied files: will run out of order"
            if p.out_of_order
            else ""
        )
        print(f"pending  {p.migration.filename}  {p.migration.package}{late}")
    for problem in current.problems:
        print(f"problem  {problem.kind}  {problem.filename}")
    print(f"applied  {len(current.applied)}, pending {len(current.pending)}")
    if current.applied:
        print(f"latest   {max(a.filename for a in current.applied)}")
    if fix:
        print(f"fix: {fix}")
    elif current.pending:
        print("next: pgforward migrate")
    return code


def _migrate(args: argparse.Namespace) -> int:
    project = config.project()
    result = pgforward.migrate(
        config.database_url(args.url),
        project.packages,
        schema_file=project.schema_file,
        echo=None if args.json else print,
    )
    return _applied(args, "migrate", result, project)


def _rebuild(args: argparse.Namespace) -> int:
    project = config.project()
    url = config.database_url(args.url)
    if not args.json:
        print("dropping and recreating the database, then applying every file")
    result = pgforward.rebuild(
        url,
        project.packages,
        schema_file=project.schema_file,
        echo=None if args.json else print,
    )
    return _applied(args, "rebuild", result, project)


def _applied(
    args: argparse.Namespace,
    command: str,
    result: pgforward.Result,
    project: config.Project,
) -> int:
    if args.json:
        _print_json(
            {
                "command": command,
                "database": result.target.as_json(),
                "applied": [vars(r) for r in result.applied],
                "schema_file": str(result.schema_file) if result.schema_file else None,
            }
        )
        return 0
    if result.schema_file:
        print(f"wrote    {_relative(result.schema_file)}")
    print(f"done     {len(result.applied)} applied; the database is current")
    if result.target.marked is None:
        print(
            "note     this database is unmarked, so it is treated as standing. If "
            "only this checkout uses it, `pgforward mark branch` lets pgforward "
            f"rebuild it and keep {_relative(project.schema_file)} current"
        )
    return 0


def _new(args: argparse.Namespace) -> int:
    project = config.project()
    path = files.new(project.packages[0], " ".join(args.description))
    if args.json:
        _print_json({"command": "new", "path": str(path)})
    else:
        print(f"created  {_relative(path)}")
    return 0


def _mark(args: argparse.Namespace) -> int:
    before, after = pgforward.mark(config.database_url(args.url), args.kind)
    if args.json:
        _print_json(
            {"command": "mark", "before": before.marked, "database": after.as_json()}
        )
    else:
        print(f"marked   {after.name}: {before.marked or 'unmarked'} -> {after.kind}")
    return 0


def _schema(args: argparse.Namespace) -> int:
    project = config.project()
    pgforward.write_schema(
        config.database_url(args.url), project.schema_file, project.packages
    )
    if args.json:
        _print_json({"command": "schema", "schema_file": str(project.schema_file)})
    else:
        print(f"wrote    {_relative(project.schema_file)}")
    return 0


def _guide(args: argparse.Namespace) -> int:
    text = (importlib.resources.files("pgforward") / "docs" / "guide.md").read_text()
    if args.json:
        _print_json({"command": "guide", "guide": text})
    else:
        print(text, end="")
    return 0


# --- output -----------------------------------------------------------------


def _error(args: argparse.Namespace, code: str, message: str, fix: str) -> int:
    if getattr(args, "json", False):
        _print_json({"error": {"code": code, "message": message, "fix": fix}})
    else:
        print(f"pgforward: {message}", file=sys.stderr)
        if fix:
            print(f"fix: {fix}", file=sys.stderr)
    return 2


def _print_json(body: dict[str, Any]) -> None:
    print(json.dumps({"version": JSON_VERSION, **body}, indent=2, default=str))


def _relative(path: pathlib.Path) -> str:
    try:
        return os.path.relpath(path)
    except ValueError:
        return str(path)
