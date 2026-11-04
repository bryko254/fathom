"""Make sandbox values safe to serialize and to send to a model."""

from __future__ import annotations

import json
from datetime import date, datetime
from decimal import Decimal
from typing import Any

CELL_LIMIT = 200
SAMPLE_PAYLOAD_LIMIT = 6000
SCHEMA_PROMPT_LIMIT = 12000


def sanitize_cell(value: Any, limit: int = CELL_LIMIT) -> Any:
    if value is None or isinstance(value, (bool, int)):
        return value
    if isinstance(value, float):
        if value != value or value in (float("inf"), float("-inf")):
            return None
        return value
    if isinstance(value, (bytes, bytearray, memoryview)):
        return f"<blob {len(value)} bytes>"
    if isinstance(value, (Decimal, date, datetime)):
        value = str(value)
    elif not isinstance(value, str):
        value = str(value)
    if len(value) > limit:
        return value[:limit] + "…"
    return value


def sanitize_rows(rows: list[dict] | None, limit: int = CELL_LIMIT) -> list[dict]:
    cleaned: list[dict] = []
    for row in rows or []:
        cleaned.append({str(key): sanitize_cell(value, limit) for key, value in row.items()})
    return cleaned


def cap_sample_payload(rows: list[dict], budget: int = SAMPLE_PAYLOAD_LIMIT) -> list[dict]:
    """Keep a prefix of already-sanitized rows that fits in the model prompt."""
    kept: list[dict] = []
    used = 2
    for row in rows:
        size = len(json.dumps(row, default=str)) + 1
        if kept and used + size > budget:
            break
        kept.append(row)
        used += size
    return kept


def schema_for_prompt(tables: dict[str, dict], budget: int = SCHEMA_PROMPT_LIMIT) -> list[dict]:
    """Table names and column names only, cut off so a wide schema cannot fill the prompt."""
    items: list[dict] = []
    used = 2
    omitted = 0
    names = list(tables)
    for name in names:
        columns = [str(column) for column in (tables[name].get("columns") or [])[:32]]
        entry = {"table": name, "columns": columns}
        size = len(json.dumps(entry)) + 1
        if items and used + size > budget:
            omitted = len(names) - len(items)
            break
        items.append(entry)
        used += size
    if omitted:
        items.append({"table": f"… {omitted} more tables omitted", "columns": []})
    return items
