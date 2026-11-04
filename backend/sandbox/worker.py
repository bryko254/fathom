#!/usr/bin/env python3
"""Fathom sandbox worker.

Reads one JSON job from stdin and writes one JSON result to stdout.
This process is the only code that runs inside the sandbox container.
It has no application imports and no network client of its own.
"""

from __future__ import annotations

import base64
import builtins
import json
import os
import signal
import sqlite3
import sys
import traceback
from io import StringIO

ALLOWED_IMPORTS = {
    "json",
    "math",
    "statistics",
    "datetime",
    "collections",
    "re",
    "itertools",
}


def main() -> None:
    raw = sys.stdin.read()
    try:
        job = json.loads(raw) if raw.strip() else {}
    except json.JSONDecodeError as exc:
        emit({"ok": False, "error": f"invalid job: {exc}"})
        return
    timeout = max(1, int(job.get("timeout_s") or 8))
    signal.signal(signal.SIGALRM, _alarm)
    signal.alarm(timeout)
    try:
        op = job.get("op")
        if op == "seed":
            result = op_seed(job)
        elif op == "sql":
            result = op_sql(job)
        elif op == "python":
            result = op_python(job)
        else:
            result = {"ok": False, "error": f"unknown op {op!r}"}
    except TimeoutError:
        result = {"ok": False, "error": "sandbox time limit exceeded", "timeout": True}
    except Exception as exc:  # noqa: BLE001 — the worker must always return JSON
        result = {"ok": False, "error": f"{type(exc).__name__}: {exc}", "trace": traceback.format_exc()[-1500:]}
    finally:
        signal.alarm(0)
    emit(result)


def _alarm(signum, frame):  # noqa: ARG001
    raise TimeoutError("sandbox time limit exceeded")


def _cell(value):
    if value is None or isinstance(value, (bool, int)):
        return value
    if isinstance(value, float):
        return None if value != value or value in (float("inf"), float("-inf")) else value
    if isinstance(value, (bytes, bytearray, memoryview)):
        return f"<blob {len(value)} bytes>"
    text = str(value)
    if len(text) > 200:
        return text[:200] + "…"
    return text


def emit(obj: dict) -> None:
    sys.stdout.write(json.dumps(obj, default=str))
    sys.stdout.write("\n")
    sys.stdout.flush()


def op_seed(job: dict) -> dict:
    path = job.get("db_path") or "/data/db.sqlite"
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    data = base64.b64decode(job["sqlite_b64"])
    with open(path, "wb") as handle:
        handle.write(data)
    os.chmod(path, 0o644)
    conn = sqlite3.connect(f"file:{path}?mode=ro&immutable=1", uri=True)
    try:
        tables = conn.execute(
            "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
        ).fetchone()[0]
    finally:
        conn.close()
    return {"ok": True, "tables": tables, "bytes": len(data)}


def op_sql(job: dict) -> dict:
    sql = str(job.get("sql") or "")
    first = sql.lstrip().split(None, 1)[0].lower() if sql.strip() else ""
    writable = bool(job.get("writable"))
    if writable:
        if first not in {"insert", "update", "delete"}:
            return {"ok": False, "error": "The sandbox only executes a confirmed INSERT, UPDATE, or DELETE."}
    elif first not in {"select", "with", "explain", "values"}:
        return {"ok": False, "error": f"The sandbox refused a non-read statement ({first or 'empty'})."}
    path = job.get("db_path") or "/data/db.sqlite"
    max_rows = int(job.get("max_rows") or 500)
    if writable:
        conn = sqlite3.connect(path)
    else:
        conn = sqlite3.connect(f"file:{path}?mode=ro&immutable=1", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        if not writable:
            conn.execute("PRAGMA query_only = ON")
        cur = conn.execute(sql)
        if writable:
            conn.commit()
            return {"ok": True, "columns": ["changes"], "rows": [{"changes": conn.total_changes}], "row_count": 1, "truncated": False}
        if cur.description is None:
            return {"ok": False, "error": "Statement returned no result set."}
        columns = [col[0] for col in cur.description]
        fetched = cur.fetchmany(max_rows + 1)
        truncated = len(fetched) > max_rows
        rows = []
        for row in fetched[:max_rows]:
            item = {}
            for key in row.keys():
                item[key] = _cell(row[key])
            rows.append(item)
        return {
            "ok": True,
            "columns": columns,
            "rows": rows,
            "row_count": len(rows),
            "truncated": truncated,
        }
    except sqlite3.Error as exc:
        return {"ok": False, "error": str(exc)}
    finally:
        conn.close()


def op_python(job: dict) -> dict:
    code = str(job.get("code") or "")
    payload = job.get("input")
    unrestricted = bool(job.get("unrestricted"))
    if unrestricted and os.environ.get("FATHOM_DRILL") != "1":
        return {"ok": False, "error": "Unrestricted Python is only available to the containment drill."}
    stdout = StringIO()
    if unrestricted:
        glb = {"__name__": "__sandbox__", "INPUT": payload}
    else:
        glb = {
            "__builtins__": _limited_builtins(stdout),
            "__name__": "__sandbox__",
            "INPUT": payload,
            "json": json,
        }
    local = {}
    previous = sys.stdout
    sys.stdout = stdout
    try:
        exec(code, glb, local)  # noqa: S102 — the container, not this dict, is the boundary
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}", "stdout": stdout.getvalue()[-4000:]}
    finally:
        sys.stdout = previous
    result = local.get("RESULT", glb.get("RESULT"))
    return {"ok": True, "stdout": stdout.getvalue()[-4000:], "result": result}


def _limited_builtins(stdout: StringIO) -> dict:
    def limited_import(name, globals=None, locals=None, fromlist=(), level=0):  # noqa: A002
        root = name.split(".", 1)[0]
        if root not in ALLOWED_IMPORTS:
            raise ImportError(f"import of {name} is not available in the sandbox")
        return builtins.__import__(name, globals, locals, fromlist, level)

    def safe_print(*args, **kwargs):
        kwargs["file"] = stdout
        builtins.print(*args, **kwargs)

    return {
        "abs": abs,
        "all": all,
        "any": any,
        "bool": bool,
        "dict": dict,
        "enumerate": enumerate,
        "float": float,
        "int": int,
        "len": len,
        "list": list,
        "max": max,
        "min": min,
        "print": safe_print,
        "range": range,
        "round": round,
        "set": set,
        "sorted": sorted,
        "str": str,
        "sum": sum,
        "tuple": tuple,
        "zip": zip,
        "map": map,
        "filter": filter,
        "isinstance": isinstance,
        "repr": repr,
        "reversed": reversed,
        "pow": pow,
        "divmod": divmod,
        "Exception": Exception,
        "ValueError": ValueError,
        "KeyError": KeyError,
        "TypeError": TypeError,
        "ZeroDivisionError": ZeroDivisionError,
        "__import__": limited_import,
        "True": True,
        "False": False,
        "None": None,
    }


if __name__ == "__main__":
    main()
