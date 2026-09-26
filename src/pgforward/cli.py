"""The `pgforward` command. Parses, calls the library, renders; no SQL here.

Exit codes: 0 done or current, 1 migrations pending (`status`), 2 refused or
failed, 3 a bug in pgforward (with its traceback). `--json` output carries
"version": 1 and keeps its shape.
"""

import argparse
import importlib.metadata
import importlib.resources
import io
import json
import os
import pathlib
import sys
import traceback
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
    except psycopg.Error as problem:
        if problem.sqlstate is None:
            return _error(
                args,
                "connect",
                f"cannot connect: {str(problem).strip()}",
                "check the address, and that Postgres is running",
            )
        return _error(
            args,
            "database",
            f"the database refused: {problem.diag.message_primary} "
            f"(SQLSTATE {problem.sqlstate})",
            "",
        )
    except Exception:
        # A bug in pgforward, not an answer: never exit 1, which means pending.
        traceback.print_exc()
        return 3


def _parser() -> argparse.ArgumentParser:
    output = argparse.ArgumentParser(add_help=False)
    output.add_argument("--json", action="store_true", help="machine-readable output")
    common = argparse.ArgumentParser(add_help=False, parents=[output])
    common.add_argument(
        "--url",
        help="database address; default MIGRATION_DATABASE_URL, else DATABASE_URL",
    )
    parser = argparse.ArgumentParser(
        prog="pgforward",
        description="Postgres migrations in plain SQL files, forward only. "
        "`pgforward guide` explains how to use it.",
    )
    parser.add_argument(
        "--version", action="version", version=importlib.metadata.version("pgforward")
    )
    commands = parser.add_subparsers(required=True, metavar="command")

    def add(name: str, run, text: str, database=True) -> argparse.ArgumentParser:
        sub = commands.add_parser(
            name,
            parents=[common if database else output],
            help=text,
            description=text,
        )
        sub.set_defaults(run=run)
        return sub

    add("status", _status, "what is applied and pending; exits 1 when pending")
    migrate = add(
        "migrate", _migrate, "apply what is pending, then changed re-run files"
    )
    migrate.add_argument(
        "--dry-run",
        action="store_true",
        help="print the SQL that would run, with each file's settings; write nothing",
    )
    new = add(
        "new",
        _new,
        "create an empty migration file, named with the current UTC time",
        database=False,
    )
    new.add_argument("description", nargs="+", help="what the migration does")
    add(
        "rebuild",
        _rebuild,
        "drop a test or branch database, ending its other sessions, and apply "
        "every file",
    )
    mark = add("mark", _mark, "record what kind of database this is")
    mark.add_argument("kind", choices=database.KINDS)
    schema = add(
        "schema", _schema, "rewrite schema.sql from a fresh build of every file"
    )
    schema.add_argument(
        "--check",
        action="store_true",
        help="compare this database with a fresh build instead; exit 1 if they differ",
    )
    grants = add(
        "grants",
        _grants,
        "what the runtime role may do on each table; exit 1 if a table has nothing",
    )
    grants.add_argument("--role", required=True, help="the role the app runs as")
    add("guide", _guide, "print the guide for this version", database=False)
    return parser


# --- commands ---------------------------------------------------------------


def _status(args: argparse.Namespace) -> int:
    project = config.project()
    current = pgforward.status(config.database_url(args.url), project.packages)
    code = 2 if current.problems else 1 if current.pending_names else 0
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
                "ahead": current.ahead,
                "reruns_due": [r.where for r in current.reruns_due],
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
    for rerun in current.reruns_due:
        print(f"rerun    {rerun.where}  new or changed: runs on the next migrate")
    for problem in current.problems:
        print(f"problem  {problem.kind}  {problem.filename}")
    for name in current.ahead:
        print(
            f"ahead    {name}  the database ran it and this code does not have it: "
            "a newer release's migration"
        )
    print(f"applied  {len(current.applied)}, pending {len(current.pending)}")
    on_disk = (
        {a.filename for a in current.applied}
        - {p.filename for p in current.problems if p.kind == "missing"}
        - set(current.ahead)
    )
    if on_disk:
        print(f"latest   {max(on_disk)}")
    if fix:
        print(f"fix: {fix}")
    elif current.pending_names:
        print("next: pgforward migrate")
    return code


def _migrate(args: argparse.Namespace) -> int:
    project = config.project()
    if args.dry_run:
        return _dry_run(args, project)
    result = pgforward.migrate(
        config.database_url(args.url),
        project.packages,
        schema_file=project.schema_file,
        echo=None if args.json else print,
    )
    return _applied(args, "migrate", result, project)


def _dry_run(args: argparse.Namespace, project: config.Project) -> int:
    plan = pgforward.plan(config.database_url(args.url), project.packages)
    if args.json:
        _print_json(
            {
                "command": "migrate --dry-run",
                "database": plan.target.as_json(),
                "steps": [
                    {
                        "file": step.migration.where,
                        "transaction": step.migration.transaction,
                        "settings": step.settings,
                        "sql": step.migration.text,
                    }
                    for step in plan.steps
                ],
            }
        )
        return 0
    print(plan.target.describe())
    for step in plan.steps:
        wrapped = "in a transaction" if step.migration.transaction else "alone"
        print(f"\n-- {step.migration.where}, {wrapped}")
        for name, value in step.settings.items():
            print(f"SET {name.replace('-', '_')} = '{value}';")
        print(step.migration.text.rstrip())
    print(f"\n-- {len(plan.steps)} files would run; nothing was written")
    return 0


def _grants(args: argparse.Namespace) -> int:
    found = pgforward.grants(config.database_url(args.url), args.role)
    if args.json:
        _print_json(
            {
                "command": "grants",
                "database": found.target.as_json(),
                "role": found.role,
                "complete": found.complete,
                "tables": [vars(t) for t in found.tables],
                "schemas_without_usage": found.schemas_without_usage,
            }
        )
        return 0 if found.complete else 1
    print(found.target.describe())
    for schema_name in found.schemas_without_usage:
        print(f"no usage {schema_name}  {found.role} cannot reach its tables at all")
    for table in found.tables:
        held = " ".join(table.privileges) or "nothing"
        print(f"{'table' if table.privileges else 'none':<8} {table.name}  {held}")
    if found.complete:
        print(f"done     {found.role} holds something on every table")
        return 0
    print(
        f"fix: grant {found.role} what it needs in the re-run file that holds "
        "the grants (in rerun/), then pgforward migrate"
    )
    return 1


def _rebuild(args: argparse.Namespace) -> int:
    project = config.project()
    url = config.database_url(args.url)
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
                "reruns": [vars(r) for r in result.reruns],
                "schema_file": str(result.schema_file) if result.schema_file else None,
            }
        )
        return 0
    if result.schema_file:
        print(f"wrote    {_relative(result.schema_file)}")
    reran = f", {len(result.reruns)} re-run" if result.reruns else ""
    print(f"done     {len(result.applied)} applied{reran}; the database is current")
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
    lines: list[str] = []
    if args.check:
        target, diff = pgforward.check_schema(
            config.database_url(args.url), project.packages, echo=lines.append
        )
        if args.json:
            _print_json(
                {
                    "command": "schema --check",
                    "database": target.as_json(),
                    "matches": not diff,
                    "diff": diff,
                }
            )
            return 1 if diff else 0
        print(target.describe())
        for line in lines:
            print(line)
        if not diff:
            print("done     the schema matches a fresh build of every file")
            return 0
        for line in diff:
            print(line)
        print(
            "fix: on a branch database, `pgforward rebuild`; elsewhere, a new "
            "migration that makes the difference go away"
        )
        return 1
    target = pgforward.write_schema(
        config.database_url(args.url),
        project.schema_file,
        project.packages,
        echo=lines.append,
    )
    if args.json:
        _print_json(
            {
                "command": "schema",
                "database": target.as_json(),
                "schema_file": str(project.schema_file),
            }
        )
    else:
        print(target.describe())
        for line in lines:
            print(line)
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
