"""Copy an external database into a SQLite file the sandbox can read.

Credentials stay in the control plane. The sandbox receives only the copy.
"""

from __future__ import annotations

import re
import sqlite3
from pathlib import Path
from typing import Any
from urllib.parse import urlparse


def redact(url: str) -> str:
    return re.sub(r"://([^:/]+):([^@]+)@", r"://\1:***@", url)


def snapshot_connection(url: str, dest: Path, *, row_cap: int) -> dict[str, Any]:
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.exists():
        dest.unlink()
    scheme = urlparse(url).scheme.lower()
    if scheme in {"postgres", "postgresql"}:
        tables = _snapshot_postgres(url, dest, row_cap)
        dialect = "postgres"
    elif scheme in {"mysql", "mariadb"}:
        tables = _snapshot_mysql(url, dest, row_cap)
        dialect = "mysql"
    elif scheme in {"sqlite", "file"} or url.startswith("sqlite:"):
        tables = _snapshot_sqlite(url, dest, row_cap)
        dialect = "sqlite"
    else:
        raise ValueError("Use a postgres://, mysql://, or sqlite:/// connection string.")
    return {"dialect": dialect, "tables": tables, "rowCap": row_cap, "redacted": redact(url)}


def _cell(value: Any) -> Any:
    if value is None or isinstance(value, (int, float, str)):
        return value
    if isinstance(value, (bytes, bytearray)):
        return value.decode("utf-8", "replace")
    return str(value)


def _safe_ident(name: str) -> str:
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name):
        raise ValueError(f"Refusing unsafe identifier {name!r}")
    return name


def _write_table(sqlite_conn: sqlite3.Connection, table: str, columns: list[str], rows: list[tuple]) -> None:
    table = _safe_ident(table)
    columns = [_safe_ident(column) for column in columns]
    col_sql = ", ".join(f'"{column}" TEXT' for column in columns)
    sqlite_conn.execute(f'CREATE TABLE "{table}" ({col_sql})')
    if not rows:
        return
    placeholders = ", ".join("?" for _ in columns)
    quoted = ", ".join(f'"{column}"' for column in columns)
    sqlite_conn.executemany(
        f'INSERT INTO "{table}" ({quoted}) VALUES ({placeholders})',
        [tuple(_cell(value) for value in row) for row in rows],
    )


def _snapshot_postgres(url: str, dest: Path, row_cap: int) -> list[dict[str, Any]]:
    import psycopg

    summary = []
    try:
        with psycopg.connect(url, connect_timeout=8) as conn:
            conn.execute("SET default_transaction_read_only = ON")
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT table_schema, table_name
                    FROM information_schema.tables
                    WHERE table_type = 'BASE TABLE'
                      AND table_schema NOT IN ('pg_catalog', 'information_schema')
                    ORDER BY 1, 2
                    """
                )
                found = cur.fetchall()
            sqlite_conn = sqlite3.connect(dest)
            try:
                for schema, table in found:
                    schema = _safe_ident(schema)
                    table = _safe_ident(table)
                    with conn.cursor() as cur:
                        cur.execute(f'SELECT * FROM "{schema}"."{table}" LIMIT {int(row_cap)}')
                        description = cur.description or []
                        columns = [item.name for item in description]
                        rows = cur.fetchall()
                    _write_table(sqlite_conn, f"{schema}_{table}" if schema != "public" else table, columns, rows)
                    summary.append({"table": table, "rows": len(rows), "capped": len(rows) >= row_cap})
                sqlite_conn.commit()
            finally:
                sqlite_conn.close()
    except Exception as exc:  # noqa: BLE001
        raise ValueError(f"Could not snapshot {redact(url)}: {exc}") from exc
    return summary


def _snapshot_mysql(url: str, dest: Path, row_cap: int) -> list[dict[str, Any]]:
    import pymysql

    parsed = urlparse(url)
    database = parsed.path.lstrip("/")
    if not database:
        raise ValueError("MySQL connection string needs a database name.")
    summary = []
    try:
        conn = pymysql.connect(
            host=parsed.hostname or "localhost",
            port=parsed.port or 3306,
            user=parsed.username,
            password=parsed.password,
            database=database,
            connect_timeout=8,
            cursorclass=pymysql.cursors.Cursor,
        )
        try:
            with conn.cursor() as cur:
                cur.execute("SET SESSION TRANSACTION READ ONLY")
                cur.execute(
                    """
                    SELECT table_name FROM information_schema.tables
                    WHERE table_schema = %s AND table_type = 'BASE TABLE'
                    ORDER BY table_name
                    """,
                    (database,),
                )
                tables = [row[0] for row in cur.fetchall()]
            sqlite_conn = sqlite3.connect(dest)
            try:
                for table in tables:
                    table = _safe_ident(table)
                    with conn.cursor() as cur:
                        cur.execute(f"SELECT * FROM `{table}` LIMIT {int(row_cap)}")
                        columns = [item[0] for item in cur.description]
                        rows = cur.fetchall()
                    _write_table(sqlite_conn, table, columns, rows)
                    summary.append({"table": table, "rows": len(rows), "capped": len(rows) >= row_cap})
                sqlite_conn.commit()
            finally:
                sqlite_conn.close()
        finally:
            conn.close()
    except Exception as exc:  # noqa: BLE001
        raise ValueError(f"Could not snapshot {redact(url)}: {exc}") from exc
    return summary


def _snapshot_sqlite(url: str, dest: Path, row_cap: int) -> list[dict[str, Any]]:
    parsed = urlparse(url)
    source = parsed.path or url.replace("sqlite://", "", 1)
    if not source:
        raise ValueError("SQLite connection string is missing a path.")
    src = sqlite3.connect(f"file:{source}?mode=ro", uri=True)
    sqlite_conn = sqlite3.connect(dest)
    summary = []
    try:
        tables = [
            row[0]
            for row in src.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")
        ]
        for table in tables:
            table = _safe_ident(table)
            cur = src.execute(f'SELECT * FROM "{table}" LIMIT {int(row_cap)}')
            columns = [item[0] for item in cur.description]
            rows = cur.fetchall()
            _write_table(sqlite_conn, table, columns, rows)
            summary.append({"table": table, "rows": len(rows), "capped": len(rows) >= row_cap})
        sqlite_conn.commit()
    finally:
        src.close()
        sqlite_conn.close()
    return summary
