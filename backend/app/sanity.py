from __future__ import annotations

from app.sandbox import ExecResult

ID_KEYS = ("account_id", "customer_id", "id")


def assess(sql: str, result: ExecResult) -> list[str]:
    """Look at a result the sandbox actually returned. Do not invent rows."""
    if not result.ok:
        return []
    warnings: list[str] = []
    rows = result.rows
    if result.truncated:
        warnings.append(
            f"Result hit the row cap ({result.row_count} rows kept) and was truncated. Aggregate before using it."
        )
    if not rows:
        warnings.append("Query returned zero rows. A status literal, date window, or column filter is likely wrong.")
        return warnings
    for key in ID_KEYS:
        if key not in rows[0]:
            continue
        distinct = len({row.get(key) for row in rows})
        if len(rows) >= 20 and distinct and len(rows) / distinct >= 5:
            warnings.append(
                f"Join fanout: {len(rows)} rows for {distinct} distinct {key} "
                f"(about {len(rows) / distinct:.0f} rows per key). Aggregate to one row per {key.replace('_', ' ')}."
            )
            break
    for column in result.columns:
        nulls = sum(1 for row in rows if row.get(column) is None)
        if len(rows) >= 10 and nulls / len(rows) >= 0.85:
            warnings.append(f"Column {column} is null in {nulls} of {len(rows)} rows.")
    lowered = f" {sql.lower()} "
    if result.truncated and " join " in lowered:
        if not any("fanout" in item for item in warnings):
            warnings.append("A join was truncated. That often means the join multiplied rows. Aggregate and run it again.")
    return warnings
