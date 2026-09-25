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
LEDGER_COLUMNS = """
SELECT column_name::text
FROM information_schema.columns
WHERE table_schema = 'public' AND table_name = 'schema_migrations'
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

SERVER_VERSION = "SELECT current_setting('server_version_num')::int / 10000"
