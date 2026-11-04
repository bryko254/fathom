"""Turn a MySQL or Postgres dump into statements SQLite can execute.

This only rewrites session noise and identifier quoting. It does not weaken
the upload authorizer: ATTACH, DETACH, PRAGMA, and load_extension are still
refused when the statements run.
"""

from __future__ import annotations

import re
import sqlite3
import time
from pathlib import Path

_CONDITIONAL_COMMENT = re.compile(r"/\*![\s\S]*?\*/")
_SKIP_STATEMENT = re.compile(
    r"^\s*(?:"
    r"SET\b|"
    r"LOCK\s+TABLES\b|"
    r"UNLOCK\s+TABLES\b|"
    r"SELECT\s+pg_catalog\b|"
    r"GRANT\b|"
    r"REVOKE\b|"
    r"COMMENT\s+ON\b|"
    r"CREATE\s+EXTENSION\b|"
    r"DROP\s+EXTENSION\b|"
    r"CREATE\s+SEQUENCE\b|"
    r"ALTER\s+SEQUENCE\b|"
    r"CREATE\s+(?:OR\s+REPLACE\s+)?(?:FUNCTION|PROCEDURE|TRIGGER)\b|"
    r"DO\s+\$\$|"
    r"ALTER\s+TABLE\b[\s\S]*\bOWNER\s+TO\b"
    r")",
    re.I,
)
_COPY_HEADER = re.compile(
    r"^\s*COPY\s+(?P<table>.+?)\s*\((?P<cols>.+?)\)\s+FROM\s+stdin\s*;\s*$",
    re.I,
)
_PSQL_META = re.compile(r"^\s*\\")
_INDEX_CLAUSE = re.compile(r"^(?:KEY|INDEX|FULLTEXT\s+KEY|FULLTEXT|SPATIAL\s+KEY|SPATIAL)\b", re.I)


class SqlImportError(ValueError):
    """A dump could not be loaded after the MySQL and Postgres cleanup."""


def prepare_sql_dump(script: str) -> str:
    text = script.lstrip("\ufeff").replace("\r\n", "\n").replace("\r", "\n")
    text = _CONDITIONAL_COMMENT.sub("", text)
    statements: list[str] = []
    pending: list[str] = []
    copying = False
    copy_header = ""
    copy_rows: list[str] = []

    def flush_pending() -> None:
        raw = "\n".join(pending).strip()
        pending.clear()
        if not raw:
            return
        if _PSQL_META.match(raw) or _SKIP_STATEMENT.match(raw):
            return
        statements.append(_translate_statement(raw))

    for line in text.split("\n"):
        if copying:
            if line.strip() == r"\.":
                statements.append(_copy_to_inserts(copy_header, copy_rows))
                copying = False
                copy_rows = []
                continue
            copy_rows.append(line)
            continue
        if _COPY_HEADER.match(line.strip()):
            flush_pending()
            copying = True
            copy_header = line.strip()
            continue
        if _PSQL_META.match(line):
            continue
        pending.append(line)
        if _ends_statement(pending):
            flush_pending()
    flush_pending()
    if copying:
        raise SqlImportError(
            "This dump has a Postgres COPY ... FROM stdin block that never ends with \\. "
            "Close the block and upload it again."
        )
    rendered = ";\n".join(part.strip().rstrip(";") for part in statements if part.strip()) + ";"
    if not rendered.strip():
        raise SqlImportError(
            "Nothing in that dump could be loaded. Fathom keeps CREATE TABLE and INSERT "
            "statements and drops MySQL session commands and Postgres catalog queries."
        )
    return rendered


def load_sql_dump(script: str, dest: Path) -> None:
    prepared = prepare_sql_dump(script)
    if re.search(r"\bload_extension\s*\(", prepared, re.I):
        raise SqlImportError("load_extension is not allowed in an upload.")
    conn = sqlite3.connect(dest)

    def authorizer(action, arg1, arg2, db_name, source):  # noqa: ARG001
        if action in (sqlite3.SQLITE_ATTACH, sqlite3.SQLITE_DETACH, sqlite3.SQLITE_PRAGMA):
            return sqlite3.SQLITE_DENY
        return sqlite3.SQLITE_OK

    conn.set_authorizer(authorizer)
    started = time.monotonic()

    def progress() -> int:
        return 1 if time.monotonic() - started > 20 else 0

    conn.set_progress_handler(progress, 10_000)
    try:
        for statement in _split_statements(prepared):
            try:
                conn.execute(statement)
            except sqlite3.Error as exc:
                snippet = " ".join(statement.split())[:240]
                raise SqlImportError(
                    "Fathom removed MySQL and Postgres dump noise (backticks, ENGINE and CHARSET options, "
                    "AUTO_INCREMENT, SET, LOCK, UNLOCK, conditional comments, COPY blocks, and pg_catalog lines) "
                    f"and SQLite still rejected a statement: {exc}. Near: {snippet}"
                ) from exc
        conn.commit()
    finally:
        conn.close()


def _ends_statement(lines: list[str]) -> bool:
    text = "\n".join(lines)
    in_single = False
    dollar = False
    i = 0
    while i < len(text):
        if text.startswith("$$", i) and not in_single:
            dollar = not dollar
            i += 2
            continue
        ch = text[i]
        if dollar:
            i += 1
            continue
        if ch == "'":
            if in_single and text.startswith("''", i):
                i += 2
                continue
            in_single = not in_single
        elif ch == ";" and not in_single:
            return True
        i += 1
    return False


def _split_statements(script: str) -> list[str]:
    statements: list[str] = []
    buf: list[str] = []
    in_single = False
    i = 0
    while i < len(script):
        ch = script[i]
        if ch == "'":
            if in_single and script.startswith("''", i):
                buf.append("''")
                i += 2
                continue
            in_single = not in_single
            buf.append(ch)
        elif ch == ";" and not in_single:
            statement = "".join(buf).strip()
            if statement:
                statements.append(statement)
            buf = []
        else:
            buf.append(ch)
        i += 1
    tail = "".join(buf).strip()
    if tail:
        statements.append(tail)
    return statements


def _translate_statement(sql: str) -> str:
    sql = _replace_backticks(sql).strip().rstrip(";").strip()
    if not sql or _SKIP_STATEMENT.match(sql):
        return ""
    if re.match(r"CREATE\s+TABLE\b", sql, re.I):
        sql = _clean_create_table(sql)
    sql = re.sub(r"\bpublic\.", "", sql, flags=re.I)
    sql = re.sub(r"\bAUTO_INCREMENT\b", "", sql, flags=re.I)
    return sql.strip()


def _replace_backticks(sql: str) -> str:
    out: list[str] = []
    in_single = False
    i = 0
    while i < len(sql):
        ch = sql[i]
        if in_single:
            out.append(ch)
            if ch == "'" and sql.startswith("''", i):
                out.append("'")
                i += 2
                continue
            if ch == "'":
                in_single = False
            i += 1
            continue
        if ch == "'":
            in_single = True
            out.append(ch)
        elif ch == "`":
            out.append('"')
        else:
            out.append(ch)
        i += 1
    return "".join(out)


def _clean_create_table(sql: str) -> str:
    open_at = _find_column_list(sql)
    if open_at is None:
        return sql
    start, end = open_at
    body = _clean_column_list(sql[start + 1 : end])
    return f"{sql[: start + 1]}\n  {body}\n)"


def _find_column_list(sql: str) -> tuple[int, int] | None:
    start = sql.find("(")
    if start < 0:
        return None
    depth = 0
    in_single = False
    i = start
    while i < len(sql):
        ch = sql[i]
        if in_single:
            if ch == "'" and sql.startswith("''", i):
                i += 2
                continue
            if ch == "'":
                in_single = False
            i += 1
            continue
        if ch == "'":
            in_single = True
        elif ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
            if depth == 0:
                return start, i
        i += 1
    return None


def _clean_column_list(body: str) -> str:
    kept: list[str] = []
    for part in _split_depth(body):
        clause = part.strip()
        if not clause or _INDEX_CLAUSE.match(clause):
            continue
        clause = re.sub(r"\bAUTO_INCREMENT\b", "", clause, flags=re.I)
        clause = re.sub(r"\bUNSIGNED\b", "", clause, flags=re.I)
        clause = re.sub(r"\s+COMMENT\s+'(?:[^']|'')*'", "", clause, flags=re.I)
        clause = re.sub(r"\s+", " ", clause).strip().rstrip(",")
        if clause:
            kept.append(clause)
    return ",\n  ".join(kept)


def _split_depth(body: str) -> list[str]:
    parts: list[str] = []
    buf: list[str] = []
    depth = 0
    in_single = False
    i = 0
    while i < len(body):
        ch = body[i]
        if in_single:
            buf.append(ch)
            if ch == "'" and body.startswith("''", i):
                buf.append("'")
                i += 2
                continue
            if ch == "'":
                in_single = False
            i += 1
            continue
        if ch == "'":
            in_single = True
            buf.append(ch)
        elif ch == "(":
            depth += 1
            buf.append(ch)
        elif ch == ")":
            depth -= 1
            buf.append(ch)
        elif ch == "," and depth == 0:
            parts.append("".join(buf))
            buf = []
        else:
            buf.append(ch)
        i += 1
    tail = "".join(buf)
    if tail.strip():
        parts.append(tail)
    return parts


def _copy_to_inserts(header: str, rows: list[str]) -> str:
    match = _COPY_HEADER.match(header.strip())
    if not match:
        raise SqlImportError(f"Could not read a COPY header: {header[:180]}")
    table = match.group("table").strip().strip('"')
    table = re.sub(r"^public\.", "", table, flags=re.I)
    columns = [column.strip().strip('"') for column in match.group("cols").split(",")]
    quoted_cols = ", ".join(f'"{column}"' for column in columns)
    inserts: list[str] = []
    for row in rows:
        fields = row.split("\t")
        if len(fields) != len(columns):
            raise SqlImportError(
                f"A COPY row for {table} has {len(fields)} fields and the header lists {len(columns)} columns."
            )
        values = ["NULL" if field == r"\N" else "'" + field.replace("'", "''") + "'" for field in fields]
        inserts.append(f'INSERT INTO "{table}" ({quoted_cols}) VALUES ({", ".join(values)});')
    return "\n".join(inserts)
