"""Analyst loop backed by an OpenAI-compatible chat API.

Vultr Serverless Inference is the intended host (`https://api.vultrinference.com/v1`).
Any other OpenAI-compatible base URL works through the same three environment variables.
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor

from app.config import Settings
from app.mock_agent import DEMO_SAMPLE_IDS, MockAnalyst, _group_counts, _is_overview, _narrate_overview, _sid, _step
from app.sanitize import SCHEMA_PROMPT_LIMIT, cap_sample_payload, schema_for_prompt
from app.sandbox import SandboxRunner
from app.sanity import assess

logger = logging.getLogger("fathom.inference")
MAX_TOKENS = 3072
MAX_ACTIONS = 6
THINKING = os.environ.get("LLM_THINKING", "off").strip().lower() in {"1", "on", "true"}

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
- Finish within 5 actions. Combine questions into one aggregate query where you can; do not page through rows.
- Keep finish small: answer under 150 words, leave chart.data empty (it is filled from your last successful query, so make that query the chart data), table.rows at most 12, dashboard at most 4 widgets.
- Do not emit DROP, writes, network calls, or secret reads. If you do, the guard will block them and you must continue with a read.
- Your calls are served by Vultr Serverless Inference. Do not ask for another provider.
- For a question about what the database contains, write a plain-English overview grouped by domain. Use the row counts you were given. Do not paste CREATE statements or sqlite_master.
"""


class LlmAnalyst:
    def __init__(self, *, runner: SandboxRunner, settings: Settings, sample_id: str | None, client=None) -> None:
        self.runner = runner
        self.settings = settings
        self.sample_id = sample_id
        self.client = client
        self.queries: list[dict] = []
        self._last_rows: list[dict] = []
        self._last_columns: list[str] = []

    def run(self, question: str) -> Iterator[dict]:
        explorer = MockAnalyst(runner=self.runner, sample_id=self.sample_id)
        yield from explorer._explore()
        if not explorer.tables:
            yield {"type": "error", "error": "Could not read the schema."}
            return
        if self.sample_id in DEMO_SAMPLE_IDS:
            yield from explorer._containment()
        if _is_overview(question):
            yield from self._overview(explorer, question)
            return
        schema = schema_for_prompt(explorer.tables)
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
        client = self.client or _openai_client(self.settings)
        if client is None:
            yield {"type": "error", "error": "OpenAI client is not installed."}
            return
        for _ in range(MAX_ACTIONS):
            try:
                action, raw = _complete(client, self.settings.llm_model, messages)
            except ValueError as exc:
                logger.warning("model reply unusable after retry: %s", exc)
                yield _finish_early(
                    "The model's reply could not be read as a plan, even after a retry. "
                    "Please ask again, or rephrase the question a little more specifically."
                )
                return
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
                messages.append({"role": "user", "content": json.dumps(_bounded_payload(payload))})
            elif kind == "run_sql":
                sql = str(action.get("sql") or "")
                purpose = str(action.get("purpose") or "Run SQL")
                events, payload = self._run_sql(purpose, sql, fixed=bool(self.queries and self.queries[-1]["status"] in {"error", "warning"}))
                for event in events:
                    yield event
                if payload.get("blocked"):
                    messages.append({"role": "user", "content": json.dumps(_bounded_payload(payload))})
                    continue
                if payload.get("needsConfirmation"):
                    yield {
                        "type": "confirmation",
                        "sql": sql,
                        "token": payload.get("confirmToken"),
                        "reason": payload.get("error"),
                    }
                    yield _finish_early("That statement changes data and was not run. Confirm it to execute this exact SQL once.")
                    return
                messages.append({"role": "user", "content": json.dumps(_bounded_payload(payload))})
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
        yield {"type": "error", "error": f"The analyst stopped after {MAX_ACTIONS} actions without a final answer."}

    def _run_sql(self, purpose: str, sql: str, *, fixed: bool) -> tuple[list[dict], dict]:
        step_id = _sid()
        running = _step(step_id, "sql", purpose, "running", sql=sql, detail="Dispatching to the sandbox.")
        result = self.runner.run_sql(sql)
        warnings = assess(sql, result) if result.ok else []
        if result.blocked or result.needs_confirmation:
            done = _step(
                step_id,
                "guard",
                "Containment: model statement blocked",
                "blocked" if result.blocked and not result.needs_confirmation else "warning",
                sql=sql,
                detail=result.error,
                error=result.error,
            )
            if result.needs_confirmation:
                self.queries.append({"sql": sql, "status": "confirmation", "purpose": purpose})
                return [running, done], {
                    "ok": False,
                    "needsConfirmation": True,
                    "confirmToken": result.confirm_token,
                    "error": result.error,
                }
            self.queries.append({"sql": sql, "status": "blocked", "purpose": purpose})
            return [running, done], {"ok": False, "blocked": True, "error": result.error}
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
            raw_widgets = [w for w in (dashboard.get("widgets") or [])[:6] if isinstance(w, dict)]
            previews = self._widget_previews([str(w.get("sql") or "") for w in raw_widgets])
            widgets = []
            for widget, preview in zip(raw_widgets, previews):
                widgets.append(
                    {
                        "title": widget.get("title") or "Query",
                        "kind": widget.get("kind") or "table",
                        "sql": widget.get("sql") or "",
                        "spec": widget.get("spec") or {},
                        "preview": preview,
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
                "provider": "openai",
                "model": self.settings.llm_model,
            },
        }

    def _widget_previews(self, sqls: list[str]) -> list[dict]:
        """Run each widget's own SELECT in its own sandbox (in parallel) so every tile shows its data."""
        fallback = {"columns": self._last_columns, "rows": self._last_rows[:20]}

        def one(sql: str) -> dict:
            if not sql.strip():
                return fallback
            try:
                result = self.runner.run_sql(sql)
            except Exception:  # noqa: BLE001
                return fallback
            if result.ok and not result.blocked:
                return {"columns": result.columns, "rows": result.rows[:20]}
            return {"columns": [], "rows": [], "error": result.error}

        if not sqls:
            return []
        with ThreadPoolExecutor(max_workers=min(4, len(sqls))) as pool:
            return list(pool.map(one, sqls))

    def _overview(self, explorer: MockAnalyst, question: str) -> Iterator[dict]:
        yield _step(
            _sid(),
            "plan",
            "Plan an overview",
            "ok",
            detail="I'll count every table in the sandbox, then ask the model for a plain-English overview grouped by domain.",
        )
        counted = yield from explorer._count_tables()
        rows = counted.rows if counted and counted.ok else []
        self.queries.extend(explorer.queries)
        grouped = _group_counts(rows)
        fallback = _narrate_overview(question, rows, grouped)
        answer = ""
        client = self.client or _openai_client(self.settings)
        if client is None:
            answer = fallback
        else:
            prompt_rows = rows[:80]
            prompt_domains = [{key: value for key, value in item.items() if key != "names"} | {"examples": item["names"][:6]} for item in grouped[:40]]
            messages = [
                {
                    "role": "system",
                    "content": (
                        "You are Fathom. Write a plain-English overview of a database from sandbox row counts. "
                        "Do not invent numbers. Group tables by the prefix before the first underscore. "
                        "Say what the application appears to do. Reply with one JSON object and nothing else: "
                        '{"action":"finish","answer":"..."}'
                    ),
                },
                {
                    "role": "user",
                    "content": json.dumps({"question": question, "counts": prompt_rows, "domains": prompt_domains})[:SCHEMA_PROMPT_LIMIT],
                },
            ]
            try:
                action, _raw = _complete(client, self.settings.llm_model, messages)
                answer = str(action.get("answer") or "").strip()
            except Exception:
                logger.exception("overview inference failed; using the row-count summary")
                answer = ""
        if not answer:
            answer = "The model did not return an overview, so this summary is from the row counts. " + fallback
        logger.info("answer provider=openai model=%s kind=overview fallback=%s", self.settings.llm_model, answer.startswith("The model did not"))
        yield {
            "type": "result",
            "result": {
                "answer": answer,
                "chart": {
                    "type": "bar",
                    "title": "Rows by domain",
                    "xKey": "domain",
                    "yKey": "row_count",
                    "data": grouped,
                },
                "table": {"title": "Tables by domain", "columns": ["domain", "tables", "row_count"], "rows": grouped},
                "dashboard": {
                    "title": "Database overview",
                    "widgets": [
                        {
                            "title": "Rows by domain",
                            "kind": "chart",
                            "sql": next((item["sql"] for item in explorer.queries if "COUNT(*)" in item["sql"]), "SELECT 1"),
                            "spec": {"type": "bar", "xKey": "domain", "yKey": "row_count"},
                            "preview": {"columns": ["domain", "tables", "row_count"], "rows": grouped},
                        }
                    ],
                },
                "queries": [{"sql": item["sql"], "status": item["status"], "purpose": item["purpose"]} for item in self.queries],
                "provider": "openai",
                "model": self.settings.llm_model,
            },
        }


def _bounded_payload(payload: dict) -> dict:
    body = dict(payload)
    rows = body.get("rows")
    if isinstance(rows, list):
        kept = cap_sample_payload(rows)
        body["rows"] = kept
        if len(kept) < len(rows):
            body["rowsOmitted"] = len(rows) - len(kept)
    return body


def _openai_client(settings: Settings):
    try:
        from openai import OpenAI
    except ImportError:
        return None
    return OpenAI(base_url=settings.llm_base_url, api_key=settings.llm_api_key or "missing", timeout=60, max_retries=1)


def message_text(message) -> str:
    """Prefer assistant content. Some Vultr models leave it empty and put the text in reasoning."""
    if isinstance(message, dict):
        content = message.get("content") or ""
        extra = message
    else:
        content = getattr(message, "content", None) or ""
        extra = getattr(message, "model_extra", None) or {}
        if not isinstance(extra, dict):
            extra = {}
    reasoning = ""
    if not isinstance(message, dict):
        reasoning = getattr(message, "reasoning_content", None) or getattr(message, "reasoning", None) or ""
    if not reasoning:
        reasoning = extra.get("reasoning_content") or extra.get("reasoning") or ""
    content = str(content).strip()
    reasoning = str(reasoning).strip()
    return content or reasoning


def _complete(client, model: str, messages: list[dict]) -> tuple[dict, str]:
    """One call, plus at most one short retry when the reply is cut off or unreadable."""
    response, text = _invoke(client, model, messages, MAX_TOKENS)
    finish = response.choices[0].finish_reason
    if text:
        try:
            return _parse_action(text), text
        except ValueError:
            logger.info("unparseable model reply finish_reason=%s; retrying once with a stricter prompt", finish)
    retry = list(messages)
    if text:
        retry.append({"role": "assistant", "content": text[:2000]})
    retry.append({"role": "user", "content": STRICT_RETRY + (" Your reply was cut off; keep it short." if finish == "length" else "")})
    _, text2 = _invoke(client, model, retry, MAX_TOKENS)
    if not text2:
        raise ValueError("The model returned no text after a retry.")
    return _parse_action(text2), text2


def _invoke(client, model: str, messages: list[dict], max_tokens: int):
    logger.info("inference request model=%s max_tokens=%s", model, max_tokens)
    started = time.perf_counter()
    response = client.chat.completions.create(
        model=model,
        messages=messages,
        temperature=0.1,
        max_tokens=max_tokens,
        **({} if THINKING else {"extra_body": {"chat_template_kwargs": {"thinking": False}}}),
    )
    text = message_text(response.choices[0].message)
    usage = getattr(response, "usage", None)
    logger.info(
        "inference response model=%s finish_reason=%s text_chars=%s completion_tokens=%s seconds=%.2f",
        model,
        response.choices[0].finish_reason,
        len(text),
        getattr(usage, "completion_tokens", None),
        time.perf_counter() - started,
    )
    return response, text


def _parse_action(raw: str) -> dict:
    """Pull the first usable action object out of loosely formatted model text."""
    text = (raw or "").strip()
    text = re.sub(r"<think>.*?</think>", "", text, flags=re.S | re.I).strip()
    text = re.sub(r"```(?:json)?", "", text, flags=re.I)
    decoder = json.JSONDecoder()
    objects: list[dict] = []
    i = 0
    while True:
        i = text.find("{", i)
        if i < 0:
            break
        try:
            obj, end = decoder.raw_decode(text, i)
        except json.JSONDecodeError:
            i += 1
            continue
        if isinstance(obj, dict):
            objects.append(obj)
        i = end
    if not objects:
        raise ValueError("The model did not return JSON.")
    with_action = [o for o in objects if o.get("action")]
    for o in with_action:
        if o.get("action") == "run_sql" and o.get("sql"):
            return o
    if with_action:
        return with_action[0]
    merged: dict = {}
    for o in objects:
        merged.update(o)
    if "action" not in merged and merged.get("sql"):
        merged["action"] = "run_sql"
    return merged


STRICT_RETRY = (
    "Your last reply could not be parsed. Reply with exactly ONE JSON object and nothing else: "
    "no prose, no code fences, no second object."
)


def _finish_early(answer: str) -> dict:
    return {"type": "result", "result": {"answer": answer, "chart": None, "table": None, "dashboard": None, "queries": []}}
