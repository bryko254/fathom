from __future__ import annotations

import json
import logging
import shutil
import sqlite3
import threading
import uuid
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from app.config import Settings, load_settings
from app.drill import run_drill
from app.llm_agent import LlmAnalyst
from app.mock_agent import MockAnalyst
from app.samples import CATALOG, ensure_samples
from app.sandbox import SandboxRunner
from app.snapshot import snapshot_connection
from app.sql_import import SqlImportError, load_sql_dump
from app.store import Store

logger = logging.getLogger("fathom")
settings: Settings = load_settings()
store = Store(settings.data_dir / "app.sqlite")
samples: dict[str, Path] = {}
locks: dict[str, threading.Lock] = {}


@asynccontextmanager
async def lifespan(_app: FastAPI):
    global samples
    samples = ensure_samples(settings.data_dir / "samples")
    yield


app = FastAPI(title="Fathom", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


class SessionIn(BaseModel):
    source: str
    sampleId: str | None = None
    connectionString: str | None = None
    name: str | None = None


class AskIn(BaseModel):
    question: str = Field(min_length=1, max_length=4000)


class ExecuteIn(BaseModel):
    sql: str
    confirmToken: str


class RefreshIn(BaseModel):
    resync: bool = False


@app.get("/health")
def health() -> dict:
    return {
        "ok": True,
        "sandbox": settings.sandbox_mode,
        "provider": settings.provider,
        "model": settings.llm_model,
        "baseUrl": settings.llm_base_url,
        "inferenceConfigured": settings.provider == "openai",
        "ledgerClock": "2026-11-04",
        "maxRows": settings.max_rows,
        "queryTimeoutS": settings.query_timeout_s,
    }


@app.get("/samples")
def list_samples() -> dict:
    return {"samples": CATALOG, "ledgerClock": "2026-11-04"}


@app.get("/sessions")
def list_sessions() -> dict:
    return {"sessions": store.list_sessions()}


@app.post("/sessions")
def create_session(body: SessionIn) -> dict:
    session_id = uuid.uuid4().hex[:12]
    if body.source == "sample":
        if body.sampleId not in samples:
            raise HTTPException(404, "Unknown sample ledger.")
        meta = next(item for item in CATALOG if item["id"] == body.sampleId)
        db_path, volume = _materialize(samples[body.sampleId], session_id)
        session = store.create_session(
            id=session_id,
            title=body.name or meta["name"],
            source_kind="sample",
            sample_id=body.sampleId,
            dialect="sqlite",
            db_path=db_path,
            volume_name=volume,
        )
    elif body.source == "connection":
        if not body.connectionString:
            raise HTTPException(400, "A connection string is required.")
        dest = settings.data_dir / "sessions" / f"{session_id}.sqlite"
        try:
            info = snapshot_connection(body.connectionString, dest, row_cap=settings.snapshot_row_cap)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        volume = _seed_volume(session_id, dest)
        session = store.create_session(
            id=session_id,
            title=body.name or "Connected database",
            source_kind="connection",
            sample_id=None,
            dialect="sqlite",
            db_path=str(dest),
            volume_name=volume,
            connection_string=body.connectionString,
        )
        return {"session": _public(session), "snapshot": {"tables": info["tables"], "rowCap": info["rowCap"]}}
    else:
        raise HTTPException(400, "source must be sample or connection. Upload a file to /sessions/upload.")
    return {"session": _public(session)}


@app.post("/sessions/upload")
async def upload_session(file: UploadFile = File(...), name: str | None = None) -> dict:
    raw = await file.read()
    if len(raw) > 25 * 1024 * 1024:
        raise HTTPException(400, "Files are limited to 25 MB.")
    session_id = uuid.uuid4().hex[:12]
    dest = settings.data_dir / "sessions" / f"{session_id}.sqlite"
    dest.parent.mkdir(parents=True, exist_ok=True)
    filename = (file.filename or "upload").lower()
    try:
        if filename.endswith((".sql", ".txt")):
            load_sql_dump(raw.decode("utf-8", "replace"), dest)
        else:
            dest.write_bytes(raw)
            _assert_sqlite(dest)
    except SqlImportError as exc:
        if dest.exists():
            dest.unlink()
        raise HTTPException(400, str(exc)) from exc
    except Exception as exc:  # noqa: BLE001
        if dest.exists():
            dest.unlink()
        raise HTTPException(400, f"Could not open that file as a database: {exc}") from exc
    volume = _seed_volume(session_id, dest)
    session = store.create_session(
        id=session_id,
        title=name or file.filename or "Uploaded ledger",
        source_kind="upload",
        sample_id=None,
        dialect="sqlite",
        db_path=str(dest),
        volume_name=volume,
    )
    return {"session": _public(session)}


@app.get("/sessions/{session_id}")
def get_session(session_id: str) -> dict:
    session = store.get_session(session_id)
    if not session:
        raise HTTPException(404, "Session not found.")
    return {
        "session": _public(session),
        "messages": store.list_messages(session_id),
        "dashboards": store.list_dashboards(session_id),
    }


@app.delete("/sessions/{session_id}")
def delete_session(session_id: str) -> dict:
    session = store.get_session(session_id)
    if not session:
        raise HTTPException(404, "Session not found.")
    _runner(session).remove_volume(session.volume_name)
    path = Path(session.db_path)
    if path.exists():
        path.unlink()
    store.delete_session(session_id)
    return {"ok": True}


@app.post("/sessions/{session_id}/ask")
def ask(session_id: str, body: AskIn) -> StreamingResponse:
    session = store.get_session(session_id)
    if not session:
        raise HTTPException(404, "Session not found.")

    def generate():
        lock = locks.setdefault(session_id, threading.Lock())
        if not lock.acquire(blocking=False):
            yield _sse({"type": "error", "error": "This ledger is already running a question."})
            return
        try:
            store.add_message(session_id, "user", body.question)
            analyst = _analyst(session)
            model_label = settings.llm_model if settings.provider == "openai" else "fallback"
            yield _sse(
                {
                    "type": "step",
                    "step": {
                        "id": "provider",
                        "kind": "plan",
                        "title": "Analyst",
                        "status": "ok",
                        "detail": f"provider={settings.provider} model={model_label}",
                    },
                }
            )
            result_payload = None
            steps = []
            for event in analyst.run(body.question):
                if event["type"] == "step":
                    steps.append(event["step"])
                if event["type"] == "result":
                    result_payload = event["result"]
                    result_payload["provider"] = settings.provider
                    result_payload["model"] = settings.llm_model if settings.provider == "openai" else None
                    logger.info(
                        "answer provider=%s model=%s sample=%s",
                        result_payload["provider"],
                        result_payload["model"],
                        session.sample_id,
                    )
                    dashboard = result_payload.get("dashboard")
                    if dashboard and dashboard.get("widgets"):
                        dashboard["widgets"] = _hydrate_metrics(session, dashboard["widgets"])
                        saved = store.create_dashboard(session_id, dashboard["title"], dashboard["widgets"])
                        result_payload["dashboard"] = saved
                        event["result"] = result_payload
                yield _sse(event)
            if result_payload:
                store.add_message(session_id, "assistant", result_payload.get("answer") or "", {"steps": _final_steps(steps), "result": result_payload})
            yield _sse({"type": "done"})
        except Exception as exc:  # noqa: BLE001
            yield _sse({"type": "error", "error": str(exc)})
        finally:
            lock.release()

    return _stream(generate())


@app.post("/sessions/{session_id}/execute")
def execute(session_id: str, body: ExecuteIn) -> dict:
    session = store.get_session(session_id)
    if not session:
        raise HTTPException(404, "Session not found.")
    result = _runner(session).run_sql(body.sql, confirm_token=body.confirmToken)
    if result.needs_confirmation:
        raise HTTPException(409, "Confirmation token does not match this statement.")
    if result.blocked and not result.ok:
        raise HTTPException(400, result.error or "Statement blocked.")
    if not result.ok:
        raise HTTPException(400, result.error or "Statement failed.")
    return {"ok": True, "columns": result.columns, "rows": result.rows, "elapsedMs": result.elapsed_ms}


@app.get("/dashboards")
def list_dashboards() -> dict:
    return {"dashboards": store.list_dashboards()}


@app.get("/dashboards/{dashboard_id}")
def get_dashboard(dashboard_id: str) -> dict:
    dashboard = store.get_dashboard(dashboard_id)
    if not dashboard:
        raise HTTPException(404, "Dashboard not found.")
    return {"dashboard": dashboard}


@app.post("/dashboards/{dashboard_id}/refresh")
def refresh_dashboard(dashboard_id: str, body: RefreshIn | None = None) -> dict:
    dashboard = store.get_dashboard(dashboard_id)
    if not dashboard:
        raise HTTPException(404, "Dashboard not found.")
    session = store.get_session(dashboard["sessionId"])
    if not session:
        raise HTTPException(404, "Session not found.")
    body = body or RefreshIn()
    if body.resync and session.connection_string:
        info = snapshot_connection(session.connection_string, Path(session.db_path), row_cap=settings.snapshot_row_cap)
        if session.volume_name:
            _runner(session).seed_volume(session.volume_name, Path(session.db_path).read_bytes())
        del info
    elif body.resync and session.sample_id and session.sample_id in samples:
        shutil.copyfile(samples[session.sample_id], session.db_path)
        if session.volume_name:
            _runner(session).seed_volume(session.volume_name, Path(session.db_path).read_bytes())
    runner = _runner(session)
    widgets = []
    for widget in dashboard["widgets"]:
        result = runner.run_sql(widget["sql"])
        preview = {
            "columns": result.columns,
            "rows": result.rows,
            "error": None if result.ok else result.error,
            "elapsedMs": result.elapsed_ms,
        }
        if widget["kind"] == "metric" and result.ok and result.rows:
            preview["value"] = next(iter(result.rows[0].values()))
        store.update_widget_preview(widget["id"], preview)
        widgets.append({**widget, "preview": preview})
    dashboard["widgets"] = widgets
    return {"dashboard": dashboard}


@app.post("/containment")
def containment() -> StreamingResponse:
    runner = SandboxRunner(
        mode=settings.sandbox_mode,
        worker_path=settings.worker_path,
        image=settings.sandbox_image,
        timeout_s=settings.query_timeout_s,
        max_rows=settings.max_rows,
        memory=settings.sandbox_memory,
        cpus=settings.sandbox_cpus,
        db_path=None,
        volume=None,
    )

    def generate():
        yield _sse({"type": "step", "step": {"id": "mode", "kind": "guard", "title": "Sandbox mode", "status": "ok", "detail": settings.sandbox_mode}})
        try:
            for event in run_drill(runner):
                yield _sse(event)
        except Exception as exc:  # noqa: BLE001
            yield _sse({"type": "error", "error": str(exc)})
        yield _sse({"type": "done"})

    return _stream(generate())


def _hydrate_metrics(session, widgets: list[dict]) -> list[dict]:
    """Fill metric tiles the analyst did not already compute, using the same sandbox."""
    runner = None
    hydrated = []
    for widget in widgets:
        preview = dict(widget.get("preview") or {})
        if widget.get("kind") == "metric" and preview.get("value") is None and widget.get("sql"):
            runner = runner or _runner(session)
            result = runner.run_sql(widget["sql"])
            if result.ok and result.rows:
                preview["value"] = next(iter(result.rows[0].values()))
                preview["error"] = None
            elif not result.ok:
                preview["error"] = result.error
        hydrated.append({**widget, "preview": preview})
    return hydrated


def _analyst(session):
    runner = _runner(session)
    if settings.provider == "openai":
        return LlmAnalyst(runner=runner, settings=settings, sample_id=session.sample_id)
    return MockAnalyst(runner=runner, sample_id=session.sample_id)


def _runner(session) -> SandboxRunner:
    return SandboxRunner(
        mode=settings.sandbox_mode,
        worker_path=settings.worker_path,
        image=settings.sandbox_image,
        timeout_s=settings.query_timeout_s,
        max_rows=settings.max_rows,
        memory=settings.sandbox_memory,
        cpus=settings.sandbox_cpus,
        db_path=session.db_path,
        volume=session.volume_name,
    )


def _materialize(source: Path, session_id: str) -> tuple[str, str | None]:
    dest = settings.data_dir / "sessions" / f"{session_id}.sqlite"
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(source, dest)
    return str(dest), _seed_volume(session_id, dest)


def _seed_volume(session_id: str, dest: Path) -> str | None:
    if settings.sandbox_mode != "docker":
        return None
    volume = f"fathom-sess-{session_id}"
    runner = SandboxRunner(
        mode="docker",
        worker_path=settings.worker_path,
        image=settings.sandbox_image,
        timeout_s=max(settings.query_timeout_s, 20),
        max_rows=settings.max_rows,
        memory=settings.sandbox_memory,
        cpus=settings.sandbox_cpus,
    )
    runner.create_volume(volume)
    seeded = runner.seed_volume(volume, dest.read_bytes())
    if not seeded.ok:
        runner.remove_volume(volume)
        raise HTTPException(500, f"Could not seed the sandbox volume: {seeded.error}")
    return volume


def _assert_sqlite(path: Path) -> None:
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        conn.execute("SELECT name FROM sqlite_master LIMIT 1")
    finally:
        conn.close()


def _public(session) -> dict:
    return {
        "id": session.id,
        "title": session.title,
        "sourceKind": session.source_kind,
        "sampleId": session.sample_id,
        "dialect": session.dialect,
        "createdAt": session.created_at,
        "hasConnection": bool(session.connection_string),
    }


def _final_steps(steps: list[dict]) -> list[dict]:
    latest: dict[str, dict] = {}
    order: list[str] = []
    for step in steps:
        if step["id"] not in latest:
            order.append(step["id"])
        latest[step["id"]] = step
    return [latest[key] for key in order]


def _sse(payload: dict) -> str:
    return f"data: {json.dumps(payload, default=str)}\n\n"


def _stream(chunks) -> StreamingResponse:
    return StreamingResponse(
        chunks,
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
