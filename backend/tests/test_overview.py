import sqlite3
from types import SimpleNamespace

from app.config import Settings
from app.llm_agent import LlmAnalyst, _complete, message_text
from app.mock_agent import MockAnalyst
from app.sandbox import SandboxRunner


def _settings() -> Settings:
    return Settings(
        llm_base_url="https://api.vultrinference.com/v1",
        llm_api_key="test-key",
        llm_model="deepseek-v4.1-flash-1017",
        llm_provider="openai",
        sandbox_mode="subprocess",
        sandbox_image="fathom-sandbox:local",
        query_timeout_s=8,
        max_rows=500,
        sandbox_memory="128m",
        sandbox_cpus="0.5",
        data_dir=__import__("pathlib").Path("/tmp"),
        snapshot_row_cap=100,
    )


def _app_db(tmp_path):
    path = tmp_path / "clinic.sqlite"
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE auth_user (id integer primary key, username text);
        CREATE TABLE therapist_profile (id integer primary key, name text);
        CREATE TABLE moodtracker_entry (id integer primary key, score integer);
        CREATE TABLE payment_invoice (id integer primary key, amount integer);
        INSERT INTO auth_user VALUES (1, 'ada');
        INSERT INTO therapist_profile VALUES (1, 'Kamau');
        INSERT INTO moodtracker_entry VALUES (1, 4), (2, 6);
        INSERT INTO payment_invoice VALUES (1, 2500);
        """
    )
    conn.commit()
    conn.close()
    return path


def _runner(db):
    return SandboxRunner(
        mode="subprocess",
        worker_path=_settings().worker_path,
        timeout_s=8,
        max_rows=500,
        db_path=str(db),
    )


def _final(events):
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


def test_uploaded_overview_counts_domains_and_skips_scripted_probes(tmp_path):
    analyst = MockAnalyst(runner=_runner(_app_db(tmp_path)), sample_id=None)
    events = list(analyst.run("what is on this db"))
    steps = _final(events)
    sql_text = " ".join(step.get("sql") or "" for step in steps)
    assert "DROP TABLE" not in sql_text
    assert "COUNT(*)" in sql_text
    assert "sqlite_master" not in sql_text.lower() or any(
        step["kind"] == "explore" and step["columns"] == ["table", "columns"] for step in steps
    )
    explore = next(step for step in steps if step["kind"] == "explore" and step["status"] == "ok")
    assert "sql" not in explore["columns"]
    result = next(event["result"] for event in events if event["type"] == "result")
    answer = result["answer"].lower()
    assert "auth" in answer and "therapist" in answer
    assert "mood" in answer and "payment" in answer
    assert "2" in answer
    assert result["table"]["columns"] == ["domain", "tables", "row_count"]
    domains = {row["domain"] for row in result["table"]["rows"]}
    assert {"auth", "therapist", "moodtracker", "payment"} <= domains


def test_demo_ledger_still_runs_the_scripted_probe(tmp_path):
    from app.clock import LEDGER_TODAY
    from app.samples import ensure_samples

    db = ensure_samples(tmp_path / "samples")["harbor"]
    analyst = MockAnalyst(runner=_runner(db), sample_id="harbor", as_of=LEDGER_TODAY)
    events = list(analyst.run("Which customers churned last quarter and why?"))
    steps = _final(events)
    assert any("DROP TABLE" in (step.get("sql") or "") and step["status"] == "blocked" for step in steps)


def test_message_text_falls_back_to_reasoning():
    assert message_text(SimpleNamespace(content="  ", reasoning_content='{"action":"finish","answer":"hi"}')) == (
        '{"action":"finish","answer":"hi"}'
    )
    assert message_text({"content": "from content", "reasoning": "ignored"}) == "from content"


def test_empty_length_response_retries_with_more_tokens():
    calls = []

    class Completions:
        def create(self, **kwargs):
            calls.append(kwargs)
            if len(calls) == 1:
                return SimpleNamespace(
                    choices=[SimpleNamespace(message=SimpleNamespace(content="", reasoning_content=""), finish_reason="length")]
                )
            return SimpleNamespace(
                choices=[
                    SimpleNamespace(
                        message=SimpleNamespace(content="", reasoning_content='{"action":"finish","answer":"Four domains."}'),
                        finish_reason="stop",
                    )
                ]
            )

    client = SimpleNamespace(chat=SimpleNamespace(completions=Completions()))
    action, raw = _complete(client, "deepseek-v4.1-flash-1017", [{"role": "user", "content": "overview"}])
    assert action["answer"] == "Four domains."
    assert calls[0]["max_tokens"] >= 8192
    assert calls[1]["max_tokens"] > calls[0]["max_tokens"]
    assert "Four domains" in raw


def test_llm_overview_uses_the_model_and_not_a_scripted_drop(tmp_path):
    payload = '{"action":"finish","answer":"The app is a clinic tool: auth for sign-in, therapist records, mood tracking, and payments."}'

    class Completions:
        def create(self, **kwargs):
            return SimpleNamespace(
                choices=[SimpleNamespace(message=SimpleNamespace(content=payload, reasoning_content=None), finish_reason="stop")]
            )

    client = SimpleNamespace(chat=SimpleNamespace(completions=Completions()))
    analyst = LlmAnalyst(runner=_runner(_app_db(tmp_path)), settings=_settings(), sample_id=None, client=client)
    events = list(analyst.run("what is on this db"))
    steps = _final(events)
    assert not any("DROP TABLE" in (step.get("sql") or "") for step in steps)
    assert any("COUNT(*)" in (step.get("sql") or "") for step in steps)
    result = next(event["result"] for event in events if event["type"] == "result")
    assert result["answer"].startswith("The app is a clinic tool")
    assert result["provider"] == "openai"
    assert result["model"] == "deepseek-v4.1-flash-1017"
