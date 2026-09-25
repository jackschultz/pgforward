"""Every statement pgforward runs against a database it migrates."""

# Which database this connection reached, and the kind marked on it.
TARGET = """
SELECT current_database(), current_setting('pgforward.kind', true)
"""

# The ledger lives in public, the one schema that exists before any migration
# runs. Its first three columns are the ones every runner copied from Intake's
# kept, so a project switching to pgforward keeps its ledger.
LEDGER_EXISTS = "SELECT to_regclass('public.schema_migrations') IS NOT NULL"

ENSURE_LEDGER = """
CREATE TABLE IF NOT EXISTS public.schema_migrations (
    filename   TEXT PRIMARY KEY,
    checksum   TEXT NOT NULL,
    applied_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
ALTER TABLE public.schema_migrations
    ADD COLUMN IF NOT EXISTS package TEXT,
    ADD COLUMN IF NOT EXISTS duration_ms INTEGER,
    ADD COLUMN IF NOT EXISTS out_of_order BOOLEAN NOT NULL DEFAULT false,
    ADD COLUMN IF NOT EXISTS pgforward_version TEXT
"""

# to_jsonb reads a ledger written before pgforward's columns existed, too.
READ_LEDGER = """
SELECT to_jsonb(m)
FROM public.schema_migrations m
ORDER BY m.applied_at, m.filename
"""

RECORD = """
INSERT INTO public.schema_migrations
    (filename, checksum, package, duration_ms, out_of_order, pgforward_version)
VALUES (%s, %s, %s, %s, %s, %s)
"""

# One key for every pgforward run, and the key Intake's runner used, so a
# project mid-switch cannot run both at once.
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
