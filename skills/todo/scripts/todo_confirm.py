"""Owner-confirmation transport for the todo draft gate (DM post + reaction poll).

No HMAC watcher-authorization split (unlike calendar's W3-1 design) — todo
drafts are posted and polled by the same cron process, mirroring
skills/mail/scripts/triage_confirm.py's simpler direct-reaction-scan pattern.
Owner DM channel resolution goes straight through the shared, sole approved
resolver (automation.interop.approval_directory.DiscordChannelDirectory) —
the same one calendar_binding.py and triage_binding.py wrap for their skills.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import quote
from urllib.request import Request, urlopen

from todo_gate import GateError

API = "https://discord.com/api/v10"
USER_AGENT = "DiscordBot (https://github.com/orientpine/autophagy-agents, 0)"
APPROVE_EMOJI = "✅"
CANCEL_EMOJI = "⛔"
ENV_SECRETS = Path.home() / ".env.secrets"


def owner_id() -> str:
    config = Path(os.environ.get("INTEROP_CONFIG", "~/.hermes/interop/config.json")).expanduser()
    try:
        value = json.loads(config.read_text(encoding="utf-8")).get("owner_id")
    except (OSError, json.JSONDecodeError) as error:
        raise GateError(f"interop config를 읽을 수 없습니다: {config}", 3) from error
    if not isinstance(value, str) or not value:
        raise GateError("interop config에 owner_id가 없습니다", 3)
    return value


def bot_token() -> str:
    token = os.environ.get("DISCORD_BOT_TOKEN", "")
    if token:
        return token
    try:
        for line in ENV_SECRETS.read_text(encoding="utf-8").splitlines():
            if line.startswith("DISCORD_BOT_TOKEN="):
                return line.split("=", 1)[1].strip()
    except OSError:
        pass
    raise GateError("DISCORD_BOT_TOKEN 없음 — 프로덕션 확인 경로 사용 불가", 3)


def _api(method: str, path: str, payload: dict | None = None) -> object:
    request = Request(
        f"{API}{path}",
        data=None if payload is None else json.dumps(payload).encode("utf-8"),
        headers={
            "Authorization": f"Bot {bot_token()}",
            "Content-Type": "application/json",
            "User-Agent": USER_AGENT,
        },
        method=method,
    )
    with urlopen(request, timeout=30) as response:  # noqa: S310
        body = response.read().decode("utf-8")
    return json.loads(body) if body else None


def owner_dm_channel() -> str:
    root = Path(
        os.environ.get("AUTOPHAGY_REPO_ROOT", str(Path(__file__).resolve().parents[3]))
    ).expanduser()
    if str(root) not in sys.path:
        sys.path.insert(0, str(root))
    try:
        from automation.interop.approval_directory import DiscordChannelDirectory
    except ImportError:
        raise GateError(f"승인 디렉터리 모듈 불가 (AUTOPHAGY_REPO_ROOT={root})", 3) from None
    return DiscordChannelDirectory(token=bot_token(), owner_id=owner_id(), api=_api).owner_dm()


def _change_summary(draft: dict) -> str:
    due_line = f"\n기한: {draft['due']}" if draft.get("due") else ""
    notes_line = f"\n메모: {draft['notes']}" if draft.get("notes") else ""
    return (
        "CHANGE-SUMMARY\n동작: 할일 생성\n"
        f"제목: {draft['title']}{due_line}{notes_line}\n"
        f"목록: {draft['tasklist']}"
    )


def post_confirmation_message(draft: dict) -> tuple[str, str]:
    """Post the owner confirmation DM and pre-add the two reaction choices."""
    channel_id = owner_dm_channel()
    content = (
        f"{_change_summary(draft)}\n\n이 메시지에 ✅ 실행 / ⛔ 취소. "
        f"텍스트 fallback: `실행 todo:{draft['id']}`/`취소 todo:{draft['id']}`\n"
        f"sha256:{draft['sha256']}"
    )
    message = _api("POST", f"/channels/{channel_id}/messages", {"content": content})
    message_id = str(message["id"])
    add_reaction(message_id, APPROVE_EMOJI, channel_id)
    add_reaction(message_id, CANCEL_EMOJI, channel_id)
    return channel_id, message_id


def add_reaction(message_id: str, emoji: str, channel_id: str) -> None:
    _api(
        "PUT",
        f"/channels/{channel_id}/messages/{message_id}/reactions/{quote(emoji, safe='')}/@me",
    )


def delete_message(message_id: str, channel_id: str) -> None:
    _api("DELETE", f"/channels/{channel_id}/messages/{message_id}")


def dm_owner(content: str) -> None:
    channel_id = owner_dm_channel()
    _api("POST", f"/channels/{channel_id}/messages", {"content": content})


def _owner_reacted(users: list[dict], owner: str) -> bool:
    return any(
        str(user.get("id", "")) == owner and not bool(user.get("bot", False)) for user in users
    )


def _reaction_users(channel_id: str, message_id: str, emoji: str) -> list[dict]:
    try:
        users = _api(
            "GET",
            f"/channels/{channel_id}/messages/{message_id}/reactions/{quote(emoji, safe='')}?limit=100",
        )
    except HTTPError as error:
        if error.code == 404:
            return []
        raise
    if not isinstance(users, list) or not all(isinstance(user, dict) for user in users):
        raise GateError("승인 리액션 응답이 유효하지 않음 — 거부", 1)
    return users


def resolve_reaction(draft: dict) -> str | None:
    """Return the bound owner decision, with ⛔ taking precedence over ✅."""
    if not draft.get("message_id"):
        raise GateError("드래프트가 아직 승인 메시지에 게시되지 않음 — 승인 불가", 1)
    channel_id = draft["channel_id"]
    message = _api("GET", f"/channels/{channel_id}/messages/{draft['message_id']}")
    if not isinstance(message, dict) or draft["sha256"] not in str(message.get("content", "")):
        raise GateError("승인 메시지가 이 드래프트 해시를 참조하지 않음 — 거부", 1)
    owner = owner_id()
    if _owner_reacted(_reaction_users(channel_id, draft["message_id"], CANCEL_EMOJI), owner):
        return CANCEL_EMOJI
    if _owner_reacted(_reaction_users(channel_id, draft["message_id"], APPROVE_EMOJI), owner):
        return APPROVE_EMOJI
    return None
