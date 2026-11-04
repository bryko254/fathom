from __future__ import annotations

import json
import sqlite3
import threading
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _id() -> str:
    return uuid.uuid4().hex[:12]


@dataclass
class Session:
    id: str
    title: str
    source_kind: str
    sample_id: str | None
    dialect: str
    db_path: str
    volume_name: str | None
    created_at: str
    connection_string: str | None = None


class Store:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._init()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        return conn

    def _init(self) -> None:
        with self._lock, self._connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS sessions (
                    id TEXT PRIMARY KEY,
                    title TEXT NOT NULL,
                    source_kind TEXT NOT NULL,
                    sample_id TEXT,
                    dialect TEXT NOT NULL,
                    db_path TEXT NOT NULL,
                    volume_name TEXT,
                    connection_string TEXT,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS messages (
                    id TEXT PRIMARY KEY,
                    session_id TEXT NOT NULL,
                    role TEXT NOT NULL,
                    content TEXT NOT NULL,
                    payload_json TEXT,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS dashboards (
                    id TEXT PRIMARY KEY,
                    session_id TEXT NOT NULL,
                    title TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS widgets (
                    id TEXT PRIMARY KEY,
                    dashboard_id TEXT NOT NULL,
                    title TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    sql TEXT NOT NULL,
                    spec_json TEXT,
                    preview_json TEXT,
                    position INTEGER NOT NULL
                );
                """
            )

    def create_session(self, **kwargs: Any) -> Session:
        session = Session(
            id=kwargs.get("id") or _id(),
            title=kwargs["title"],
            source_kind=kwargs["source_kind"],
            sample_id=kwargs.get("sample_id"),
            dialect=kwargs.get("dialect", "sqlite"),
            db_path=kwargs["db_path"],
            volume_name=kwargs.get("volume_name"),
            created_at=_now(),
            connection_string=kwargs.get("connection_string"),
        )
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                INSERT INTO sessions
                (id, title, source_kind, sample_id, dialect, db_path, volume_name, connection_string, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    session.id,
                    session.title,
                    session.source_kind,
                    session.sample_id,
                    session.dialect,
                    session.db_path,
                    session.volume_name,
                    session.connection_string,
                    session.created_at,
                ),
            )
        return session

    def get_session(self, session_id: str) -> Session | None:
        with self._lock, self._connect() as conn:
            row = conn.execute("SELECT * FROM sessions WHERE id = ?", (session_id,)).fetchone()
        return _session(row) if row else None

    def list_sessions(self) -> list[dict[str, Any]]:
        with self._lock, self._connect() as conn:
            rows = conn.execute("SELECT * FROM sessions ORDER BY created_at DESC").fetchall()
        return [_public_session(_session(row)) for row in rows]

    def add_message(self, session_id: str, role: str, content: str, payload: dict | None = None) -> dict:
        message = {
            "id": _id(),
            "sessionId": session_id,
            "role": role,
            "content": content,
            "payload": payload,
            "createdAt": _now(),
        }
        with self._lock, self._connect() as conn:
            conn.execute(
                "INSERT INTO messages (id, session_id, role, content, payload_json, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                (message["id"], session_id, role, content, json.dumps(payload) if payload else None, message["createdAt"]),
            )
        return message

    def list_messages(self, session_id: str) -> list[dict[str, Any]]:
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM messages WHERE session_id = ? ORDER BY created_at",
                (session_id,),
            ).fetchall()
        out = []
        for row in rows:
            payload = json.loads(row["payload_json"]) if row["payload_json"] else None
            out.append(
                {
                    "id": row["id"],
                    "sessionId": row["session_id"],
                    "role": row["role"],
                    "content": row["content"],
                    "payload": payload,
                    "createdAt": row["created_at"],
                }
            )
        return out

    def create_dashboard(self, session_id: str, title: str, widgets: list[dict]) -> dict:
        dashboard_id = _id()
        created = _now()
        with self._lock, self._connect() as conn:
            conn.execute(
                "INSERT INTO dashboards (id, session_id, title, created_at) VALUES (?, ?, ?, ?)",
                (dashboard_id, session_id, title, created),
            )
            for index, widget in enumerate(widgets):
                conn.execute(
                    """
                    INSERT INTO widgets
                    (id, dashboard_id, title, kind, sql, spec_json, preview_json, position)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        _id(),
                        dashboard_id,
                        widget["title"],
                        widget["kind"],
                        widget["sql"],
                        json.dumps(widget.get("spec") or {}),
                        json.dumps(widget.get("preview") or {}),
                        index,
                    ),
                )
        return self.get_dashboard(dashboard_id)  # type: ignore[return-value]

    def list_dashboards(self, session_id: str | None = None) -> list[dict[str, Any]]:
        with self._lock, self._connect() as conn:
            if session_id:
                rows = conn.execute(
                    "SELECT * FROM dashboards WHERE session_id = ? ORDER BY created_at DESC",
                    (session_id,),
                ).fetchall()
            else:
                rows = conn.execute("SELECT * FROM dashboards ORDER BY created_at DESC").fetchall()
        return [
            {"id": row["id"], "sessionId": row["session_id"], "title": row["title"], "createdAt": row["created_at"]}
            for row in rows
        ]

    def get_dashboard(self, dashboard_id: str) -> dict | None:
        with self._lock, self._connect() as conn:
            dash = conn.execute("SELECT * FROM dashboards WHERE id = ?", (dashboard_id,)).fetchone()
            if not dash:
                return None
            widgets = conn.execute(
                "SELECT * FROM widgets WHERE dashboard_id = ? ORDER BY position",
                (dashboard_id,),
            ).fetchall()
        return {
            "id": dash["id"],
            "sessionId": dash["session_id"],
            "title": dash["title"],
            "createdAt": dash["created_at"],
            "widgets": [_widget(row) for row in widgets],
        }

    def delete_session(self, session_id: str) -> None:
        with self._lock, self._connect() as conn:
            dashboards = conn.execute("SELECT id FROM dashboards WHERE session_id = ?", (session_id,)).fetchall()
            for dashboard in dashboards:
                conn.execute("DELETE FROM widgets WHERE dashboard_id = ?", (dashboard["id"],))
            conn.execute("DELETE FROM dashboards WHERE session_id = ?", (session_id,))
            conn.execute("DELETE FROM messages WHERE session_id = ?", (session_id,))
            conn.execute("DELETE FROM sessions WHERE id = ?", (session_id,))

    def update_widget_preview(self, widget_id: str, preview: dict) -> None:
        with self._lock, self._connect() as conn:
            conn.execute("UPDATE widgets SET preview_json = ? WHERE id = ?", (json.dumps(preview), widget_id))


def _session(row: sqlite3.Row) -> Session:
    return Session(
        id=row["id"],
        title=row["title"],
        source_kind=row["source_kind"],
        sample_id=row["sample_id"],
        dialect=row["dialect"],
        db_path=row["db_path"],
        volume_name=row["volume_name"],
        created_at=row["created_at"],
        connection_string=row["connection_string"],
    )


def _public_session(session: Session) -> dict[str, Any]:
    return {
        "id": session.id,
        "title": session.title,
        "sourceKind": session.source_kind,
        "sampleId": session.sample_id,
        "dialect": session.dialect,
        "createdAt": session.created_at,
        "hasConnection": bool(session.connection_string),
    }


def _widget(row: sqlite3.Row) -> dict[str, Any]:
    spec = json.loads(row["spec_json"] or "{}")
    preview = json.loads(row["preview_json"] or "{}")
    return {
        "id": row["id"],
        "title": row["title"],
        "kind": row["kind"],
        "sql": row["sql"],
        "spec": spec,
        "preview": preview,
    }
