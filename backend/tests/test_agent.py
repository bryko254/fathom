import sqlite3

from app.clock import LEDGER_TODAY
from app.config import load_settings
from app.samples import ensure_samples
from app.sandbox import SandboxRunner
from app.mock_agent import MockAnalyst


def _events(tmp_path, question: str, sample: str = "harbor"):
    paths = ensure_samples(tmp_path / "samples")
    settings = load_settings()
    runner = SandboxRunner(
        mode="subprocess",
        worker_path=settings.worker_path,
        timeout_s=20,
        max_rows=500,
        db_path=str(paths[sample]),
    )
    analyst = MockAnalyst(runner=runner, sample_id=sample, as_of=LEDGER_TODAY)
    return list(analyst.run(question)), paths[sample]


def _final_steps(events):
    latest = {}
    order = []
    for event in events:
        if event["type"] != "step":
            continue
        step = event["step"]
        if step["id"] not in latest:
            order.append(step["id"])
        latest[step["id"]] = step
    return [latest[key] for key in order]


def test_churn_question_self_corrects_and_builds_a_dashboard(tmp_path):
    events, db = _events(tmp_path, "Which customers churned last quarter and why?")
    steps = _final_steps(events)
    statuses = [step["status"] for step in steps]
    assert "error" in statuses
    assert "warning" in statuses
    assert "fixed" in statuses
    assert "blocked" in statuses
    assert any("DROP TABLE" in (step.get("sql") or "") and step["status"] == "blocked" for step in steps)
    failed = next(step for step in steps if step["status"] == "error" and step["kind"] == "sql")
    assert "canceled_at" in failed["sql"]
    warned = next(step for step in steps if step["status"] == "warning")
    assert "fanout" in (warned.get("detail") or "").lower() or any("fanout" in w.lower() for w in warned.get("warnings") or [])
    result = next(event["result"] for event in events if event["type"] == "result")
    expected = sqlite3.connect(db).execute(
        """
        SELECT COUNT(*) FROM subscriptions
        WHERE status = 'canceled' AND ended_at >= '2026-07-01' AND ended_at < '2026-10-01'
        """
    ).fetchone()[0]
    assert str(expected) in result["answer"]
    assert expected > 0
    assert result["chart"]["data"]
    assert result["chart"]["xKey"] == "cancel_reason"
    assert len(result["dashboard"]["widgets"]) >= 3
    metrics = [widget for widget in result["dashboard"]["widgets"] if widget["kind"] == "metric"]
    assert metrics[0]["preview"]["value"] == expected
    assert metrics[1]["preview"]["value"] > 0
    assert any(step["kind"] == "python" and step["status"] == "ok" for step in steps)


def test_sales_dashboard_fixes_the_missing_column(tmp_path):
    events, _db = _events(tmp_path, "Build me a sales dashboard for this year", sample="northline")
    steps = _final_steps(events)
    assert any(step["status"] == "error" and "order_date" in (step.get("sql") or "") for step in steps)
    assert any(step["status"] == "fixed" for step in steps)
    result = next(event["result"] for event in events if event["type"] == "result")
    assert result["chart"]["type"] == "line"
    assert result["chart"]["data"]
    assert "$" in result["answer"]


def test_pesa_question_fixes_a_wrong_literal(tmp_path):
    events, _db = _events(
        tmp_path,
        "Where are withdrawals failing, and how much fee revenue did we collect last month?",
        sample="pesaledger",
    )
    steps = _final_steps(events)
    assert any(step["status"] == "error" for step in steps)
    assert any(step["status"] == "warning" for step in steps)
    result = next(event["result"] for event in events if event["type"] == "result")
    assert "KES" in result["answer"]
    assert result["chart"]["data"]
