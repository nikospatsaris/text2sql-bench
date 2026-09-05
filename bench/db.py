"""SQLite access.

Two connection modes matter here. The loader needs to write; the benchmark must
not be able to. Candidate SQL comes from a language model, so it is executed
through a read-only connection with a statement timeout -- a model that emits
DROP TABLE should fail its case, not damage the database.
"""

import os
import sqlite3

SCHEMA_PATH = os.path.join(os.path.dirname(__file__), "schema.sql")
DEFAULT_DB = os.path.join(os.path.dirname(os.path.dirname(__file__)), "data", "worldbank.sqlite")


def connect(path=DEFAULT_DB):
    if path != ":memory:":
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    conn = sqlite3.connect(path, timeout=30.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def connect_readonly(path=DEFAULT_DB, timeout_ms=5000):
    """A connection that cannot write and gives up on runaway queries.

    Opened through the file: URI so SQLite itself enforces read-only, rather than
    trusting a check on the query text -- a model can always find a phrasing that
    a string check misses.
    """
    uri = "file:%s?mode=ro" % os.path.abspath(path).replace("\\", "/")
    conn = sqlite3.connect(uri, uri=True, timeout=5.0)
    conn.row_factory = sqlite3.Row

    deadline = [timeout_ms]

    def _interrupt_after_budget():
        # progress_handler fires every N bytecode steps; returning non-zero aborts.
        deadline[0] -= 1
        return 1 if deadline[0] <= 0 else 0

    conn.set_progress_handler(_interrupt_after_budget, 10000)
    return conn


def init(conn):
    with open(SCHEMA_PATH, encoding="utf-8") as handle:
        conn.executescript(handle.read())
    conn.commit()
    return conn


def counts(conn):
    tables = ["regions", "income_levels", "economies", "indicators", "observations"]
    return {t: conn.execute("SELECT COUNT(*) FROM %s" % t).fetchone()[0] for t in tables}


def reference_values(conn):
    """The indicator ids that actually exist, for the model prompt.

    Without this the model has to invent an identifier string from memory, and
    it reaches for well-known ones that the World Bank has since retired --
    EN.ATM.CO2E.PC is the case that caught this. Listing the enumerable key
    values of a small dimension table is ordinary schema context, not a hint.
    """
    rows = conn.execute(
        "SELECT indicator_id, name FROM indicators ORDER BY indicator_id").fetchall()
    if not rows:
        return ""
    return "\n".join("  %s = %s" % (r[0], r[1]) for r in rows)


def schema_text(conn):
    """The CREATE statements, for putting in a model prompt."""
    rows = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND sql IS NOT NULL "
        "AND name NOT LIKE 'sqlite_%' ORDER BY name").fetchall()
    return "\n\n".join(r[0].strip() for r in rows)
