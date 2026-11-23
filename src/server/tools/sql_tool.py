"""Read-only SQL query runner for data/enterprise_ops.db.

Four independent layers, so one mistake does not open the database:

1. Static validation: one statement only, must start with SELECT or WITH, no comments,
   no write/DDL keywords outside string literals, length limit.
2. SQLite authorizer: SQLite itself reports every action while compiling the query; anything
   that is not a read, a SELECT or an allow-listed function is denied.
3. Read-only connection: the file is opened with the URI flag ``mode=ro``, so even a query
   that slipped past 1 and 2 cannot write.
4. Resource limits: progress-handler timeout and a row cap.

Errors are returned as short, generic messages: no file paths, no stack traces.
"""

from __future__ import annotations

import re
import sqlite3
import time
from pathlib import Path
from typing import Any

DB_PATH = Path(__file__).resolve().parents[3] / "data" / "enterprise_ops.db"

MAX_QUERY_CHARS = 2000
MAX_ROWS = 100
TIMEOUT_SECONDS = 2.0

FORBIDDEN_KEYWORDS = (
    "insert", "update", "delete", "drop", "alter", "create", "truncate",
    "attach", "detach", "pragma", "vacuum", "reindex", "analyze", "begin", "commit",
    "rollback", "savepoint", "release", "load_extension",
)
FORBIDDEN_RE = re.compile(r"\b(" + "|".join(FORBIDDEN_KEYWORDS) + r")\b", re.IGNORECASE)
# REPLACE(...) the string function is fine; REPLACE INTO (an upsert) is a write.
WRITE_REPLACE_RE = re.compile(r"\b(replace)\s+into\b", re.IGNORECASE)
STRING_LITERAL_RE = re.compile(r"'(?:[^']|'')*'")

ALLOWED_FUNCTIONS = {
    "count", "sum", "avg", "min", "max", "total", "round", "abs", "lower", "upper",
    "length", "substr", "trim", "coalesce", "ifnull", "nullif", "date", "strftime",
    "julianday", "instr", "replace", "cast", "printf", "group_concat",
}


class SecurityException(Exception):
    """Raised when a query is rejected by the security layer."""


def validate_query(sql: str) -> str:
    """Return a cleaned single SELECT statement or raise SecurityException."""
    if not isinstance(sql, str) or not sql.strip():
        raise SecurityException("Empty query.")
    query = sql.strip()
    if len(query) > MAX_QUERY_CHARS:
        raise SecurityException(f"Query is too long (max {MAX_QUERY_CHARS} characters).")

    # Look at the SQL with string literals blanked out, so WHERE status = 'update pending'
    # is allowed but real keywords and separators are still caught.
    code = STRING_LITERAL_RE.sub("''", query).rstrip().rstrip(";").rstrip()
    if ";" in code:
        raise SecurityException("Only one statement is allowed (stacked queries are blocked).")
    if "--" in code or "/*" in code or "*/" in code:
        raise SecurityException("SQL comments are not allowed.")
    if not re.match(r"^\s*(select|with)\b", code, re.IGNORECASE):
        raise SecurityException("Only SELECT queries are allowed.")
    bad = FORBIDDEN_RE.search(code) or WRITE_REPLACE_RE.search(code)
    if bad:
        raise SecurityException(f"Keyword '{bad.group(1).upper()}' is not allowed in a read-only query.")
    return query.rstrip().rstrip(";").rstrip()


def _authorizer(action: int, arg1: Any, arg2: Any, db_name: Any, trigger: Any) -> int:
    """SQLite calls this for every operation while compiling the statement."""
    if action in (sqlite3.SQLITE_SELECT, sqlite3.SQLITE_READ):
        return sqlite3.SQLITE_OK
    if action == getattr(sqlite3, "SQLITE_RECURSIVE", 33):  # WITH RECURSIVE
        return sqlite3.SQLITE_OK
    if action == sqlite3.SQLITE_FUNCTION and str(arg2).lower() in ALLOWED_FUNCTIONS:
        return sqlite3.SQLITE_OK
    return sqlite3.SQLITE_DENY


def connect_read_only(db_path: Path = DB_PATH) -> sqlite3.Connection:
    if not db_path.exists():
        raise SecurityException("Database is not available.")
    con = sqlite3.connect(f"{db_path.resolve().as_uri()}?mode=ro", uri=True, timeout=TIMEOUT_SECONDS)
    con.set_authorizer(_authorizer)
    return con


def execute_read_only(sql: str, db_path: Path = DB_PATH, validate: bool = True) -> dict:
    """Run one SELECT and return {columns, rows, row_count, truncated}.

    ``validate=False`` exists only so the tests can prove layers 2 and 3 hold on their own.
    """
    query = validate_query(sql) if validate else sql
    con = connect_read_only(db_path)
    deadline = time.monotonic() + TIMEOUT_SECONDS
    con.set_progress_handler(lambda: 1 if time.monotonic() > deadline else 0, 10_000)
    try:
        cur = con.execute(query)
        columns = [d[0] for d in cur.description or []]
        rows = cur.fetchmany(MAX_ROWS + 1)
    except sqlite3.DatabaseError as exc:
        msg = str(exc)
        if "not authorized" in msg or "readonly" in msg:
            raise SecurityException("Blocked by the database guard: only reading is allowed.") from None
        if "interrupted" in msg:
            raise SecurityException(f"Query took longer than {TIMEOUT_SECONDS:.0f} s and was stopped.") from None
        raise ValueError(f"SQL error: {msg}") from None
    finally:
        con.close()
    truncated = len(rows) > MAX_ROWS
    rows = [list(r) for r in rows[:MAX_ROWS]]
    return {"columns": columns, "rows": rows, "row_count": len(rows), "truncated": truncated}


def describe_schema(db_path: Path = DB_PATH) -> str:
    """Short schema text for the tool description, e.g. 'orders(order_id, ...)'."""
    con = sqlite3.connect(f"{db_path.resolve().as_uri()}?mode=ro", uri=True)
    try:
        tables = [r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")]
        parts = []
        for t in tables:
            cols = [r[1] for r in con.execute(f"PRAGMA table_info({t})")]
            parts.append(f"{t}({', '.join(cols)})")
        return "; ".join(parts)
    finally:
        con.close()
