"""Small helpers the tests share."""

import psycopg


def query(url: str, statement: str, *args):
    with psycopg.connect(url, autocommit=True) as conn:
        cur = conn.execute(statement, args or None)
        return cur.fetchall() if cur.description else []


CHECKS = """
CREATE TABLE checks (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    name text NOT NULL UNIQUE
);
"""
