"""Bringing a test database up to date, with the guard that keeps tests off
every other database.

    import os, pgforward.testing

    @pytest.fixture(scope="session")
    def database_url():
        url = os.environ["TEST_DATABASE_URL"]
        pgforward.testing.prepare(url)
        return url
"""

import os
from collections.abc import Sequence

from pgforward import apply, config, database, files, ledger
from pgforward.errors import Refused


def prepare(url: str, packages: Sequence[str] | None = None) -> list[str]:
    """Migrate the test database; returns the files applied.

    Refuses unless the database's name ends in `_test`, it is not the database
    `DATABASE_URL` names, and it is marked test or not marked at all (an
    unmarked one is marked test here). A test database is disposable, so when
    its ledger disagrees with the files, or a file would run out of order, it
    is rebuilt from nothing rather than refused.
    """
    if not url or not url.strip():
        raise Refused(
            "the test database address is empty",
            "set TEST_DATABASE_URL to a database whose name ends in _test",
        )
    development = os.environ.get("DATABASE_URL")
    if development and database.same_database(url, development):
        raise Refused(
            "the test database is the same database as DATABASE_URL; tests may "
            "empty it",
            "point TEST_DATABASE_URL at its own database, named <name>_test",
        )
    names = tuple(packages) if packages is not None else config.project().packages
    with database.connect(url) as conn:
        target = database.target(conn)
    if not target.name.endswith("_test"):
        raise Refused(
            f"{target.describe()}: a test database's name ends in _test",
            "create one (for example createdb <name>_test) and point "
            "TEST_DATABASE_URL at it",
        )
    if target.marked is None:
        database.mark(url, "test")
    elif target.marked != "test":
        raise Refused(
            f"{target.describe()} is marked {target.marked}, not test",
            "point TEST_DATABASE_URL at a test database",
        )
    with database.connect(url) as conn:
        status = ledger.status(conn, files.find(names))
    if status.problems or any(p.out_of_order for p in status.pending):
        database.recreate(url)
    return [ran.filename for ran in apply.migrate(url, names).applied]
