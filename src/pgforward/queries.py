"""Every statement pgforward runs against a database it migrates."""

# Which database this connection reached, the kind marked on the database
# itself, and the kind this session sees. They differ when a connection
# option, PGOPTIONS, a role or the server sets pgforward.kind: only the
# database's own mark counts.
TARGET = """
SELECT current_database(),
       (SELECT substr(s, length('pgforward.kind=') + 1)
        FROM pg_db_role_setting d, unnest(d.setconfig) AS s
        WHERE d.setdatabase = (SELECT oid FROM pg_database
                               WHERE datname = current_database())
          AND d.setrole = 0
          AND s LIKE 'pgforward.kind=%'),
       current_setting('pgforward.kind', true)
"""

# The ledger lives in public, the one schema that exists before any migration
# runs. Its first three columns are the ones every runner copied from Intake's
# kept, so a project switching to pgforward keeps its ledger.
# From the catalog, not information_schema, which hides the columns of a
# table the role cannot read and would make the ledger look absent.
LEDGER_COLUMNS = """
SELECT attname::text
FROM pg_attribute
WHERE attrelid = to_regclass('public.schema_migrations')
  AND attnum > 0
  AND NOT attisdropped
"""

LEDGER_READABLE = """
SELECT has_table_privilege('public.schema_migrations', 'SELECT'), current_user
"""

ENSURE_LEDGER = """
CREATE TABLE IF NOT EXISTS public.schema_migrations (
    filename   TEXT PRIMARY KEY,
    checksum   TEXT NOT NULL,
    applied_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
ALTER TABLE public.schema_migrations
    ADD COLUMN IF NOT EXISTS checksum TEXT,
    ADD COLUMN IF NOT EXISTS package TEXT,
    ADD COLUMN IF NOT EXISTS duration_ms INTEGER,
    ADD COLUMN IF NOT EXISTS out_of_order BOOLEAN NOT NULL DEFAULT false,
    ADD COLUMN IF NOT EXISTS pgforward_version TEXT
"""

# to_jsonb reads a ledger written before pgforward's columns existed, too,
# including one with no applied_at.
READ_LEDGER = """
SELECT to_jsonb(m)
FROM public.schema_migrations m
ORDER BY to_jsonb(m) ->> 'applied_at', m.filename
"""

RECORD = """
INSERT INTO public.schema_migrations
    (filename, checksum, package, duration_ms, out_of_order, pgforward_version)
VALUES (%s, %s, %s, %s, %s, %s)
"""

# Re-run files: which version of each last ran. Its own table, since a re-run
# file runs many times and a migration once.
RERUNS_EXIST = "SELECT to_regclass('public.schema_reruns') IS NOT NULL"

ENSURE_RERUNS = """
CREATE TABLE IF NOT EXISTS public.schema_reruns (
    package    TEXT NOT NULL,
    filename   TEXT NOT NULL,
    checksum   TEXT NOT NULL,
    applied_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (package, filename)
)
"""

READ_RERUNS = "SELECT package, filename, checksum FROM public.schema_reruns"

RECORD_RERUN = """
INSERT INTO public.schema_reruns (package, filename, checksum)
VALUES (%s, %s, %s)
ON CONFLICT (package, filename)
DO UPDATE SET checksum = EXCLUDED.checksum, applied_at = NOW()
"""

# One key for every pgforward run. It is also the key Intake's runner took, so
# Intake cannot run both at once while it switches; the other runners used
# other keys.
LOCK_KEY = 7_425_318_601
TRY_LOCK = "SELECT pg_try_advisory_lock(%s)"
UNLOCK = "SELECT pg_advisory_unlock(%s)"

# Who holds the lock. A bigint advisory key is stored split into classid (high
# 32 bits) and objid (low 32 bits).
LOCK_HOLDER = """
SELECT a.pid,
       coalesce(nullif(a.application_name, ''), 'unnamed client'),
       round(extract(epoch FROM now() - coalesce(a.xact_start, a.backend_start)))::int,
       a.state
FROM pg_locks l
JOIN pg_stat_activity a USING (pid)
WHERE l.locktype = 'advisory'
  AND l.granted
  AND l.classid = %s::bigint >> 32
  AND l.objid = %s::bigint & 4294967295
  AND l.objsubid = 1
  AND l.database = (SELECT oid FROM pg_database WHERE datname = current_database())
"""

# A ledger an earlier runner wrote without checksums: record them from the
# files once, as databases.md allows a first checksum-aware run to do.
BASELINE = """
UPDATE public.schema_migrations SET checksum = %s
WHERE filename = %s AND checksum IS NULL
"""

SET = "SELECT set_config(%s, %s, false)"

# Left behind by a CREATE INDEX CONCURRENTLY that failed partway.
INVALID_INDEXES = """
SELECT i.indexrelid::regclass::text
FROM pg_index i
WHERE NOT i.indisvalid
ORDER BY 1
"""

# The application's tables: every table outside the system schemas except
# pgforward's own two, with what a role may do on each.
TABLE_PRIVILEGES = """
SELECT format('%%I.%%I', n.nspname, c.relname),
       has_table_privilege(%s, c.oid, 'SELECT'),
       has_table_privilege(%s, c.oid, 'INSERT'),
       has_table_privilege(%s, c.oid, 'UPDATE'),
       has_table_privilege(%s, c.oid, 'DELETE')
FROM pg_class c
JOIN pg_namespace n ON n.oid = c.relnamespace
WHERE c.relkind IN ('r', 'p', 'v', 'm')
  AND n.nspname NOT IN ('pg_catalog', 'information_schema')
  AND n.nspname NOT LIKE 'pg\\_%%'
  AND (n.nspname, c.relname) NOT IN
      (('public', 'schema_migrations'), ('public', 'schema_reruns'))
ORDER BY 1
"""

SCHEMAS_WITHOUT_USAGE = """
SELECT DISTINCT n.nspname::text
FROM pg_class c
JOIN pg_namespace n ON n.oid = c.relnamespace
WHERE c.relkind IN ('r', 'p', 'v', 'm')
  AND n.nspname NOT IN ('pg_catalog', 'information_schema')
  AND n.nspname NOT LIKE 'pg\\_%%'
  AND NOT has_schema_privilege(%s, n.oid, 'USAGE')
ORDER BY 1
"""

ROLE_EXISTS = "SELECT EXISTS (SELECT FROM pg_roles WHERE rolname = %s)"

SERVER_VERSION = "SELECT current_setting('server_version_num')::int / 10000"

# Which database this is, as the server reports it: two addresses that spell
# the same server differently still give the same answer.
IDENTITY = """
SELECT (SELECT system_identifier FROM pg_control_system())::text,
       current_database()
"""

# Taken on the server's postgres database, keyed by the name of the database
# about to be dropped, so two runs cannot drop it under each other.
TRY_DATABASE_LOCK = "SELECT pg_try_advisory_lock(hashtext(%s))"
DATABASE_UNLOCK = "SELECT pg_advisory_unlock(hashtext(%s))"
