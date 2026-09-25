"""Migration files: found inside installed packages, named, read, checked.

Each package keeps its migrations in `<package>/migrations/`. They are read
through the installed package, so an app installed from a wheel finds its own
files and its libraries' alike. Every file is ordered by name, which starts
with a timestamp, into one sequence.
"""

import dataclasses
import datetime as dt
import hashlib
import importlib
import importlib.resources
import pathlib
import re
from collections.abc import Sequence
from importlib.resources.abc import Traversable

from pgforward import statements
from pgforward.errors import ConfigError

NAME = re.compile(r"^\d{14}_[a-z0-9_]+\.sql$")
DIRECTIVE = re.compile(r"^--\s*pgforward:(.*)$")
DURATION = re.compile(r"^\d+(ms|s|min|h)?$")
SETTINGS = ("lock-timeout", "statement-timeout")


@dataclasses.dataclass(frozen=True)
class Migration:
    filename: str
    package: str
    source: Traversable
    text: str
    checksum: str
    transaction: bool
    settings: dict[str, str]


def find(packages: Sequence[str]) -> list[Migration]:
    found: dict[str, Migration] = {}
    for package in packages:
        for migration in _package_files(package):
            if migration.filename in found:
                raise ConfigError(
                    f"{migration.filename} is in both "
                    f"{found[migration.filename].package} and {package}",
                    "a migration file name must be unique across every package",
                )
            found[migration.filename] = migration
    return [found[name] for name in sorted(found)]


def folder(package: str) -> Traversable:
    try:
        root = importlib.resources.files(package)
    except ModuleNotFoundError:
        raise ConfigError(
            f"package {package!r} is not importable",
            "check [tool.pgforward] packages in pyproject.toml, then `uv sync`",
        ) from None
    migrations = root / "migrations"
    if not migrations.is_dir():
        raise ConfigError(
            f"package {package!r} has no migrations/ folder ({migrations})",
            f"create it: the app's migrations live in src/{package}/migrations/",
        )
    return migrations


def new(package: str, description: str, now: dt.datetime | None = None) -> pathlib.Path:
    slug = re.sub(r"[^a-z0-9]+", "_", description.lower()).strip("_")
    if not slug:
        raise ConfigError(
            f"{description!r} has no letters or digits to name a file with",
            "pgforward new add_last_ping_at",
        )
    target = pathlib.Path(str(folder(package)))
    if "site-packages" in target.parts:
        raise ConfigError(
            f"{package} is installed into site-packages, not from source ({target})",
            "run `pgforward new` from the project's own checkout after `uv sync`",
        )
    moment = now or dt.datetime.now(dt.UTC)
    taken = {p.name[:14] for p in target.glob("*.sql")}
    while (stamp := moment.strftime("%Y%m%d%H%M%S")) in taken:
        moment += dt.timedelta(seconds=1)
    path = target / f"{stamp}_{slug}.sql"
    path.write_text("")
    return path


def _package_files(package: str) -> list[Migration]:
    migrations = []
    for entry in folder(package).iterdir():
        where = f"{package}/migrations/{entry.name}"
        if ".sql" not in entry.name.lower():
            continue
        if not NAME.fullmatch(entry.name):
            raise ConfigError(
                f"{where}: a migration is named YYYYMMDDHHMMSS_description.sql, "
                "lowercase",
                "pgforward new <description> writes a correctly named file; "
                "rename or move this one",
            )
        data = entry.read_bytes()
        try:
            text = data.decode("utf-8-sig")
        except UnicodeDecodeError as problem:
            raise ConfigError(
                f"{where} is not UTF-8 (byte {problem.start})",
                "save it as UTF-8",
            ) from None
        found = statements.split(text)
        if not found:
            raise ConfigError(
                f"{where} holds no SQL", "write the migration in it, or delete it"
            )
        transaction, settings = _directives(where, text)
        _check_statements(where, found, transaction)
        migrations.append(
            Migration(
                filename=entry.name,
                package=package,
                source=entry,
                text=text,
                checksum=hashlib.sha256(data).hexdigest(),
                transaction=transaction,
                settings=settings,
            )
        )
    return migrations


def _directives(where: str, text: str) -> tuple[bool, dict[str, str]]:
    """`-- pgforward:` lines, read from the comments that open the file."""
    transaction = True
    settings: dict[str, str] = {}
    heading = True
    for number, line in enumerate(text.splitlines(), start=1):
        stripped = line.strip()
        if heading and stripped and not stripped.startswith("--"):
            heading = False
        match = DIRECTIVE.match(stripped)
        if match is None:
            continue
        if not heading:
            raise ConfigError(
                f"{where} line {number}: a `-- pgforward:` line must come before "
                "the first statement",
                "move it to the top of the file",
            )
        word, _, value = match[1].strip().partition("=")
        word, value = word.strip(), value.strip()
        if word == "no-transaction" and not value:
            transaction = False
        elif word in SETTINGS and DURATION.fullmatch(value):
            settings[word] = value
        else:
            raise ConfigError(
                f"{where} line {number}: unknown or malformed directive {stripped!r}",
                "known: `-- pgforward: no-transaction`, "
                "`-- pgforward: lock-timeout=10s`, "
                "`-- pgforward: statement-timeout=10min` (0 turns a timeout off)",
            )
    return transaction, settings


def _check_statements(
    where: str, found: list[statements.Statement], transaction: bool
) -> None:
    if not transaction and len(found) > 1:
        raise ConfigError(
            f"{where}: a `-- pgforward: no-transaction` file must hold exactly one "
            f"statement; this one holds {len(found)} (lines "
            f"{', '.join(str(s.line) for s in found)})",
            "split it: one file per statement that cannot run in a transaction",
        )
    for statement in found:
        if transaction and statements.transaction_control(statement):
            raise ConfigError(
                f"{where} line {statement.line}: {' '.join(statement.words[:2])} "
                "would end the transaction pgforward runs this file in, and "
                "its ledger row with it",
                "delete it: every file already runs in its own transaction. A "
                "statement that cannot run in one goes in its own file marked "
                "`-- pgforward: no-transaction`",
            )
