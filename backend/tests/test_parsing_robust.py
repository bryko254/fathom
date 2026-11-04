import sqlite3
from pathlib import Path

import pytest

from app.llm_agent import _parse_action
from app.mock_agent import parse_create_columns


def test_parse_action_takes_sql_object_from_many():
    raw = '{"action":"sample","table":"x"}\n{"action":"run_sql","sql":"SELECT 1"}\nthanks'
    assert _parse_action(raw)["sql"] == "SELECT 1"


def test_parse_action_strips_fences_and_reasoning():
    raw = '<think>maybe {bad}</think>```json\n{"action":"finish","answer":"a"}\n``` done'
    assert _parse_action(raw) == {"action": "finish", "answer": "a"}


def test_parse_action_without_json_raises_value_error():
    with pytest.raises(ValueError):
        _parse_action("no json here")


def test_parse_create_columns_django_style():
    sql = '''CREATE TABLE "client_clientprofile" (
        "id" integer NOT NULL PRIMARY KEY AUTOINCREMENT,
        "phone" varchar(20) NULL CHECK ("phone" <> ''),
        `user_id` integer NOT NULL UNIQUE REFERENCES "auth_user" ("id") DEFERRABLE INITIALLY DEFERRED,
        [weird name] text,
        CONSTRAINT pk UNIQUE ("id", "phone"),
        FOREIGN KEY ("user_id") REFERENCES "auth_user" ("id")
    )'''
    assert parse_create_columns(sql) == ["id", "phone", "user_id", "weird name"]


def test_schema_only_db_all_tables_have_columns():
    path = Path("/workspace/schema_only.sqlite")
    if not path.exists():
        pytest.skip("fixture not present")
    conn = sqlite3.connect(path)
    rows = conn.execute("SELECT name, sql FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'").fetchall()
    for name, sql in rows:
        expected = [r[1] for r in conn.execute(f'PRAGMA table_info("{name}")')]
        assert parse_create_columns(sql) == expected, name
