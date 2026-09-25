"""Where pgforward's settings come from: the environment and pyproject.toml.

The database address is `--url`, else `MIGRATION_DATABASE_URL`, else
`DATABASE_URL`. pgforward never reads `.env` itself and never falls back from
an empty value to another one: a command that could land on the wrong
database refuses instead.
"""

import dataclasses
import os
import pathlib
import tomllib

from pgforward.errors import ConfigError

ENV_FILE_HINT = "uv run --env-file .env pgforward <command>"


@dataclasses.dataclass(frozen=True)
class Project:
    root: pathlib.Path
    packages: tuple[str, ...]

    @property
    def schema_file(self) -> pathlib.Path:
        return self.root / "schema.sql"


def database_url(explicit: str | None = None) -> str:
    if explicit is not None:
        if not explicit.strip():
            raise ConfigError(
                "--url is empty",
                "pass the database address, or leave --url out to use "
                "MIGRATION_DATABASE_URL or DATABASE_URL",
            )
        return explicit
    for name in ("MIGRATION_DATABASE_URL", "DATABASE_URL"):
        value = os.environ.get(name)
        if value is None:
            continue
        if not value.strip():
            raise ConfigError(
                f"{name} is set but empty",
                f"set {name} to the database address, or unset it",
            )
        return value
    raise ConfigError(
        "no database address: MIGRATION_DATABASE_URL and DATABASE_URL are unset",
        f"if the project keeps them in .env, run `{ENV_FILE_HINT}`",
    )


def project(start: pathlib.Path | None = None) -> Project:
    """The nearest pyproject.toml above `start` that has [tool.pgforward]."""
    here = (start or pathlib.Path.cwd()).resolve()
    for folder in (here, *here.parents):
        path = folder / "pyproject.toml"
        if not path.is_file():
            continue
        settings = tomllib.loads(path.read_text()).get("tool", {}).get("pgforward")
        if settings is None:
            continue
        packages = settings.get("packages")
        if (
            not isinstance(packages, list)
            or not packages
            or not all(isinstance(p, str) and p for p in packages)
        ):
            raise ConfigError(
                f"{path}: [tool.pgforward] packages must be a list of package "
                "names, the app's own first",
                'packages = ["myapp"]',
            )
        return Project(folder, tuple(packages))
    raise ConfigError(
        f"no pyproject.toml with [tool.pgforward] at or above {here}",
        'add to pyproject.toml:\n\n    [tool.pgforward]\n    packages = ["myapp"]'
        "\n\nand put migrations in src/myapp/migrations/",
    )
