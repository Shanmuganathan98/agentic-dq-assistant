"""Thin DB layer. SQLite by default; PostgreSQL when DATABASE_URL=postgresql://user:pass@host:5432/db

All SQL in this project uses '?' placeholders and portable syntax; they are translated for psycopg.
"""
import os
import re
import sqlite3

from app.tools.sql_guard import classify_sql

SCHEMA = [
    "CREATE TABLE IF NOT EXISTS runs (id TEXT PRIMARY KEY, created_at TEXT, filename TEXT, goal TEXT, "
    "status TEXT, rows_count INTEGER, report_json TEXT)",
    "CREATE TABLE IF NOT EXISTS approvals (id TEXT PRIMARY KEY, run_id TEXT, action TEXT, status TEXT, "
    "payload_json TEXT, result_json TEXT, created_at TEXT, decided_at TEXT, decided_by TEXT)",
    "CREATE TABLE IF NOT EXISTS memory_decisions (check_name TEXT PRIMARY KEY, resolution TEXT, "
    "decided_by TEXT, decided_at TEXT, times_used INTEGER)",
    "CREATE TABLE IF NOT EXISTS dq_quarantine (run_id TEXT, target_table TEXT, row_index INTEGER, "
    "reason TEXT, row_json TEXT)",
]
INTERNAL_TABLES = {"runs", "approvals", "memory_decisions", "dq_quarantine"}
_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def _url() -> str:
    return os.getenv("DATABASE_URL", "sqlite:///data/dq.db")


def is_postgres() -> bool:
    return _url().startswith(("postgres://", "postgresql"))


def connect(read_only: bool = False):
    url = _url()
    if is_postgres():
        import psycopg
        from psycopg.rows import dict_row
        conn = psycopg.connect(url, row_factory=dict_row)
        if read_only:
            conn.read_only = True                      # server enforces read-only transaction
            conn.execute("SET statement_timeout = 10000")
        return conn
    path = url.replace("sqlite:///", "", 1)
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    if read_only:
        conn.execute("PRAGMA query_only = ON")         # engine refuses any write
    return conn


def sql_for_driver(sql: str, args: tuple) -> str:
    return sql.replace("?", "%s") if (is_postgres() and args) else sql


def conn_query(conn, sql: str, params=()) -> list[dict]:
    args = tuple(params)
    final = sql_for_driver(sql, args)
    cur = conn.execute(final, args if (args or not is_postgres()) else None)
    if cur.description is None:
        return []
    return [dict(r) for r in cur.fetchall()]


def query(sql: str, params=(), read_only: bool = False) -> list[dict]:
    conn = connect(read_only)
    try:
        return conn_query(conn, sql, params)
    finally:
        conn.close()


def execute(sql: str, params=()) -> None:
    conn = connect()
    try:
        conn_query(conn, sql, params)
        conn.commit()
    finally:
        conn.close()


def init_schema() -> None:
    conn = connect()
    try:
        for stmt in SCHEMA:
            conn.execute(stmt)
        conn.commit()
    finally:
        conn.close()


# ---- agent-facing tools -------------------------------------------------------------------

def list_tables() -> list[str]:
    if is_postgres():
        rows = query("SELECT table_name AS name FROM information_schema.tables WHERE table_schema = current_schema()")
    else:
        rows = query("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")
    return sorted(r["name"] for r in rows)


def quote_ident(name: str) -> str:
    """Quote an identifier for SQL. Callers validate names first; this also escapes any embedded quote."""
    return '"' + str(name).replace('"', '""') + '"'


def conn_table_columns(conn, table: str) -> list[str]:
    """Column names of a table on an existing connection (empty list if the table does not exist)."""
    if is_postgres():
        rows = conn_query(conn, "SELECT column_name AS name FROM information_schema.columns "
                                "WHERE table_name = ? AND table_schema = current_schema() ORDER BY ordinal_position", (table,))
    else:
        rows = conn_query(conn, f"PRAGMA table_info({quote_ident(table)})")
    return [r["name"] for r in rows]


def get_table_schema(table: str) -> list[dict]:
    if not _NAME.match(table):
        raise ValueError("invalid table name")
    if is_postgres():
        rows = query("SELECT column_name AS name, data_type AS type, is_nullable AS nullable "
                     "FROM information_schema.columns WHERE table_name = ? AND table_schema = current_schema() "
                     "ORDER BY ordinal_position", (table,))
        return [{"name": r["name"], "type": r["type"], "nullable": r["nullable"] == "YES"} for r in rows]
    rows = query(f"PRAGMA table_info({quote_ident(table)})")
    return [{"name": r["name"], "type": r["type"], "nullable": not r["notnull"]} for r in rows]


def execute_select_query(sql: str, limit: int = 200) -> dict:
    """Two independent layers: the SQL guard, then a read-only connection."""
    verdict = classify_sql(sql)
    if not verdict["allowed"]:
        raise PermissionError(f"Blocked: {verdict['reason']}. Writes require the approval workflow.")
    inner = sql.strip().rstrip(";")
    rows = query(f"SELECT * FROM ({inner}) AS q LIMIT {int(limit) + 1}", read_only=True)
    truncated = len(rows) > limit
    rows = rows[:limit]
    return {"columns": list(rows[0].keys()) if rows else [], "rows": rows, "truncated": truncated}
