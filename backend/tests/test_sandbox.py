import sqlite3
from pathlib import Path

import pytest

from app.config import load_settings
from app.sandbox import SandboxRunner, build_docker_args
from app.sanity import assess
from app.sandbox import ExecResult


def _runner(db: Path, **kwargs) -> SandboxRunner:
    settings = load_settings()
    return SandboxRunner(
        mode="subprocess",
        worker_path=settings.worker_path,
        timeout_s=kwargs.get("timeout_s", 8),
        max_rows=kwargs.get("max_rows", 5),
        db_path=str(db),
    )


def _db(tmp_path: Path) -> Path:
    path = tmp_path / "t.sqlite"
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE accounts (id INTEGER PRIMARY KEY, name TEXT)")
    conn.executemany("INSERT INTO accounts VALUES (?, ?)", [(1, "A"), (2, "B"), (3, None)])
    conn.commit()
    conn.execute("PRAGMA journal_mode=DELETE")
    conn.close()
    return path


def test_select_runs_and_respects_row_cap(tmp_path):
    db = _db(tmp_path)
    result = _runner(db, max_rows=2).run_sql("SELECT id, name FROM accounts ORDER BY id")
    assert result.ok
    assert result.row_count == 2
    assert result.truncated
    assert result.rows[0]["name"] == "A"


def test_drop_never_reaches_the_worker(tmp_path):
    db = _db(tmp_path)
    result = _runner(db).run_sql("DROP TABLE accounts")
    assert result.blocked
    assert not result.ok
    conn = sqlite3.connect(db)
    assert conn.execute("SELECT COUNT(*) FROM accounts").fetchone()[0] == 3
    conn.close()


def test_write_requires_the_matching_token(tmp_path):
    db = _db(tmp_path)
    sql = "UPDATE accounts SET name = 'Z' WHERE id = 1"
    runner = _runner(db)
    blocked = runner.run_sql(sql)
    assert blocked.needs_confirmation
    conn = sqlite3.connect(db)
    assert conn.execute("SELECT name FROM accounts WHERE id = 1").fetchone()[0] == "A"
    conn.close()
    applied = runner.run_sql(sql, confirm_token=blocked.confirm_token)
    assert applied.ok, applied.error
    conn = sqlite3.connect(db)
    assert conn.execute("SELECT name FROM accounts WHERE id = 1").fetchone()[0] == "Z"
    conn.close()


def test_python_computes_and_blocks_os(tmp_path):
    db = _db(tmp_path)
    runner = _runner(db)
    good = runner.run_python("RESULT = sum(r['id'] for r in INPUT['rows'])\nprint(RESULT)\n", input_data={"rows": [{"id": 1}, {"id": 2}]})
    assert good.ok, good.error
    assert good.result == 3
    bad = runner.run_python("import os\nos.system('echo no')\n")
    assert not bad.ok
    assert "os" in (bad.error or "")


def test_python_timeout(tmp_path):
    db = _db(tmp_path)
    result = _runner(db, timeout_s=1).run_python("while True:\n    pass\n", timeout_s=1)
    assert not result.ok
    assert result.timed_out or "time" in (result.error or "").lower()


def test_worker_sqlite_rejects_write_when_read_only(tmp_path):
    db = _db(tmp_path)
    import importlib.util

    spec = importlib.util.spec_from_file_location("fathom_worker", load_settings().worker_path)
    worker = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(worker)
    payload = worker.op_sql({"sql": "DELETE FROM accounts", "db_path": str(db), "writable": False, "max_rows": 10})
    assert not payload["ok"]


def test_docker_argv_has_no_host_access():
    args = build_docker_args(image="fathom-sandbox:local", name="fathom-sbx-test", memory="256m", cpus="0.5", volume="fathom-sess-abc")
    text = " ".join(args)
    assert "--network none" in text
    assert "--read-only" in text
    assert "--cap-drop ALL" in text
    assert "--rm" in text
    assert "docker.sock" not in text
    assert "type=bind" not in text
    assert "type=volume,src=fathom-sess-abc,dst=/data,readonly" in text
    writable = build_docker_args(
        image="fathom-sandbox:local",
        name="fathom-sbx-write",
        memory="128m",
        cpus="0.5",
        volume="fathom-sess-abc",
        volume_readonly=False,
    )
    assert ",rw" not in " ".join(writable)
    assert "dst=/data" in " ".join(writable)
    with pytest.raises(ValueError):
        build_docker_args(image="fathom-sandbox:local", name="x", memory="256m", cpus="0.5", volume="/tmp/host")


def test_sanity_flags_fanout_and_empty():
    empty = assess("SELECT 1", ExecResult(ok=True, rows=[], row_count=0))
    assert any("zero" in item for item in empty)
    rows = [{"account_id": 1, "event": "login"} for _ in range(25)]
    fanout = assess("SELECT * FROM a JOIN b", ExecResult(ok=True, columns=["account_id", "event"], rows=rows, row_count=25))
    assert any("fanout" in item for item in fanout)
