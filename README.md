# pgforward

Postgres migrations in plain SQL files, applied forward only, built so coding
agents get them right the first time.

    uv add pgforward
    uv run pgforward guide

## What it does

- **Plain SQL, one transaction per file.** A file and its ledger row commit
  together; a file that fails leaves nothing behind.
- **Forward only.** A database only you use is rebuilt from the files
  (`pgforward rebuild`); every other database is fixed with a new migration.
- **Knows what kind each database is** (test, branch, standing, production),
  stored in the database itself, and refuses what that kind must not do.
  Every command's first line names the database.
- **Safe defaults while migrating:** a 5s `lock_timeout` with retries, a 1min
  `statement_timeout`, an advisory lock so two runs never race, and one-line
  directives to change them per file.
- **`CREATE INDEX CONCURRENTLY`** in a `-- pgforward: no-transaction` file,
  with any invalid index left by a failure named.
- **Late merges** of older migrations are applied and reported, not skipped or
  refused.
- **schema.sql** from a fresh build of every file, rewritten on branch
  databases, so the column order is the one a new database gets.
- **Libraries ship their migrations** inside their package; the app lists them
  and every file runs in one ledger.
- **Re-run files** for views, functions and grants, run again whenever they
  change; `pgforward grants` checks what the runtime role holds.
- **`migrate --dry-run`** and **`schema --check`**: the SQL that would run, and
  how a database differs from a fresh build.
- **A test guard** (`pgforward.testing.prepare`) and a read-only `pending()`
  for health endpoints.
- `--json` output with a stable shape; exit codes 0 current, 1 pending,
  2 refused or failed. Every error says what to do next.

Nothing is silent: no fallback to another database, no ignored directive, no
skipped file.

## Status

The first version, in progress. Postgres 15+ (tested on 18), Python 3.12+,
psycopg 3.

## Development

    uv run pytest -q          # needs TEST_DATABASE_URL: a database ending _test
    uv run ruff check . && uv run ruff format --check .
    uv run ty check src

The tests create and drop their own databases beside the test database, so
the role needs CREATEDB.

## License

MIT
