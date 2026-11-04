import json
import sqlite3
from decimal import Decimal
from types import SimpleNamespace

from app.llm_agent import LlmAnalyst
from app.mock_agent import MockAnalyst, _is_overview
from app.sanitize import cap_sample_payload, sanitize_cell, sanitize_rows, schema_for_prompt
from app.sandbox import SandboxRunner
from tests.test_overview import _runner, _settings


def test_sanitize_blobs_long_text_and_decimals():
    rows = sanitize_rows(
        [
            {
                "note": "x" * 500,
                "photo": b"\x00\xff" * 100,
                "amount": Decimal("12.50"),
                "ok": 1,
            }
        ]
    )
    assert rows[0]["photo"] == "<blob 200 bytes>"
    assert rows[0]["note"].endswith("…")
    assert len(rows[0]["note"]) == 201
    assert rows[0]["amount"] == "12.50"
    assert rows[0]["ok"] == 1
    assert sanitize_cell(float("nan")) is None
    packed = cap_sample_payload([{"text": "y" * 200} for _ in range(100)], budget=1000)
    assert len(json.dumps(packed)) <= 1200
    wide = {f"table_{index}": {"columns": [f"col_{n}" for n in range(10)]} for index in range(400)}
    prompt = schema_for_prompt(wide, budget=1500)
    assert len(json.dumps(prompt)) < 2500
    assert any("omitted" in item["table"] for item in prompt)


def test_blob_database_overview_stays_small_and_answers(tmp_path):
    path = tmp_path / "notes.sqlite"
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE therapist_note (id integer, body text, scan blob)")
    conn.execute("CREATE TABLE payment_receipt (id integer, amount text)")
    conn.execute(
        "INSERT INTO therapist_note VALUES (1, ?, ?)",
        ("journal " * 500, b"\x89PNG" + b"\x00" * 5000),
    )
    conn.execute("INSERT INTO payment_receipt VALUES (1, '19.95')")
    conn.commit()
    conn.close()
    runner = _runner(path)
    sampled = runner.run_sql("SELECT body, scan FROM therapist_note")
    assert sampled.rows[0]["scan"].startswith("<blob")
    assert len(sampled.rows[0]["body"]) <= 201
    analyst = MockAnalyst(runner=runner, sample_id=None)
    events = list(analyst.run("what is thisdb about"))
    raw = json.dumps(events, default=str)
    assert "\\x89PNG" not in raw
    assert len(raw) < 200_000
    result = next(event["result"] for event in events if event["type"] == "result")
    assert "therapist" in result["answer"].lower()
    assert "payment" in result["answer"].lower()
    assert _is_overview("what is thisdb about")


def test_llm_timeout_falls_back_to_counts(tmp_path):
    path = tmp_path / "clinic.sqlite"
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE auth_user (id integer)")
    conn.execute("CREATE TABLE therapist_profile (id integer)")
    conn.execute("INSERT INTO auth_user VALUES (1)")
    conn.commit()
    conn.close()

    class Completions:
        def create(self, **kwargs):
            raise TimeoutError("inference timed out")

    client = SimpleNamespace(chat=SimpleNamespace(completions=Completions()))
    analyst = LlmAnalyst(runner=_runner(path), settings=_settings(), sample_id=None, client=client)
    events = list(analyst.run("what is this database about"))
    result = next(event["result"] for event in events if event["type"] == "result")
    assert "row counts" in result["answer"].lower()
    assert "auth" in result["answer"].lower()
    assert "therapist" in result["answer"].lower()
    assert not any("DROP TABLE" in (event.get("step", {}).get("sql") or "") for event in events)
