"""Dispatch work into a fresh sandbox and destroy it when the task returns.

Docker mode is the containment boundary: read-only root, dropped capabilities,
no network, no host mounts, memory and CPU caps, and `--rm`. Subprocess mode
runs the same worker for tests and for machines without Docker. It is not the
Vultr containment boundary.
"""

from __future__ import annotations

import base64
import json
import subprocess
import sys
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from app.sql_guard import GuardDecision, guard_sql

HOST_MARKERS = ("/var/run/docker.sock", "/workspace", "/home", "bind", "type=bind")


@dataclass
class ExecResult:
    ok: bool
    error: str | None = None
    blocked: bool = False
    needs_confirmation: bool = False
    confirm_token: str | None = None
    columns: list[str] = field(default_factory=list)
    rows: list[dict[str, Any]] = field(default_factory=list)
    row_count: int = 0
    truncated: bool = False
    stdout: str = ""
    result: Any = None
    elapsed_ms: int = 0
    timed_out: bool = False
    container_name: str | None = None


def build_docker_args(
    *,
    image: str,
    name: str,
    memory: str,
    cpus: str,
    volume: str | None = None,
    volume_readonly: bool = True,
    user: str | None = "65534:65534",
    drill: bool = False,
) -> list[str]:
    """Argv for one ephemeral sandbox container. Host paths are refused."""
    if volume and ("/" in volume or volume.startswith(".")):
        raise ValueError("sandbox refuses host paths; pass a docker volume name")
    args = [
        "docker",
        "run",
        "--rm",
        "-i",
        "--name",
        name,
        "--network",
        "none",
        "--read-only",
        "--cap-drop",
        "ALL",
        "--security-opt",
        "no-new-privileges:true",
        "--pids-limit",
        "64",
        "--memory",
        memory,
        "--memory-swap",
        memory,
        "--cpus",
        str(cpus),
        "--tmpfs",
        "/tmp:rw,nosuid,nodev,size=64m",
        "--label",
        "fathom.role=sandbox",
    ]
    if user:
        args.extend(["--user", user])
    if volume:
        mount = f"type=volume,src={volume},dst=/data"
        if volume_readonly:
            mount += ",readonly"
        args.extend(["--mount", mount])
    if drill:
        args.extend(["--env", "FATHOM_DRILL=1"])
    args.append(image)
    joined = " ".join(args)
    for marker in ("docker.sock", "type=bind"):
        if marker in joined:
            raise ValueError(f"sandbox argv tried to mount the host ({marker})")
    return args


class SandboxRunner:
    def __init__(
        self,
        *,
        mode: str,
        worker_path: Path,
        image: str = "fathom-sandbox:local",
        timeout_s: int = 8,
        max_rows: int = 500,
        memory: str = "256m",
        cpus: str = "0.5",
        db_path: str | None = None,
        volume: str | None = None,
    ) -> None:
        self.mode = mode
        self.worker_path = worker_path
        self.image = image
        self.timeout_s = timeout_s
        self.max_rows = max_rows
        self.memory = memory
        self.cpus = cpus
        self.db_path = db_path
        self.volume = volume

    def run_sql(self, sql: str, *, confirm_token: str | None = None, dialect: str = "sqlite") -> ExecResult:
        decision = guard_sql(sql, dialect=dialect)
        if decision.kind == "blocked":
            return ExecResult(ok=False, blocked=True, error=decision.reason)
        if decision.kind == "write":
            if confirm_token != decision.token:
                return ExecResult(
                    ok=False,
                    blocked=True,
                    needs_confirmation=True,
                    confirm_token=decision.token,
                    error=decision.reason,
                )
        started = time.perf_counter()
        job = {
            "op": "sql",
            "sql": sql,
            "db_path": "/data/db.sqlite" if self.mode == "docker" else self.db_path,
            "max_rows": self.max_rows,
            "timeout_s": self.timeout_s,
            "writable": decision.kind == "write",
        }
        payload = self._invoke(
            job,
            volume_readonly=decision.kind != "write",
            user="0:0" if decision.kind == "write" else "65534:65534",
        )
        return self._to_result(payload, started)

    def run_python(self, code: str, *, input_data: Any = None, timeout_s: int | None = None) -> ExecResult:
        started = time.perf_counter()
        job = {
            "op": "python",
            "code": code,
            "input": input_data,
            "timeout_s": timeout_s or self.timeout_s,
            "unrestricted": False,
        }
        payload = self._invoke(job, volume_readonly=True, user="65534:65534", timeout_s=job["timeout_s"])
        return self._to_result(payload, started)

    def seed_volume(self, volume: str, sqlite_bytes: bytes) -> ExecResult:
        if self.mode != "docker":
            return ExecResult(ok=True, stdout="subprocess seed is a file copy")
        started = time.perf_counter()
        job = {
            "op": "seed",
            "db_path": "/data/db.sqlite",
            "sqlite_b64": base64.b64encode(sqlite_bytes).decode("ascii"),
            "timeout_s": max(self.timeout_s, 20),
        }
        payload = self._invoke(job, volume=volume, volume_readonly=False, user="0:0", timeout_s=job["timeout_s"])
        return self._to_result(payload, started)

    def create_volume(self, name: str) -> None:
        if self.mode != "docker":
            return
        subprocess.run(
            ["docker", "volume", "create", "--label", "fathom.role=session", name],
            check=True,
            capture_output=True,
            text=True,
        )

    def remove_volume(self, name: str | None) -> None:
        if self.mode != "docker" or not name:
            return
        subprocess.run(["docker", "volume", "rm", "-f", name], capture_output=True, text=True)

    def run_drill_python(self, code: str, *, timeout_s: int) -> ExecResult:
        started = time.perf_counter()
        job = {
            "op": "python",
            "code": code,
            "input": None,
            "timeout_s": timeout_s,
            "unrestricted": True,
        }
        payload = self._invoke(
            job,
            volume_readonly=True,
            user="65534:65534",
            drill=True,
            timeout_s=timeout_s,
            mount_volume=False,
        )
        return self._to_result(payload, started)

    def _invoke(
        self,
        job: dict,
        *,
        volume: str | None = None,
        volume_readonly: bool = True,
        user: str | None = "65534:65534",
        drill: bool = False,
        timeout_s: int | None = None,
        mount_volume: bool = True,
    ) -> dict:
        timeout_s = timeout_s or self.timeout_s
        raw = json.dumps(job).encode("utf-8")
        chosen = (volume if volume is not None else self.volume) if mount_volume else None
        if self.mode == "docker":
            return self._docker(
                raw,
                volume=chosen,
                volume_readonly=volume_readonly,
                user=user,
                drill=drill,
                timeout_s=timeout_s,
            )
        return self._subprocess(raw, timeout_s=timeout_s, drill=drill)

    def _subprocess(self, raw: bytes, *, timeout_s: int, drill: bool) -> dict:
        env = {
            "PATH": "/usr/bin:/bin",
            "HOME": "/tmp",
            "TMPDIR": "/tmp",
            "PYTHONDONTWRITEBYTECODE": "1",
            "FATHOM_DRILL": "1" if drill else "0",
        }
        try:
            proc = subprocess.run(
                [sys.executable, str(self.worker_path)],
                input=raw,
                capture_output=True,
                timeout=timeout_s + 2,
                env=env,
                cwd="/tmp",
            )
        except subprocess.TimeoutExpired:
            return {"ok": False, "error": "sandbox time limit exceeded", "timeout": True}
        return _parse_output(proc.stdout, proc.stderr)

    def _docker(
        self,
        raw: bytes,
        *,
        volume: str | None,
        volume_readonly: bool,
        user: str | None,
        drill: bool,
        timeout_s: int,
    ) -> dict:
        name = f"fathom-sbx-{uuid.uuid4().hex[:12]}"
        args = build_docker_args(
            image=self.image,
            name=name,
            memory=self.memory,
            cpus=self.cpus,
            volume=volume,
            volume_readonly=volume_readonly,
            user=user,
            drill=drill,
        )
        try:
            proc = subprocess.run(
                args,
                input=raw,
                capture_output=True,
                timeout=timeout_s + 3,
            )
        except subprocess.TimeoutExpired:
            subprocess.run(["docker", "rm", "-f", name], capture_output=True)
            return {"ok": False, "error": "sandbox time limit exceeded", "timeout": True, "container": name}
        except FileNotFoundError:
            return {"ok": False, "error": "docker CLI is not installed on the control plane"}
        return _parse_output(proc.stdout, proc.stderr, container=name)

    def _to_result(self, payload: dict, started: float) -> ExecResult:
        elapsed = int((time.perf_counter() - started) * 1000)
        if not payload.get("ok"):
            return ExecResult(
                ok=False,
                error=str(payload.get("error") or "sandbox failed"),
                stdout=str(payload.get("stdout") or ""),
                elapsed_ms=elapsed,
                timed_out=bool(payload.get("timeout")),
                container_name=payload.get("container"),
            )
        rows = payload.get("rows") or []
        return ExecResult(
            ok=True,
            columns=list(payload.get("columns") or []),
            rows=rows,
            row_count=int(payload.get("row_count") if payload.get("row_count") is not None else len(rows)),
            truncated=bool(payload.get("truncated")),
            stdout=str(payload.get("stdout") or ""),
            result=payload.get("result"),
            elapsed_ms=elapsed,
            container_name=payload.get("container"),
        )


def _parse_output(stdout: bytes, stderr: bytes, container: str | None = None) -> dict:
    text = stdout.decode("utf-8", "replace").strip()
    for line in reversed(text.splitlines()):
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            continue
        if container:
            payload["container"] = container
        return payload
    err = stderr.decode("utf-8", "replace").strip()
    hint = err[-800:] or text[-800:] or "sandbox produced no output"
    return {"ok": False, "error": hint, "container": container}


def decision_for(sql: str) -> GuardDecision:
    return guard_sql(sql)
