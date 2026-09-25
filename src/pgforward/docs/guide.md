# pgforward guide

Postgres migrations as plain SQL files, applied forward only. There are no
down migrations: a database that only you use is rebuilt from the files, and
every other database is fixed with a new migration.

## Set up

    uv add pgforward

In pyproject.toml, name the packages whose migrations run, your app first:

    [tool.pgforward]
    packages = ["myapp"]            # add libraries that ship tables: "rota", ...

Migrations live inside each package, in `src/myapp/migrations/`, so an
installed app finds them. A library does the same in its own package.

The database is `--url`, else `MIGRATION_DATABASE_URL`, else `DATABASE_URL`.
pgforward does not read `.env`; run it as

    uv run --env-file .env pgforward <command>

An empty or missing address is an error, never a fallback to another one.

## Commands

    pgforward new add_last_ping_at   create src/myapp/migrations/<timestamp>_add_last_ping_at.sql
    pgforward migrate                apply what is pending; on a branch database, rewrite schema.sql
    pgforward status                 applied and pending; exit 0 current, 1 pending, 2 problem
    pgforward rebuild                drop a test or branch database and apply every file
    pgforward mark branch            record the database's kind (test, branch, standing, production)
    pgforward schema                 rewrite schema.sql from a fresh build of every file
    pgforward guide                  this text

Every command's first line names the database and its kind. Add `--json` for
output with a stable shape (`"version": 1`). Exit code 2 means refused or
failed; the message says why and the `fix:` line what to do. Exit code 3 is
a bug in pgforward, with its traceback.

## Writing a migration

One file per change, named `YYYYMMDDHHMMSS_description.sql`, lowercase;
`pgforward new` creates it empty, and a file with no SQL in it is an error
until you write it. Files run in name order across all packages. Each
file runs in its own transaction with its ledger row, so a file that fails
leaves nothing behind: fix it and run `migrate` again. Do not write BEGIN,
COMMIT or ROLLBACK in a file: pgforward refuses them, since they would end
its transaction. Each file starts with default settings, so a `SET` in one
file does not carry into the next.

While migrating, `lock_timeout` is 5s (a file that times out waiting for a
lock is retried, three tries in all) and `statement_timeout` is 1min. A file
changes its own at the top:

    -- pgforward: statement-timeout=20min
    -- pgforward: lock-timeout=0

`0` turns a timeout off.

A statement that cannot run in a transaction, such as
`CREATE INDEX CONCURRENTLY`, goes in a file of its own with one statement:

    -- pgforward: no-transaction
    CREATE INDEX CONCURRENTLY IF NOT EXISTS pings_check_received
        ON pings (check_id, received_at);

A no-transaction file has no timeouts unless it sets them: a concurrent
index build blocks no reads or writes, but it waits for every older
transaction in the database, and a short `lock_timeout` would fail it
whenever any query runs long.

pgforward refuses a no-transaction file with more than one statement, and
refuses to run one while the database holds an invalid index (one a failed
concurrent build left half made, which `IF NOT EXISTS` would otherwise skip):
it names the `DROP INDEX CONCURRENTLY` to run first. `IF NOT EXISTS` makes the
file safe to run again when a run was stopped after the index was built but
before it was recorded. An unknown or misplaced `-- pgforward:` line, a file
named like a migration but not quite (`.SQL`), and a file with no SQL are
errors, never skipped.

## The kind of database

The kind is stored in the database itself (`pgforward mark <kind>`). A
session, role or server setting of `pgforward.kind` does not count; pgforward
refuses when one disagrees with the database's own mark.

- `test`: tests may empty it. Its name ends in `_test`.
- `branch`: one checkout's development database. `rebuild` may drop it, and
  `migrate` rewrites schema.sql on it.
- `standing`: the copy someone uses. Never rebuilt. An unmarked database is
  treated as standing.
- `production`: never rebuilt, and no scratch database is made on its server.

A database marked standing or production is never made disposable by `mark`.

## Changing a migration you already applied

If only your branch and test databases ran it, edit the file, then

    pgforward rebuild

The ledger stores each file's checksum, so an edited applied file is refused
until then. On a standing or production database an applied file is never
edited: restore it and put the change in a new migration.

## A teammate's older migration

A file older than ones already applied (a branch merged after your later work
ran) is applied and reported as out of order, and recorded so in the ledger.
Its columns land after yours, where a fresh build puts them before: on a
branch database, `pgforward rebuild` makes the two match.

## schema.sql

After `migrate` applies anything on a branch database, pgforward rewrites
`schema.sql` beside pyproject.toml: the schema a fresh build of every file
produces, dumped with `pg_dump`. The fresh build happens in a scratch
database, `pgforward_scratch_<random>`, which pgforward creates on the same
server and drops again, and says so in its output. `rebuild` and `schema` do
the same; nothing else creates a database. Commit it;
read it instead of replaying the migrations. It needs `pg_dump` at least as new
as the server, and a role that may create databases.

## Tests

    import os
    import pgforward.testing
    import pytest

    @pytest.fixture(scope="session")
    def database_url():
        url = os.environ["TEST_DATABASE_URL"]
        pgforward.testing.prepare(url)
        return url

`prepare` refuses unless the database's name ends in `_test`, it is not the
database `DATABASE_URL` names, and it is marked test (an unmarked one is
marked test). It migrates the database, and rebuilds it first when an applied
file changed or a file would run out of order.

## From the application

    import pgforward
    pgforward.pending(url)     # file names not yet applied; read-only
    pgforward.migrate(url)     # apply what is pending (schema.sql only when
                               # given schema_file=, as the command does)
    pgforward.status(url)      # applied, pending, problems

A health endpoint reports `pending(url)` and answers unhealthy while it is
not empty. `packages` defaults to pyproject.toml's list; pass it when the
app runs where pyproject.toml is not.

## Production

Run migrations as the role that owns the schema: set `MIGRATION_DATABASE_URL`
for the deploy, and give running processes only `DATABASE_URL` with the
runtime role. Mark the database once: `pgforward mark production`.

Two runs never apply at once: each takes an advisory lock, waits up to 60s
for another run, and names the process holding it if it gives up.

Large data changes that would hold locks for long, or pass the statement
timeout, are batched jobs, not migrations.

## The ledger

`public.schema_migrations`: filename, checksum (SHA-256 of the file),
applied_at, package, duration_ms, out_of_order, pgforward_version. A ledger
an earlier runner wrote is kept and extended by the next `migrate`; rows
with no checksum get one from the files on disk, once, and it says so. A
ledger whose columns have other names is refused, with the `ALTER TABLE` that
renames them.
