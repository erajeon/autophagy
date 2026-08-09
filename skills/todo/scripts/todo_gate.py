"""Local pending-draft store for owner-approval-gated Google Tasks writes.

Mirrors skills/calendar/scripts/calendar_gate.py's draft-store shape, but this
module never touches Google Tasks itself. On owner confirm it appends the
exact approval record ``automation.interop.external_effect_gate`` requires
(see ``append_manual_reaction_approval`` — channel/method are fixed literals
the gate hard-checks, not a description of the DM transport) and then hands
off to the ALREADY-AUDITED write path in ``todo_cli.create_task`` (insert +
mandatory re-read verification). This module only owns the local draft
lifecycle and the approval-log append.
"""
from __future__ import annotations

import fcntl
import hashlib
import json
import os
import secrets
from datetime import UTC, datetime
from pathlib import Path


class GateError(RuntimeError):
    """Gate refusal with a CLI exit code (1 unconfirmed, 3 config)."""

    def __init__(self, message: str, exit_code: int = 3) -> None:
        super().__init__(message)
        self.exit_code = exit_code


def gate_dir() -> Path:
    path = Path(os.environ.get("TODO_GATE_DIR", "~/.hermes/todo-gate")).expanduser()
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    return path


def utc_now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _drafts_dir() -> Path:
    path = gate_dir() / "drafts"
    path.mkdir(mode=0o700, exist_ok=True)
    return path


def _draft_path(draft_id: str) -> Path:
    if not draft_id.isalnum():
        raise GateError(f"잘못된 드래프트 id: {draft_id!r}", 3)
    return _drafts_dir() / f"{draft_id}.json"


def write_json(path: Path, record: dict) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    path.write_text(json.dumps(record, ensure_ascii=False, sort_keys=True), encoding="utf-8")
    path.chmod(0o600)


def draft_sha256(record: dict) -> str:
    """Content hash binding a draft to the exact Tasks insert it will execute."""
    bound = {key: record[key] for key in ("title", "notes", "due", "tasklist")}
    canonical = json.dumps(bound, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def create_draft(*, title: str, notes: str, due: str, tasklist: str) -> dict:
    draft_id = secrets.token_hex(3)
    while _draft_path(draft_id).exists():
        draft_id = secrets.token_hex(3)
    record = {
        "id": draft_id,
        "title": title,
        "notes": notes,
        "due": due,
        "tasklist": tasklist,
        "channel_id": "",
        "message_id": "",
        "created": utc_now(),
        "status": "pending",
    }
    record["sha256"] = draft_sha256(record)
    write_json(_draft_path(draft_id), record)
    return record


def load_draft(draft_id: str) -> dict:
    path = _draft_path(draft_id)
    if not path.exists():
        raise GateError(f"드래프트 없음: {draft_id}", 3)
    record = json.loads(path.read_text(encoding="utf-8"))
    if record.get("status") != "pending":
        raise GateError(f"드래프트 {draft_id} 상태={record.get('status')} — pending 아님", 1)
    return record


def set_message_id(draft: dict, message_id: str, channel_id: str) -> dict:
    updated = {**draft, "message_id": message_id, "channel_id": channel_id}
    write_json(_draft_path(draft["id"]), updated)
    return updated


def discard_draft(draft_id: str) -> None:
    path = _draft_path(draft_id)
    if not path.exists():
        raise GateError(f"드래프트 없음: {draft_id}", 3)
    path.unlink()


def list_drafts() -> list[dict]:
    return [
        json.loads(path.read_text(encoding="utf-8"))
        for path in sorted(_drafts_dir().glob("*.json"))
    ]


def mark_executed(draft: dict, *, task_id: str) -> None:
    write_json(_draft_path(draft["id"]), {**draft, "status": "executed", "task_id": task_id})


def approval_log() -> Path:
    default = str(Path.home() / ".hermes" / "todo" / "approvals.jsonl")
    return Path(os.environ.get("TODO_APPROVAL_LOG", default)).expanduser()


def append_manual_reaction_approval(
    *, action_hash: str, target_id: str, owner_id: str, message_ref: str
) -> None:
    """Append the exact record shape ``external_effect_gate._has_valid_approval`` accepts.

    ``channel``/``method`` are fixed literals the gate hard-requires; they label
    the approval TYPE the gate recognizes, not the transport the owner actually
    confirmed through (a DM, here) — see automation/interop/external_effect_gate.py.
    """
    record = {
        "action": "external_effect.approval",
        "approval": {
            "channel": "approvals",
            "message_id": message_ref,
            "method": "manual_reaction",
            "owner_id": owner_id,
        },
        "hash": action_hash,
        "result": {"status": "approved"},
        "target_id": target_id,
        "timestamp": utc_now(),
    }
    path = approval_log()
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        handle.write(
            json.dumps(record, ensure_ascii=False, separators=(",", ":"), sort_keys=True) + "\n"
        )
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    path.chmod(0o600)
