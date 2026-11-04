"""Containment drill.

Each check is a real action: a statement the guard refuses, or Python that
tries to escape the sandbox. Docker mode should pass every check. Subprocess
mode is expected to fail the host and network checks, and the UI says so.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from pathlib import Path

from app.sandbox import SandboxRunner


def run_drill(runner: SandboxRunner) -> Iterator[dict]:
    marker = Path(f"/tmp/fathom-host-marker-{uuid.uuid4().hex}")
    escape = Path(f"/tmp/fathom-escape-{uuid.uuid4().hex}")
    marker.write_text("host-only-marker")
    try:
        blocked = runner.run_sql("DROP TABLE accounts")
        yield _check(
            "drop",
            "DROP TABLE never reaches the sandbox",
            blocked.blocked and not blocked.ok,
            blocked.error or "guard did not block the statement",
        )
        extension = runner.run_sql("SELECT load_extension('libevil')")
        yield _check(
            "extension",
            "load_extension is refused",
            extension.blocked and not extension.ok,
            extension.error or "function call was not blocked",
        )
        loop = runner.run_drill_python("while True:\n    pass\n", timeout_s=2)
        yield _check(
            "loop",
            "An infinite loop is killed by the time limit",
            loop.timed_out or (not loop.ok and "time" in (loop.error or "").lower()),
            loop.error or "loop returned",
        )
        read = runner.run_drill_python(f"print(open({str(marker)!r}).read())\n", timeout_s=3)
        leaked = marker.read_text() in (read.stdout or "")
        yield _check(
            "host-read",
            "A host-only file cannot be read",
            not leaked,
            "the sandbox printed the host marker" if leaked else (read.error or "file was not visible"),
        )
        write = runner.run_drill_python(
            f"open({str(escape)!r}, 'w').write('sandbox-write')\nprint('wrote')\n",
            timeout_s=3,
        )
        yield _check(
            "host-write",
            "A write does not appear on the host",
            not escape.exists(),
            "host path was created" if escape.exists() else (write.error or "write stayed inside the sandbox"),
        )
        network = runner.run_drill_python(
            "import socket\n"
            "s = socket.socket()\n"
            "s.settimeout(2)\n"
            "s.connect(('1.1.1.1', 80))\n"
            "print('connected')\n",
            timeout_s=4,
        )
        connected = network.ok and "connected" in (network.stdout or "")
        yield _check(
            "network",
            "Outbound network is closed",
            not connected,
            "opened a socket to 1.1.1.1" if connected else (network.error or "connection failed"),
        )
    finally:
        marker.unlink(missing_ok=True)
        if escape.exists():
            escape.unlink()


def _check(check_id: str, title: str, passed: bool, detail: str) -> dict:
    return {
        "type": "drill",
        "check": {"id": check_id, "title": title, "passed": passed, "detail": detail},
    }
