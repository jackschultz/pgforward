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
    pgforward migrate                apply what is pending
    pgforward status                 applied and pending; exit 0 current, 1 pending, 2 problem
    pgforward rebuild                drop a test or branch database and apply every file
    pgforward mark branch            record the database's kind (test, branch, standing, production)
    pgforward schema                 rewrite schema.sql from a fresh build of every file
    pgforward guide                  this text

Every command's first line names the database and its kind. Add `--json` for
output with a stable shape (`"version": 1`). Exit code 2 means refused or
failed; the message says why and the `fix:` line what to do.

## Writing a migration

One file per change, named `YYYYMMDDHHMMSS_description.sql`, lowercase;
`pgforward new` creates it empty, and a file with no SQL in it is an error
until you write it. Files run in name order across all packages. Each
file runs in its own transaction with its ledger row, so a file that fails
leaves nothing behind: fix it and run `migrate` again.

While migrating, `lock_timeout` is 5s (a file that times out waiting for a
lock is retried, three tries in all) and `statement_timeout` is 1min. A file
changes its own at the top:

    -- pgforward: statement-timeout=20min
    -- pgforward: lock-timeout=0

`0` turns a timeout off.

A statement that cannot run in a transaction, such as
`CREATE INDEX CONCURRENTLY`, goes in a file of its own with one statement:

    -- pgforward: no-transaction
    CREATE INDEX CONCURRENTLY pings_check_received ON pings (check_id, received_at);

pgforward refuses a no-transaction file with more than one statement. If the
index build fails, it names any invalid index left behind and the
`DROP INDEX CONCURRENTLY` to run before trying again. An unknown or misplaced
`-- pgforward:` line is an error, never ignored.

## The kind of database

The kind is stored in the database itself (`pgforward mark <kind>`):

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
produces, built in a scratch database and dumped with `pg_dump`. Commit it;
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
    pgforward.migrate(url)     # what `pgforward migrate` does
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
with only the first three columns, as earlier runners wrote, is kept and
extended by the next `migrate`.
