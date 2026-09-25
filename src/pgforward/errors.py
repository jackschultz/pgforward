class PgforwardError(Exception):
    """A refusal or failure pgforward can name, with what to do about it.

    `code` is stable and appears in `--json` output; `fix` is the next step,
    usually a command, or empty when there is nothing to run.
    """

    code = "error"

    def __init__(self, message: str, fix: str = "") -> None:
        super().__init__(message)
        self.message = message
        self.fix = fix


class ConfigError(PgforwardError):
    """The project or the command is missing something pgforward needs."""

    code = "config"


class Refused(PgforwardError):
    """The database is the wrong kind for what was asked."""

    code = "refused"


class LedgerMismatch(PgforwardError):
    """The ledger and the migration files disagree."""

    code = "ledger"


class LockTimeout(PgforwardError):
    """Another run held the migration lock for longer than pgforward waits."""

    code = "lock"


class MigrationFailed(PgforwardError):
    """A migration file failed while running."""

    code = "failed"


class SchemaDumpFailed(PgforwardError):
    """schema.sql could not be written."""

    code = "schema"
