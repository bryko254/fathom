"""Analyst loop backed by an OpenAI-compatible chat API.

Vultr Serverless Inference is the intended host (`https://api.vultrinference.com/v1`).
Any other OpenAI-compatible base URL works through the same three environment variables.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterator

from app.config import Settings
from app.mock_agent import MockAnalyst, _sid, _step
from app.sandbox import SandboxRunner
from app.sanity import assess

SYSTEM = """You are Fathom, an analyst who answers questions about a SQLite database by doing the work.
You only know the database through tool results. Never invent a number, a column, or a row.

Reply with one JSON object and nothing else. Actions:
{"action":"sample","table":"accounts"}
{"action":"run_sql","purpose":"why this query","sql":"SELECT ..."}
{"action":"run_python","purpose":"what you are checking","code":"RESULT = {...}\\nprint(...)"}
{"action":"finish","answer":"...","chart":{"type":"bar","title":"...","xKey":"...","yKey":"...","data":[]},"table":{"title":"...","columns":[],"rows":[]},"dashboard":{"title":"...","widgets":[{"title":"...","kind":"chart","sql":"SELECT ...","spec":{"type":"bar","xKey":"...","yKey":"..."}}]}}

Rules:
- SQL is a single read-only statement. No writes, DDL, PRAGMA, ATTACH, or multiple statements.
- Use only columns that appeared in the schema or a sample.
- chart.data and table.rows must be copied from tool results, not estimated.
- If a tool result has ok=false or warnings, do not finish. Fix the SQL and run it again.
- A zero-row result, a missing column, or a join fanout warning is a failed attempt.
- Prefer aggregations for charts.
- dashboard.sql must be rerunnable SELECT statements.
- Python may import only json, math, statistics, datetime, collections, re, itertools. Read rows from INPUT. Set RESULT.
- Finish within 8 actions.
- A containment probe has already been executed and blocked. Do not emit DROP, writes, network calls, or secret reads.
- Your LLM calls are served by Vultr Serverless Inference. Do not ask for another provider.
"""


class LlmAnalyst:
    def __init__(self, *, runner: SandboxRunner, settings: Settings, sample_id: str | None) -> None:
        self.runner = runner
        self.settings = settings
        self.sample_id = sample_id
        self.queries: list[dict] = []
        self._last_rows: list[dict] = []
        self._last_columns: list[str] = []

    def run(self, question: str) -> Iterator[dict]:
        explorer = MockAnalyst(runner=self.runner, sample_id=self.sample_id)
        yield from explorer._explore()
        if not explorer.tables:
            yield {"type": "error", "error": "Could not read the schema."}
            return
        yield from explorer._containment()
        schema = [
            {"table": name, "columns": info["columns"]}
            for name, info in explorer.tables.items()
        ]
        messages = [
            {"role": "system", "content": SYSTEM},
            {
                "role": "user",
                "content": json.dumps(
                    {
                        "question": question,
                        "schema": schema,
                        "ledgerClock": "2026-11-04",
                        "note": "Last quarter relative to the ledger clock is 2026-07-01 inclusive to 2026-10-01 exclusive. Last month is October 2026.",
                    }
                ),
            },
        ]
        try:
            from openai import OpenAI
        except ImportError as exc:
            yield {"type": "error", "error": f"OpenAI client is not installed: {exc}"}
            return
        client = OpenAI(base_url=self.settings.llm_base_url, api_key=self.settings.llm_api_key, timeout=90)
        for _ in range(8):
            try:
                action, raw = _complete(client, self.settings.llm_model, messages)
            except Exception as exc:  # noqa: BLE001
                yield {"type": "error", "error": f"Inference call failed: {exc}"}
                return
            messages.append({"role": "assistant", "content": raw})
            kind = action.get("action")
            if kind == "sample":
                table = str(action.get("table") or "")
                if table not in explorer.tables:
                    messages.append({"role": "user", "content": json.dumps({"ok": False, "error": "unknown table"})})
                    continue
                result_event, payload = self._run_sql(f"Sample {table}", f'SELECT * FROM "{table}" LIMIT 5', fixed=False)
                yield result_event[0]
                yield result_event[1]
                messages.append({"role": "user", "content": json.dumps(payload)})
            elif kind == "run_sql":
                sql = str(action.get("sql") or "")
                purpose = str(action.get("purpose") or "Run SQL")
                events, payload = self._run_sql(purpose, sql, fixed=bool(self.queries and self.queries[-1]["status"] in {"error", "warning"}))
                for event in events:
                    yield event
                if payload.get("needsConfirmation"):
                    yield {
                        "type": "confirmation",
                        "sql": sql,
                        "token": payload.get("confirmToken"),
                        "reason": payload.get("error"),
                    }
                    yield _finish_early("That statement changes data and was not run. Confirm it to execute this exact SQL once.")
                    return
                messages.append({"role": "user", "content": json.dumps(payload)})
            elif kind == "run_python":
                code = str(action.get("code") or "")
                purpose = str(action.get("purpose") or "Run Python")
                step_id = _sid()
                yield _step(step_id, "python", purpose, "running", code=code, detail="Running inside the sandbox.")
                result = self.runner.run_python(code, input_data={"rows": self._last_rows})
                status = "ok" if result.ok else "error"
                yield _step(
                    step_id,
                    "python",
                    purpose,
                    status,
                    code=code,
                    detail=(result.stdout or result.error or "").strip(),
                    stdout=result.stdout,
                    error=None if result.ok else result.error,
                    elapsedMs=result.elapsed_ms,
                )
                messages.append(
                    {
                        "role": "user",
                        "content": json.dumps({"ok": result.ok, "stdout": result.stdout, "result": result.result, "error": result.error}),
                    }
                )
            elif kind == "finish":
                yield self._finish(action)
                return
            else:
                messages.append({"role": "user", "content": json.dumps({"ok": False, "error": "unknown action"})})
        yield {"type": "error", "error": "The analyst stopped after 8 actions without a final answer."}

    def _run_sql(self, purpose: str, sql: str, *, fixed: bool) -> tuple[list[dict], dict]:
        step_id = _sid()
        running = _step(step_id, "sql", purpose, "running", sql=sql, detail="Dispatching to the sandbox.")
        result = self.runner.run_sql(sql)
        warnings = assess(sql, result) if result.ok else []
        if result.needs_confirmation:
            done = _step(step_id, "guard", "Waiting for confirmation", "warning", sql=sql, detail=result.error)
            self.queries.append({"sql": sql, "status": "confirmation", "purpose": purpose})
            return [running, done], {
                "ok": False,
                "needsConfirmation": True,
                "confirmToken": result.confirm_token,
                "error": result.error,
            }
        if not result.ok:
            status, detail = "error", result.error or "The sandbox rejected the statement."
        elif warnings:
            status, detail = "warning", " ".join(warnings)
        else:
            status, detail = ("fixed" if fixed else "ok"), f"{result.row_count} rows returned."
            self._last_rows = result.rows
            self._last_columns = result.columns
        self.queries.append({"sql": sql, "status": status, "purpose": purpose})
        done = _step(
            step_id,
            "sql",
            purpose,
            status,
            sql=sql,
            detail=detail,
            columns=result.columns,
            rows=result.rows[:8],
            warnings=warnings,
            error=None if result.ok else result.error,
            elapsedMs=result.elapsed_ms,
        )
        return [running, done], {
            "ok": result.ok and not warnings,
            "error": result.error,
            "warnings": warnings,
            "columns": result.columns,
            "rows": result.rows[:40],
            "rowCount": result.row_count,
            "truncated": result.truncated,
        }

    def _finish(self, action: dict) -> dict:
        chart = action.get("chart")
        if isinstance(chart, dict) and not chart.get("data") and self._last_rows:
            chart = {**chart, "data": self._last_rows}
        table = action.get("table")
        dashboard = action.get("dashboard")
        if isinstance(dashboard, dict):
            widgets = []
            for widget in dashboard.get("widgets") or []:
                widgets.append(
                    {
                        "title": widget.get("title") or "Query",
                        "kind": widget.get("kind") or "table",
                        "sql": widget.get("sql") or "",
                        "spec": widget.get("spec") or {},
                        "preview": {"columns": self._last_columns, "rows": self._last_rows[:20]},
                    }
                )
            dashboard = {"title": dashboard.get("title") or "Dashboard", "widgets": widgets}
        elif self.queries:
            last = next((item for item in reversed(self.queries) if item["status"] in {"ok", "fixed"}), None)
            if last:
                dashboard = {
                    "title": "Saved query",
                    "widgets": [
                        {
                            "title": last["purpose"],
                            "kind": "table",
                            "sql": last["sql"],
                            "spec": {},
                            "preview": {"columns": self._last_columns, "rows": self._last_rows[:20]},
                        }
                    ],
                }
        return {
            "type": "result",
            "result": {
                "answer": str(action.get("answer") or "The analyst finished without a written answer."),
                "chart": chart if isinstance(chart, dict) else None,
                "table": table if isinstance(table, dict) else None,
                "dashboard": dashboard,
                "queries": [{"sql": item["sql"], "status": item["status"], "purpose": item["purpose"]} for item in self.queries],
            },
        }


def _complete(client, model: str, messages: list[dict]) -> tuple[dict, str]:
    response = client.chat.completions.create(
        model=model,
        messages=messages,
        temperature=0.1,
        max_tokens=1800,
    )
    raw = response.choices[0].message.content or ""
    return _parse_action(raw), raw


def _parse_action(raw: str) -> dict:
    text = raw.strip()
    fenced = re.search(r"```(?:json)?\s*(\{.*\})\s*```", text, re.S)
    if fenced:
        text = fenced.group(1)
    start = text.find("{")
    end = text.rfind("}")
    if start < 0 or end < start:
        raise ValueError("The model did not return JSON.")
    return json.loads(text[start : end + 1])


def _finish_early(answer: str) -> dict:
    return {"type": "result", "result": {"answer": answer, "chart": None, "table": None, "dashboard": None, "queries": []}}
