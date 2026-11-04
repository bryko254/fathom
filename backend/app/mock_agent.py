"""Fallback analyst used when no inference key is configured.

The policy is scripted. The work is not: every explore, SQL statement, and
Python check is executed by the sandbox, and the answer is filled from the
rows that came back. The first statement on each scripted question is a
mistake a person actually makes (wrong column, wrong literal, or a join that
multiplies rows). The next statement is written because the sandbox said so.
"""

from __future__ import annotations

import re

import sqlite3
import uuid
from collections.abc import Iterator
from datetime import date
from pathlib import Path

from app.clock import LEDGER_TODAY, calendar_year, previous_month, previous_quarter
from app.sandbox import SandboxRunner
from app.sanity import assess

DEMO_SAMPLE_IDS = frozenset({"harbor", "northline", "pesaledger"})

DOMAIN_HINTS = {
    "auth": "sign-in, users, and permissions",
    "account": "accounts and identity",
    "admin": "Django admin",
    "django": "Django framework bookkeeping",
    "session": "login sessions",
    "therapist": "therapist profiles and clinical records",
    "moodtracker": "mood tracking",
    "mood": "mood tracking",
    "payment": "payments and billing",
    "billing": "payments and billing",
    "order": "orders",
    "subscription": "subscriptions",
    "customer": "customers",
}

FANOUT_CODE = """
rows = INPUT["rows"]
ids = [r.get("account_id") for r in rows]
uniq = len({i for i in ids if i is not None})
ratio = (len(rows) / uniq) if uniq else 0
RESULT = {"rows": len(rows), "distinctAccounts": uniq, "rowsPerAccount": round(ratio, 1), "fanout": ratio >= 5}
print("rows=%s distinct_accounts=%s rows_per_account=%.1f" % (len(rows), uniq, ratio))
"""

TOTAL_CODE = """
rows = INPUT["rows"]
accounts = sum(int(r["accounts"]) for r in rows)
mrr = round(sum(float(r["mrr_lost"]) for r in rows), 2)
top = max(rows, key=lambda r: int(r["accounts"]))
RESULT = {"accounts": accounts, "mrrLost": mrr, "topReason": top["cancel_reason"], "topAccounts": int(top["accounts"])}
print("accounts=%s mrr_lost=%s top=%s" % (accounts, mrr, top["cancel_reason"]))
"""

FEE_CODE = """
rows = INPUT["rows"]
total = round(sum(float(r["fee_kes"]) for r in rows), 2)
RESULT = {"feeKes": total, "types": len(rows)}
print("fee_kes=%s" % total)
"""


class MockAnalyst:
    def __init__(self, *, runner: SandboxRunner, sample_id: str | None, as_of: date = LEDGER_TODAY) -> None:
        self.runner = runner
        self.sample_id = sample_id
        self.as_of = as_of
        self.tables: dict[str, dict] = {}
        self.queries: list[dict] = []

    def run(self, question: str) -> Iterator[dict]:
        yield from self._explore()
        if not self.tables:
            yield _event("error", "Could not read the schema.")
            return
        route = _route(question, set(self.tables), self.sample_id)
        if route == "write":
            yield from self._write(question)
            return
        if self.sample_id in DEMO_SAMPLE_IDS:
            yield from self._containment()
        if route == "overview":
            yield from self._overview(question)
        elif route == "saas_churn":
            yield from self._saas_churn()
        elif route == "ecom_sales":
            yield from self._ecom_sales()
        elif route == "pesa":
            yield from self._pesa()
        else:
            yield from self._generic(question)

    def _containment(self) -> Iterator[dict]:
        """One destructive SQL statement and one escape attempt, both meant to fail closed."""
        drop_sql = "DROP TABLE accounts"
        step_id = _sid()
        yield _step(
            step_id,
            "guard",
            "Containment: try DROP TABLE",
            "running",
            sql=drop_sql,
            detail="The analyst emits a destructive statement. The read-only guard runs before the sandbox.",
        )
        dropped = self.runner.run_sql(drop_sql)
        drop_blocked = bool(dropped.blocked and not dropped.ok)
        yield _step(
            step_id,
            "guard",
            "Containment: try DROP TABLE",
            "blocked" if drop_blocked else "error",
            sql=drop_sql,
            detail=(
                "Blocked by the read-only guard. This statement never entered the sandbox, and no table was dropped."
                if drop_blocked
                else (dropped.error or "The destructive statement was not blocked.")
            ),
            error=None if drop_blocked else dropped.error,
            elapsedMs=dropped.elapsed_ms,
        )
        marker = Path(f"/tmp/fathom-host-marker-{uuid.uuid4().hex}")
        marker.write_text("host-only-secret")
        code = (
            "import os, socket\n"
            "print('KEY', repr(os.environ.get('LLM_API_KEY')))\n"
            "try:\n"
            f"    print('FILE', open({str(marker)!r}).read())\n"
            "except Exception as exc:\n"
            "    print('FILE_ERR', type(exc).__name__)\n"
            "try:\n"
            "    sock = socket.socket()\n"
            "    sock.settimeout(2)\n"
            "    sock.connect(('1.1.1.1', 80))\n"
            "    print('NET connected')\n"
            "except Exception as exc:\n"
            "    print('NET_ERR', type(exc).__name__)\n"
        )
        step_id = _sid()
        yield _step(
            step_id,
            "python",
            "Containment: read the host and call out",
            "running",
            code=code,
            detail="A new container tries to read a host-only file, the inference key, and 1.1.1.1. It is destroyed when this step returns.",
        )
        try:
            probed = self.runner.run_drill_python(code, timeout_s=4)
        finally:
            marker.unlink(missing_ok=True)
        stdout = probed.stdout or ""
        contained = (
            "KEY None" in stdout
            and "FILE_ERR" in stdout
            and "host-only-secret" not in stdout
            and "NET_ERR" in stdout
            and "NET connected" not in stdout
        )
        if contained:
            detail = (
                "The container had no LLM_API_KEY, could not read the host marker, and could not open a socket. "
                "It was removed after the step."
            )
        else:
            detail = (stdout or probed.error or "The escape was not fully contained.").strip()
        yield _step(
            step_id,
            "python",
            "Containment: read the host and call out",
            "blocked" if contained else "error",
            code=code,
            detail=detail,
            stdout=stdout,
            error=None if contained else (probed.error or "Escape was not contained."),
            elapsedMs=probed.elapsed_ms,
        )

    def _pragma_columns(self) -> dict[str, list[str]]:
        """Read real column names from the database via pragma_table_info (paged)."""
        out: dict[str, list[str]] = {}
        page = 400
        for offset in range(0, 20000, page):
            try:
                res = self.runner.run_sql(
                    "SELECT m.name AS tbl, p.name AS col FROM sqlite_master AS m "
                    "JOIN pragma_table_info(m.name) AS p "
                    "WHERE m.type = 'table' AND m.name NOT LIKE 'sqlite_%' "
                    f"ORDER BY m.name, p.cid LIMIT {page} OFFSET {offset}"
                )
            except Exception:  # noqa: BLE001
                break
            if not res.ok:
                break
            for r in res.rows:
                out.setdefault(r["tbl"], []).append(r["col"])
            if len(res.rows) < page:
                break
        return out

    def _explore(self) -> Iterator[dict]:
        step_id = _sid()
        yield _step(step_id, "explore", "Explore the schema", "running", detail="Reading sqlite_master.")
        result = self.runner.run_sql(
            "SELECT name, sql FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
        )
        if not result.ok:
            yield _step(step_id, "explore", "Explore the schema", "error", error=result.error, elapsedMs=result.elapsed_ms)
            return
        import sqlglot
        from sqlglot import exp

        live = self._pragma_columns()
        for row in result.rows:
            columns: list[str] = list(live.get(row["name"], []))
            if not columns:
                try:
                    tree = sqlglot.parse_one(row["sql"], read="sqlite")
                    columns = [node.name for node in tree.find_all(exp.ColumnDef)]
                except Exception:
                    columns = []
            if not columns:
                columns = parse_create_columns(row["sql"] or "")
            self.tables[row["name"]] = {"sql": row["sql"], "columns": columns}
        shown = list(self.tables)
        preview = ", ".join(shown[:12])
        if len(shown) > 12:
            preview = f"{preview}, and {len(shown) - 12} more"
        yield _step(
            step_id,
            "explore",
            "Explore the schema",
            "ok",
            detail=f"Found {len(self.tables)} tables: {preview}.",
            columns=["table", "columns"],
            rows=[
                {"table": name, "columns": ", ".join(info["columns"]) or "(no parsed columns)"}
                for name, info in self.tables.items()
            ],
            elapsedMs=result.elapsed_ms,
        )
        sample_tables = [name for name in ("subscriptions", "orders", "transactions", "accounts", "customers") if name in self.tables][:2]
        if not sample_tables:
            sample_tables = list(self.tables)[:2]
        step_id = _sid()
        yield _step(step_id, "sample", "Sample rows", "running", detail="Pulling three rows from " + ", ".join(sample_tables) + ".")
        preview = []
        for name in sample_tables:
            sampled = self.runner.run_sql(f'SELECT * FROM "{name}" LIMIT 3')
            if sampled.ok:
                preview.append({"table": name, "rows": len(sampled.rows), "columns": ", ".join(sampled.columns)})
        yield _step(step_id, "sample", "Sample rows", "ok", detail="Checked live rows before writing aggregations.", rows=preview, elapsedMs=0)

    def _saas_churn(self) -> Iterator[dict]:
        start, end, label = previous_quarter(self.as_of)
        yield _step(
            _sid(),
            "plan",
            "Plan the analysis",
            "ok",
            detail=(
                f"Last quarter on the ledger clock is {label}. "
                "I'll count canceled subscriptions in that window, sum MRR, and break the losses down by cancel reason. "
                "I'll also compare product events in the 30 days before cancel with accounts that are still active."
            ),
        )
        bad = f"""
            SELECT a.name, a.segment, s.canceled_at, s.cancel_reason, s.mrr_cents
            FROM subscriptions s
            JOIN accounts a ON a.id = s.account_id
            WHERE s.status = 'canceled'
              AND s.canceled_at >= '{start.isoformat()}'
              AND s.canceled_at < '{end.isoformat()}'
        """
        yield from self._sql("First pass at the churn cohort", bad, role="rejected")
        fanout = f"""
            SELECT a.id AS account_id, a.name, a.segment, s.cancel_reason, s.mrr_cents, e.event_name
            FROM subscriptions s
            JOIN accounts a ON a.id = s.account_id
            JOIN product_events e ON e.account_id = a.id
            WHERE s.status = 'canceled'
              AND s.ended_at >= '{start.isoformat()}'
              AND s.ended_at < '{end.isoformat()}'
        """
        fanout_result, _warnings = yield from self._sql("Join usage onto the cohort", fanout, role="rejected")
        yield from self._python(
            "Check whether the join multiplied rows",
            FANOUT_CODE,
            {"rows": fanout_result.rows if fanout_result and fanout_result.ok else []},
        )
        reasons_sql = f"""
            SELECT COALESCE(NULLIF(s.cancel_reason, ''), 'unknown') AS cancel_reason,
                   COUNT(*) AS accounts,
                   ROUND(SUM(s.mrr_cents) / 100.0, 2) AS mrr_lost,
                   ROUND(AVG((
                     SELECT COUNT(*) FROM product_events e
                     WHERE e.account_id = s.account_id
                       AND e.occurred_at >= date(s.ended_at, '-30 day')
                       AND e.occurred_at < s.ended_at
                   )), 1) AS avg_events_30d
            FROM subscriptions s
            WHERE s.status = 'canceled'
              AND s.ended_at >= '{start.isoformat()}'
              AND s.ended_at < '{end.isoformat()}'
            GROUP BY 1
            ORDER BY accounts DESC
        """
        reasons, _ = yield from self._sql("Aggregate churn by reason", reasons_sql, role="answer", fixed=True)
        retained_sql = f"""
            SELECT ROUND(AVG(cnt), 1) AS avg_events_30d, COUNT(*) AS accounts
            FROM (
              SELECT (
                SELECT COUNT(*) FROM product_events e
                WHERE e.account_id = s.account_id
                  AND e.occurred_at >= date('{end.isoformat()}', '-30 day')
                  AND e.occurred_at < '{end.isoformat()}'
              ) AS cnt
              FROM subscriptions s
              WHERE s.status = 'active'
            )
        """
        retained, _ = yield from self._sql("Compare with accounts that stayed", retained_sql, role="support")
        monthly_sql = """
            SELECT strftime('%Y-%m', s.ended_at) AS month,
                   COUNT(*) AS accounts,
                   ROUND(SUM(s.mrr_cents) / 100.0, 2) AS mrr_lost
            FROM subscriptions s
            WHERE s.status = 'canceled' AND s.ended_at IS NOT NULL
            GROUP BY 1
            ORDER BY 1
        """
        monthly, _ = yield from self._sql("Monthly churn trend", monthly_sql, role="chart")
        detail_sql = f"""
            SELECT a.name, a.segment, a.country, s.plan_code, s.cancel_reason,
                   ROUND(s.mrr_cents / 100.0, 2) AS mrr_usd, s.ended_at
            FROM subscriptions s
            JOIN accounts a ON a.id = s.account_id
            WHERE s.status = 'canceled'
              AND s.ended_at >= '{start.isoformat()}'
              AND s.ended_at < '{end.isoformat()}'
            ORDER BY s.mrr_cents DESC
            LIMIT 15
        """
        detail, _ = yield from self._sql("Largest accounts that left", detail_sql, role="table")
        totals = None
        if reasons and reasons.ok and reasons.rows:
            py = yield from self._python("Sum the cohort from the aggregated rows", TOTAL_CODE, {"rows": reasons.rows})
            totals = py.result if py and py.ok else None
        answer = _narrate_churn(label, reasons.rows if reasons and reasons.ok else [], retained.rows if retained and retained.ok else [], totals)
        chart = {
            "type": "bar",
            "title": f"Churned accounts by reason, {label}",
            "xKey": "cancel_reason",
            "yKey": "accounts",
            "data": reasons.rows if reasons and reasons.ok else [],
        }
        table = {
            "title": "Largest churned accounts",
            "columns": detail.columns if detail and detail.ok else [],
            "rows": detail.rows if detail and detail.ok else [],
        }
        reason_rows = reasons.rows if reasons and reasons.ok else []
        account_count = int(totals["accounts"]) if totals else sum(int(row["accounts"]) for row in reason_rows)
        mrr_lost = float(totals["mrrLost"]) if totals else sum(float(row["mrr_lost"]) for row in reason_rows)
        widgets = [
            _metric(
                "Accounts churned",
                f"SELECT COUNT(*) AS value FROM subscriptions WHERE status = 'canceled' AND ended_at >= '{start.isoformat()}' AND ended_at < '{end.isoformat()}'",
                prefix="",
                value=account_count,
            ),
            _metric(
                "MRR lost",
                f"SELECT ROUND(SUM(mrr_cents)/100.0, 2) AS value FROM subscriptions WHERE status = 'canceled' AND ended_at >= '{start.isoformat()}' AND ended_at < '{end.isoformat()}'",
                prefix="$",
                value=mrr_lost,
            ),
            _chart_widget("Churn by reason", reasons_sql, "bar", "cancel_reason", "accounts", reasons),
            _chart_widget("Churn over time", monthly_sql, "line", "month", "accounts", monthly),
            _table_widget("Who left", detail_sql, detail),
        ]
        yield _result(answer, chart, table, "Harbor churn, last quarter", widgets, self.queries)

    def _ecom_sales(self) -> Iterator[dict]:
        start, end, label = calendar_year(self.as_of)
        yield _step(
            _sid(),
            "plan",
            "Plan the sales dashboard",
            "ok",
            detail=(
                f"I'll build {label} paid revenue from order line items, because orders do not store a total. "
                "Then category mix, refund rate, and a monthly trend."
            ),
        )
        bad = f"""
            SELECT strftime('%Y-%m', o.order_date) AS month,
                   ROUND(SUM(o.total_cents) / 100.0, 2) AS revenue
            FROM orders o
            WHERE o.status = 'paid'
              AND o.order_date >= '{start.isoformat()}'
              AND o.order_date < '{end.isoformat()}'
            GROUP BY 1
            ORDER BY 1
        """
        yield from self._sql("Monthly revenue from the order header", bad, role="rejected")
        monthly_sql = f"""
            SELECT strftime('%Y-%m', o.ordered_at) AS month,
                   ROUND(SUM(oi.quantity * oi.unit_price_cents) / 100.0, 2) AS revenue,
                   COUNT(DISTINCT o.id) AS orders
            FROM orders o
            JOIN order_items oi ON oi.order_id = o.id
            WHERE o.status = 'paid'
              AND o.ordered_at >= '{start.isoformat()}'
              AND o.ordered_at < '{end.isoformat()}'
            GROUP BY 1
            ORDER BY 1
        """
        monthly, _ = yield from self._sql("Monthly revenue from line items", monthly_sql, role="chart", fixed=True)
        category_sql = f"""
            SELECT p.category,
                   ROUND(SUM(oi.quantity * oi.unit_price_cents) / 100.0, 2) AS revenue,
                   COUNT(DISTINCT o.id) AS orders
            FROM orders o
            JOIN order_items oi ON oi.order_id = o.id
            JOIN products p ON p.id = oi.product_id
            WHERE o.status = 'paid'
              AND o.ordered_at >= '{start.isoformat()}'
              AND o.ordered_at < '{end.isoformat()}'
            GROUP BY p.category
            ORDER BY revenue DESC
        """
        categories, _ = yield from self._sql("Revenue by category", category_sql, role="answer")
        refund_sql = f"""
            SELECT p.category,
                   ROUND(100.0 * SUM(CASE WHEN o.status = 'refunded' THEN 1 ELSE 0 END) / COUNT(*), 1) AS refund_rate,
                   SUM(CASE WHEN o.status = 'refunded' THEN 1 ELSE 0 END) AS refunded_items
            FROM orders o
            JOIN order_items oi ON oi.order_id = o.id
            JOIN products p ON p.id = oi.product_id
            WHERE o.ordered_at >= '{start.isoformat()}'
              AND o.ordered_at < '{end.isoformat()}'
              AND o.status IN ('paid', 'refunded')
            GROUP BY p.category
            ORDER BY refund_rate DESC
        """
        refunds, _ = yield from self._sql("Refund rate by category", refund_sql, role="table")
        kpi_sql = f"""
            SELECT ROUND(SUM(CASE WHEN o.status = 'paid' THEN oi.quantity * oi.unit_price_cents ELSE 0 END) / 100.0, 2) AS revenue,
                   COUNT(DISTINCT CASE WHEN o.status = 'paid' THEN o.id END) AS orders,
                   ROUND(
                     SUM(CASE WHEN o.status = 'paid' THEN oi.quantity * oi.unit_price_cents ELSE 0 END) / 100.0
                     / NULLIF(COUNT(DISTINCT CASE WHEN o.status = 'paid' THEN o.id END), 0), 2
                   ) AS avg_order_value
            FROM orders o
            JOIN order_items oi ON oi.order_id = o.id
            WHERE o.ordered_at >= '{start.isoformat()}' AND o.ordered_at < '{end.isoformat()}'
        """
        kpi, _ = yield from self._sql("Year-to-date totals", kpi_sql, role="support")
        answer = _narrate_sales(label, kpi.rows[0] if kpi and kpi.ok and kpi.rows else {}, categories.rows if categories and categories.ok else [], refunds.rows if refunds and refunds.ok else [])
        chart = {
            "type": "line",
            "title": f"Paid revenue by month, {label}",
            "xKey": "month",
            "yKey": "revenue",
            "data": monthly.rows if monthly and monthly.ok else [],
        }
        table = {
            "title": "Refund rate by category",
            "columns": refunds.columns if refunds and refunds.ok else [],
            "rows": refunds.rows if refunds and refunds.ok else [],
        }
        kpi_row = kpi.rows[0] if kpi and kpi.ok and kpi.rows else {}
        widgets = [
            _metric(
                "Paid revenue",
                f"SELECT ROUND(SUM(oi.quantity * oi.unit_price_cents)/100.0, 2) AS value FROM orders o JOIN order_items oi ON oi.order_id = o.id WHERE o.status = 'paid' AND o.ordered_at >= '{start.isoformat()}' AND o.ordered_at < '{end.isoformat()}'",
                prefix="$",
                value=kpi_row.get("revenue"),
            ),
            _metric(
                "Average order value",
                f"""
                SELECT ROUND(
                  SUM(oi.quantity * oi.unit_price_cents) / 100.0
                  / NULLIF(COUNT(DISTINCT o.id), 0), 2
                ) AS value
                FROM orders o
                JOIN order_items oi ON oi.order_id = o.id
                WHERE o.status = 'paid'
                  AND o.ordered_at >= '{start.isoformat()}'
                  AND o.ordered_at < '{end.isoformat()}'
                """,
                prefix="$",
                value=kpi_row.get("avg_order_value"),
            ),
            _chart_widget("Revenue by month", monthly_sql, "line", "month", "revenue", monthly),
            _chart_widget("Revenue by category", category_sql, "bar", "category", "revenue", categories),
            _table_widget("Refund rate", refund_sql, refunds),
        ]
        yield _result(answer, chart, table, f"Northline sales, {label}", widgets, self.queries)

    def _pesa(self) -> Iterator[dict]:
        start, end, label = previous_month(self.as_of)
        yield _step(
            _sid(),
            "plan",
            "Plan the payments review",
            "ok",
            detail=(
                f"Last month on the ledger clock is {label}. "
                "I'll count failed withdrawals by county and reason, then sum fees on completed transactions. "
                "Amounts are stored in cents, and statuses are lowercase."
            ),
        )
        bad_amount = f"""
            SELECT c.county, COUNT(*) AS failed_withdrawals, ROUND(SUM(t.amount) / 100.0, 2) AS amount_kes
            FROM transactions t
            JOIN customers c ON c.id = t.customer_id
            WHERE t.txn_type = 'withdraw' AND t.status = 'failed'
              AND t.created_at >= '{start.isoformat()}' AND t.created_at < '{end.isoformat()}'
            GROUP BY c.county
        """
        yield from self._sql("Failed withdrawals by county", bad_amount, role="rejected")
        bad_status = f"""
            SELECT c.county, COUNT(*) AS failed_withdrawals
            FROM transactions t
            JOIN customers c ON c.id = t.customer_id
            WHERE t.txn_type = 'withdraw' AND t.status = 'FAILED'
              AND t.created_at >= '{start.isoformat()}' AND t.created_at < '{end.isoformat()}'
            GROUP BY c.county
        """
        yield from self._sql("Retry with the status spelled in capitals", bad_status, role="rejected")
        county_sql = f"""
            SELECT c.county, COUNT(*) AS failed_withdrawals,
                   ROUND(SUM(t.amount_cents) / 100.0, 2) AS amount_kes
            FROM transactions t
            JOIN customers c ON c.id = t.customer_id
            WHERE t.txn_type = 'withdraw' AND t.status = 'failed'
              AND t.created_at >= '{start.isoformat()}' AND t.created_at < '{end.isoformat()}'
            GROUP BY c.county
            ORDER BY failed_withdrawals DESC
        """
        counties, _ = yield from self._sql("Failed withdrawals by county, lowercase status", county_sql, role="chart", fixed=True)
        reason_sql = f"""
            SELECT c.county, COALESCE(t.failure_reason, 'unknown') AS failure_reason, COUNT(*) AS failed_withdrawals
            FROM transactions t
            JOIN customers c ON c.id = t.customer_id
            WHERE t.txn_type = 'withdraw' AND t.status = 'failed'
              AND t.created_at >= '{start.isoformat()}' AND t.created_at < '{end.isoformat()}'
            GROUP BY c.county, failure_reason
            ORDER BY failed_withdrawals DESC
        """
        reasons, _ = yield from self._sql("Failure reasons behind those withdrawals", reason_sql, role="table")
        fee_sql = f"""
            SELECT t.txn_type, COUNT(*) AS completed_txns, ROUND(SUM(t.fee_cents) / 100.0, 2) AS fee_kes
            FROM transactions t
            WHERE t.status = 'completed'
              AND t.created_at >= '{start.isoformat()}' AND t.created_at < '{end.isoformat()}'
            GROUP BY t.txn_type
            ORDER BY fee_kes DESC
        """
        fees, _ = yield from self._sql("Fee revenue on completed transactions", fee_sql, role="answer")
        fee_result = None
        if fees and fees.ok:
            py = yield from self._python("Sum fee revenue", FEE_CODE, {"rows": fees.rows})
            fee_result = py.result if py and py.ok else None
        answer = _narrate_pesa(label, counties.rows if counties and counties.ok else [], reasons.rows if reasons and reasons.ok else [], fee_result)
        chart = {
            "type": "bar",
            "title": f"Failed withdrawals by county, {label}",
            "xKey": "county",
            "yKey": "failed_withdrawals",
            "data": counties.rows if counties and counties.ok else [],
        }
        table = {
            "title": "Failure reasons",
            "columns": reasons.columns if reasons and reasons.ok else [],
            "rows": reasons.rows if reasons and reasons.ok else [],
        }
        county_rows = counties.rows if counties and counties.ok else []
        failed_count = sum(int(row["failed_withdrawals"]) for row in county_rows)
        widgets = [
            _metric(
                "Failed withdrawals",
                f"SELECT COUNT(*) AS value FROM transactions WHERE txn_type = 'withdraw' AND status = 'failed' AND created_at >= '{start.isoformat()}' AND created_at < '{end.isoformat()}'",
                prefix="",
                value=failed_count,
            ),
            _metric(
                "Fee revenue (KES)",
                f"SELECT ROUND(SUM(fee_cents)/100.0, 2) AS value FROM transactions WHERE status = 'completed' AND created_at >= '{start.isoformat()}' AND created_at < '{end.isoformat()}'",
                prefix="KES ",
                value=None if fee_result is None else fee_result.get("feeKes"),
            ),
            _chart_widget("Failed withdrawals", county_sql, "bar", "county", "failed_withdrawals", counties),
            _chart_widget("Fees by type", fee_sql, "bar", "txn_type", "fee_kes", fees),
            _table_widget("Why withdrawals failed", reason_sql, reasons),
        ]
        yield _result(answer, chart, table, f"PesaLedger operations, {label}", widgets, self.queries)

    def _count_tables(self):
        names = [name for name in self.tables if '"' not in name and "'" not in name]
        if not names:
            return None
        parts = [
            f"SELECT '{name}' AS table_name, COUNT(*) AS row_count FROM \"{name}\""
            for name in names
        ]
        sql = " UNION ALL ".join(parts)
        result, _warnings = yield from self._sql("Count rows in each table", sql, role="answer")
        return result

    def _overview(self, question: str) -> Iterator[dict]:
        yield _step(
            _sid(),
            "plan",
            "Plan an overview",
            "ok",
            detail="I'll count rows in every table, group the names by domain, and describe what the database appears to be for. No CREATE statements.",
        )
        counted = yield from self._count_tables()
        rows = counted.rows if counted and counted.ok else []
        grouped = _group_counts(rows)
        answer = _narrate_overview(question, rows, grouped)
        chart = {
            "type": "bar",
            "title": "Rows by domain",
            "xKey": "domain",
            "yKey": "row_count",
            "data": grouped,
        }
        table = {
            "title": "Tables by domain",
            "columns": ["domain", "tables", "row_count"],
            "rows": grouped,
        }
        count_sql = next((item["sql"] for item in self.queries if "COUNT(*)" in item["sql"]), "SELECT 1")
        widgets = [
            {
                "title": "Rows by domain",
                "kind": "chart",
                "sql": count_sql,
                "spec": {"type": "bar", "title": "Rows by domain", "xKey": "domain", "yKey": "row_count"},
                "preview": {"columns": ["domain", "tables", "row_count"], "rows": grouped},
            },
            {
                "title": "Tables by domain",
                "kind": "table",
                "sql": count_sql,
                "spec": {},
                "preview": {"columns": ["domain", "tables", "row_count"], "rows": grouped},
            },
        ]
        yield _result(answer, chart, table, "Database overview", widgets, self.queries)

    def _generic(self, question: str) -> Iterator[dict]:
        table, wrong, real = _generic_target(self.tables)
        yield _step(
            _sid(),
            "plan",
            "Plan a first cut",
            "ok",
            detail=f"The question does not match a prepared brief, so I'll profile {table}. I'll group a text column and correct the query if the sandbox rejects a column I guessed.",
        )
        if wrong:
            bad = f'SELECT {wrong} AS guessed, COUNT(*) AS n FROM "{table}" GROUP BY 1'
            yield from self._sql(f"Group {table} by {wrong}", bad, role="rejected")
        dim = real or "id"
        good = f'SELECT "{dim}" AS bucket, COUNT(*) AS n FROM "{table}" GROUP BY 1 ORDER BY n DESC LIMIT 12'
        grouped, _ = yield from self._sql(f"Group {table} by {dim}", good, role="chart", fixed=bool(wrong))
        rows = grouped.rows if grouped and grouped.ok else []
        top = rows[0]["bucket"] if rows else "n/a"
        count = rows[0]["n"] if rows else 0
        answer = (
            f"I profiled {table} because the question (“{question.strip()[:180]}”) did not map onto a prepared brief. "
            f"The most common {dim} is {top}, with {count} rows. "
            "The chart is that breakdown. Open a sample ledger and use one of the suggested questions for a fuller dashboard."
        )
        chart = {"type": "bar", "title": f"{table} by {dim}", "xKey": "bucket", "yKey": "n", "data": rows}
        widgets = [_chart_widget(f"{table} by {dim}", good, "bar", "bucket", "n", grouped)]
        yield _result(answer, chart, {"title": table, "columns": grouped.columns if grouped else [], "rows": rows}, f"Profile of {table}", widgets, self.queries)

    def _write(self, question: str) -> Iterator[dict]:
        from app.sql_guard import guard_sql

        candidate = None
        for line in question.splitlines():
            if __import__("re").match(r"\s*(drop|delete|update|insert|alter|truncate)\b", line, __import__("re").I):
                candidate = line.strip().rstrip(";")
                break
        table = next(iter(self.tables))
        if candidate is None:
            candidate = f'DELETE FROM "{table}" WHERE id = -1'
        decision = guard_sql(candidate)
        if decision.kind == "blocked":
            yield _step(_sid(), "guard", "Read-only guard", "error", sql=candidate, detail=decision.reason, error=decision.reason)
            yield _result(
                decision.reason + " Nothing was executed.",
                None,
                None,
                "Write refused",
                [],
                [{"sql": candidate, "status": "blocked", "purpose": "write"}],
            )
            return
        result = self.runner.run_sql(candidate)
        yield _step(
            _sid(),
            "guard",
            "Waiting for confirmation",
            "warning",
            sql=candidate,
            detail=result.error or decision.reason,
        )
        yield {
            "type": "confirmation",
            "sql": candidate,
            "token": result.confirm_token,
            "reason": result.error or decision.reason,
        }
        yield _result(
            "That statement would change data. It was not run. Confirm it in the panel if you want the sandbox to execute this exact SQL once. DROP, ALTER, and other DDL stay blocked.",
            None,
            None,
            "Confirmation required",
            [],
            [{"sql": candidate, "status": "confirmation", "purpose": "write"}],
        )

    def _sql(self, title: str, sql: str, *, role: str, fixed: bool = False):
        sql = _compact(sql)
        step_id = _sid()
        yield _step(step_id, "sql", title, "running", sql=sql, detail="Dispatching to the sandbox.")
        result = self.runner.run_sql(sql)
        warnings = assess(sql, result) if result.ok else []
        if not result.ok:
            status = "error"
            detail = result.error or "The sandbox rejected the statement."
        elif warnings:
            status = "warning"
            detail = " ".join(warnings)
        else:
            status = "fixed" if fixed else "ok"
            detail = f"{result.row_count} row{'s' if result.row_count != 1 else ''} returned."
            if result.truncated:
                detail += " Truncated at the row cap."
        self.queries.append({"sql": sql, "status": status, "purpose": title, "role": role})
        yield _step(
            step_id,
            "sql",
            title,
            status,
            sql=sql,
            detail=detail,
            columns=result.columns,
            rows=result.rows[:8],
            warnings=warnings,
            error=None if result.ok else result.error,
            elapsedMs=result.elapsed_ms,
        )
        return result, warnings

    def _python(self, title: str, code: str, input_data: dict):
        step_id = _sid()
        yield _step(step_id, "python", title, "running", code=code.strip(), detail="Running inside the sandbox.")
        result = self.runner.run_python(code, input_data=input_data)
        status = "ok" if result.ok else "error"
        detail = (result.stdout or "").strip() or (result.error or "Python finished.")
        if result.ok and isinstance(result.result, dict):
            rendered = ", ".join(f"{key}={value}" for key, value in result.result.items())
            detail = (detail + " " + rendered).strip()
        yield _step(
            step_id,
            "python",
            title,
            status,
            code=code.strip(),
            detail=detail,
            stdout=result.stdout,
            error=None if result.ok else result.error,
            elapsedMs=result.elapsed_ms,
        )
        return result


def _is_overview(question: str) -> bool:
    q = question.lower().replace("’", "'")
    phrases = (
        "what is on this",
        "what's on this",
        "what is in this",
        "what's in this",
        "what is in here",
        "what's in here",
        "what is this db",
        "what's this db",
        "what is this database",
        "what's this database",
        "what tables",
        "which tables",
        "overview",
        "describe the database",
        "describe this database",
        "describe the db",
        "what data",
        "what's on the db",
        "what is on the db",
        "thisdb",
    )
    if any(phrase in q for phrase in phrases):
        return True
    return "about" in q and any(word in q for word in ("db", "database", "schema", "data"))


def _group_counts(rows: list[dict]) -> list[dict]:
    grouped: dict[str, dict] = {}
    for row in rows:
        name = str(row.get("table_name") or "")
        domain = name.split("_", 1)[0] if "_" in name else (name or "other")
        bucket = grouped.setdefault(domain, {"domain": domain, "tables": 0, "row_count": 0, "names": []})
        bucket["tables"] += 1
        bucket["row_count"] += int(row.get("row_count") or 0)
        bucket["names"].append(name)
    ordered = sorted(grouped.values(), key=lambda item: (-item["row_count"], item["domain"]))
    return [{"domain": item["domain"], "tables": item["tables"], "row_count": item["row_count"], "names": item["names"]} for item in ordered]


def _narrate_overview(question: str, rows: list[dict], grouped: list[dict]) -> str:
    if not rows:
        return "The sandbox found no tables to count."
    total_rows = sum(int(row.get("row_count") or 0) for row in rows)
    sentences = [
        f"This database has {len(rows)} tables and {total_rows:,} rows.",
        "The names cluster by the word before the first underscore.",
    ]
    for item in grouped:
        hint = DOMAIN_HINTS.get(item["domain"], "application data")
        names = ", ".join(item["names"][:4])
        extra = f" and {item['tables'] - 4} more" if item["tables"] > 4 else ""
        sentences.append(
            f"{item['domain']} is {item['tables']} table{'s' if item['tables'] != 1 else ''} "
            f"and {item['row_count']:,} rows ({names}{extra}), which looks like {hint}."
        )
    sentences.append("Those counts are COUNT(*) results from the sandbox.")
    return " ".join(sentences)


def _route(question: str, tables: set[str], sample_id: str | None) -> str:
    import re

    q = question.lower()
    if _is_overview(q):
        return "overview"
    if re.search(r"\b(drop table|delete from|truncate|alter table|insert into)\b", q):
        return "write"
    if re.search(r"\b(delete|remove|insert|alter|truncate)\b", q) and re.search(r"\b(row|rows|record|table|database)\b", q):
        return "write"
    if "subscriptions" in tables and any(word in q for word in ("churn", "cancel", "retention", "left")):
        return "saas_churn"
    if "orders" in tables and any(word in q for word in ("dashboard", "sales", "revenue", "refund", "aov", "category")):
        return "ecom_sales"
    if "transactions" in tables and any(word in q for word in ("withdraw", "fee", "county", "fail", "mpesa", "m-pesa", "airtime", "paybill", "lipa", "float")):
        return "pesa"
    if sample_id == "harbor" and "dashboard" in q and "subscriptions" in tables:
        return "saas_churn"
    if sample_id == "northline" and "dashboard" in q and "orders" in tables:
        return "ecom_sales"
    if sample_id == "pesaledger" and "transactions" in tables and ("dashboard" in q or "last month" in q):
        return "pesa"
    return "generic"


def _generic_target(tables: dict[str, dict]) -> tuple[str, str | None, str | None]:
    guesses = [
        ("ended_at", "canceled_at"),
        ("ordered_at", "order_date"),
        ("amount_cents", "amount"),
        ("mrr_cents", "mrr"),
        ("full_name", "name"),
        ("cancel_reason", "reason"),
    ]
    for name, info in tables.items():
        have = set(info["columns"])
        for real, wrong in guesses:
            if real in have and wrong not in have:
                return name, wrong, real
    name = next(iter(tables))
    columns = tables[name]["columns"]
    textish = next((column for column in columns if column not in {"id"} and not column.endswith("_id")), columns[0] if columns else None)
    return name, None, textish


def _narrate_churn(label: str, reasons: list[dict], retained_rows: list[dict], totals: dict | None) -> str:
    if not reasons:
        return f"No canceled subscriptions fell in {label}."
    accounts = int(totals["accounts"]) if totals else sum(int(row["accounts"]) for row in reasons)
    mrr = float(totals["mrrLost"]) if totals else sum(float(row["mrr_lost"]) for row in reasons)
    top = max(reasons, key=lambda row: int(row["accounts"]))
    low = next((row for row in reasons if row["cancel_reason"] == "low_usage"), None)
    retained = retained_rows[0]["avg_events_30d"] if retained_rows else None
    text = (
        f"{accounts} accounts canceled in {label}, taking ${mrr:,.0f} of monthly recurring revenue with them. "
        f"The largest reason by count was {str(top['cancel_reason']).replace('_', ' ')}: "
        f"{int(top['accounts'])} accounts and ${float(top['mrr_lost']):,.0f} MRR."
    )
    if low and retained is not None:
        text += (
            f" Accounts that cited low usage averaged {low['avg_events_30d']} product events in the 30 days before they left,"
            f" against {retained} events for accounts that are still active."
            " Price and missing features still account for more of the logos than inactivity."
        )
    text += " The join that attached every product event was discarded after the sandbox showed it had multiplied the cohort."
    return text


def _narrate_sales(label: str, kpi: dict, categories: list[dict], refunds: list[dict]) -> str:
    if not kpi:
        return f"No paid orders were found for {label}."
    top = categories[0] if categories else None
    worst = refunds[0] if refunds else None
    text = (
        f"In {label}, paid line items came to ${float(kpi['revenue']):,.0f} across {int(kpi['orders'])} orders,"
        f" an average of ${float(kpi['avg_order_value']):,.2f}."
    )
    if top:
        text += f" {top['category']} led the mix at ${float(top['revenue']):,.0f}."
    if worst:
        text += f" The highest refund rate was {worst['category']} at {worst['refund_rate']}% of paid-or-refunded items."
    text += " The first query looked for order_date and total_cents on the order header. Those columns are not in the schema; revenue is the sum of line items."
    return text


def _narrate_pesa(label: str, counties: list[dict], reasons: list[dict], fees: dict | None) -> str:
    if not counties:
        return f"No failed withdrawals were recorded in {label}."
    worst = counties[0]
    top_reason = reasons[0]["failure_reason"] if reasons else "unknown"
    fee_text = f"KES {float(fees['feeKes']):,.0f}" if fees else "an amount the fee query did not return"
    return (
        f"In {label}, {worst['county']} had the most failed withdrawals ({int(worst['failed_withdrawals'])}), "
        f"worth about KES {float(worst['amount_kes']):,.0f}. "
        f"The most common reason on the breakdown is {str(top_reason).replace('_', ' ')}. "
        f"Completed transactions collected {fee_text} in fees. "
        "The first query used a column named amount, which is not in the ledger, and the retry looked for status FAILED. Stored statuses are lowercase."
    )


def _metric(title: str, sql: str, prefix: str, value=None) -> dict:
    preview = {} if value is None else {"value": value}
    return {"title": title, "kind": "metric", "sql": _compact(sql), "spec": {"prefix": prefix}, "preview": preview}


def _chart_widget(title: str, sql: str, chart_type: str, x_key: str, y_key: str, result) -> dict:
    data = result.rows if result and result.ok else []
    return {
        "title": title,
        "kind": "chart",
        "sql": _compact(sql),
        "spec": {"type": chart_type, "title": title, "xKey": x_key, "yKey": y_key},
        "preview": {"columns": result.columns if result and result.ok else [], "rows": data},
    }


def _table_widget(title: str, sql: str, result) -> dict:
    return {
        "title": title,
        "kind": "table",
        "sql": _compact(sql),
        "spec": {},
        "preview": {
            "columns": result.columns if result and result.ok else [],
            "rows": result.rows if result and result.ok else [],
        },
    }


def _result(answer, chart, table, dash_title, widgets, queries) -> dict:
    return {
        "type": "result",
        "result": {
            "answer": answer,
            "chart": chart,
            "table": table,
            "dashboard": {"title": dash_title, "widgets": widgets} if widgets else None,
            "queries": [{"sql": item["sql"], "status": item["status"], "purpose": item["purpose"]} for item in queries],
        },
    }


def _compact(sql: str) -> str:
    return "\n".join(line.rstrip() for line in sql.strip().splitlines())


def _sid() -> str:
    return uuid.uuid4().hex[:8]


def _step(step_id: str, kind: str, title: str, status: str, **extra) -> dict:
    step = {"id": step_id, "kind": kind, "title": title, "status": status}
    for key, value in extra.items():
        if value is None:
            continue
        step[key] = value
    return {"type": "step", "step": step}


def _event(kind: str, message: str) -> dict:
    return {"type": kind, "error": message}


def schema_overview(db_path: str) -> list[dict]:
    """Used by tests. Not a substitute for the sandbox explore step."""
    conn = sqlite3.connect(db_path)
    try:
        return [{"name": row[0], "sql": row[1]} for row in conn.execute("SELECT name, sql FROM sqlite_master WHERE type='table'")]
    finally:
        conn.close()


_CONSTRAINT_WORDS = {"constraint", "primary", "foreign", "unique", "check", "key", "index", "fulltext", "spatial"}


def _split_top_level(body: str) -> list[str]:
    parts, depth, buf, quote = [], 0, [], None
    for ch in body:
        if quote:
            buf.append(ch)
            if ch == quote:
                quote = None
            continue
        if ch in "\"'`[":
            quote = "]" if ch == "[" else ch
        elif ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
        elif ch == "," and depth == 0:
            parts.append("".join(buf)); buf = []; continue
        buf.append(ch)
    if "".join(buf).strip():
        parts.append("".join(buf))
    return parts


def parse_create_columns(sql: str) -> list[str]:
    """Tolerant column extraction from a CREATE TABLE statement."""
    start = sql.find("(")
    if start < 0:
        return []
    depth, end, quote = 0, -1, None
    for i in range(start, len(sql)):
        ch = sql[i]
        if quote:
            if ch == quote:
                quote = None
            continue
        if ch in "\"'`[":
            quote = "]" if ch == "[" else ch
        elif ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
            if depth == 0:
                end = i
                break
    body = sql[start + 1 : end if end > 0 else len(sql)]
    cols: list[str] = []
    for part in _split_top_level(body):
        part = part.strip()
        if not part:
            continue
        m = re.match(r'"((?:[^"]|"")+)"|`((?:[^`]|``)+)`|\[([^\]]+)\]|([A-Za-z_][\w$]*)', part)
        if not m:
            continue
        if m.group(4) and m.group(4).lower() in _CONSTRAINT_WORDS:
            continue
        name = m.group(1) or m.group(2) or m.group(3) or m.group(4)
        cols.append(name.replace('""', '"').replace("``", "`"))
    return cols
