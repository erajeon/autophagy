"""Mail-wrapper and calendar subprocess transport for the triage CLI."""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path

import triage_core
import triage_gate

SCRIPT_DIR = Path(__file__).resolve().parent
SKILL_DIR = SCRIPT_DIR.parent


def _env_path(name: str, default: str) -> Path:
    return Path(os.environ.get(name, default)).expanduser()


def _rules_path() -> Path:
    return _env_path("TRIAGE_RULES_FILE", str(SKILL_DIR / "configs/sensitivity-rules.yaml"))


def _wrapper_json(argv: list[str], *, timeout: int = 1200) -> tuple[int, dict]:
    proc = subprocess.run(  # noqa: S603 — fixed wrapper path, read-only surface
        [sys.executable, str(SCRIPT_DIR / "mail_wrapper.py"), *argv],
        capture_output=True, text=True, timeout=timeout, check=False,
    )
    try:
        payload = json.loads(proc.stdout)
    except json.JSONDecodeError:
        payload = {}
    return proc.returncode, payload


def _list_mails(limit: int, sync: bool) -> list[dict]:
    argv = ["list", "--limit", str(limit)] + (["--sync"] if sync else [])
    rc, payload = _wrapper_json(argv)
    if rc == 5:
        return []
    if rc != 0:
        guidance = str(payload.get("guidance") or "")[:200]
        raise triage_gate.GateError(f"wrapper list 실패 rc={rc}: {guidance}", 4)
    return list(payload.get("mails") or [])


def _get_mail(uid: str) -> dict:
    rc, payload = _wrapper_json(["get", uid, "--body"])
    if rc != 0:
        raise triage_gate.GateError(f"wrapper get 실패 rc={rc} uid(불투명)={triage_core.mask_value(uid)}", 4)
    return dict(payload.get("mail") or {})


def _delegate_schedule(schedule_text: str, uid_opaque: str) -> str:
    calendar_cli = _env_path(
        "TRIAGE_CALENDAR_CLI", "~/.hermes/skills/calendar/scripts/calendar_cli.py"
    )
    if not calendar_cli.exists():
        return "calendar-unavailable"
    proc = subprocess.run(  # noqa: S603 — W3-1 skill delegation (draft only)
        [sys.executable, str(calendar_cli), "draft-create", "--text", schedule_text],
        capture_output=True, text=True, timeout=120, check=False,
    )
    if proc.returncode == 0:
        match = re.search(r"^DRAFT-CREATED id=(\w+)", proc.stdout, re.M)
        return f"calendar:{match.group(1)}" if match else "calendar:unknown"
    if proc.returncode == 5:
        print(f"CAL-AMBIGUOUS uid={uid_opaque} (되묻기 필요 — 초안 없음)")
        return "calendar-ambiguous"
    print(f"CAL-FAIL uid={uid_opaque} rc={proc.returncode}", file=sys.stderr)
    return "calendar-failed"


def _delegate_todo(todo_text: str, uid_opaque: str) -> str:
    todo_cli = _env_path("TRIAGE_TODO_CLI", "~/.hermes/skills/todo/scripts/todo_cli.py")
    if not todo_cli.exists():
        return "todo-unavailable"
    proc = subprocess.run(  # noqa: S603 — owner-approval-gated delegation (draft + DM only)
        [sys.executable, str(todo_cli), "draft-create", "--title", todo_text],
        capture_output=True, text=True, timeout=120, check=False,
    )
    if proc.returncode == 0:
        match = re.search(r"^DRAFT-CREATED id=(\w+)", proc.stdout, re.M)
        return f"todo:{match.group(1)}" if match else "todo:unknown"
    print(f"TODO-FAIL uid={uid_opaque} rc={proc.returncode}", file=sys.stderr)
    return "todo-failed"
