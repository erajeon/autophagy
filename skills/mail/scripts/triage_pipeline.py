"""Shared drafting pipeline used by the triage CLI's process/draft paths."""
from __future__ import annotations

import argparse
import os
import secrets
import sys
from dataclasses import replace

import triage_approval
import triage_core
import triage_gate
import triage_llm
import triage_mode
import triage_recipient
import triage_sensitivity
import triage_store
from triage_transport import (
    SKILL_DIR,
    _delegate_schedule,
    _delegate_todo,
    _env_path,
    _get_mail,
    _list_mails,
    _rules_path,
)

DEFAULT_REPLY_PROMPT = SKILL_DIR / "prompts/reply-draft-v2.md"


def _reply_draft_enabled() -> bool:
    """회신 초안 자동 작성 기능 스위치 (owner 요청 2026-08-12, 기본값 보류).

    보류 중에는 reply_needed 메일에 LLM 회신 초안을 작성해 승인받는 대신,
    "회신 필요" 할일 알림만 보낸다. 재개하려면 .env.secrets 에
    TRIAGE_REPLY_DRAFT_ENABLED=1 을 추가할 것.
    """
    return os.environ.get("TRIAGE_REPLY_DRAFT_ENABLED", "0") == "1"


def _post_draft_for_approval(draft: dict, *, notice: str = "") -> str:
    """Post through the shared lifecycle: one live approval message per mail key."""
    return triage_approval.post_for_approval(draft, notice=notice)


def compose_and_post(
    to: str, subject: str, body: str, *, post: bool,
    attachments: tuple[str, ...] = (),
) -> dict:
    rules = triage_sensitivity.load_rules(_rules_path())
    gate = triage_sensitivity.evaluate("\n".join((subject, to, body)), rules)
    draft = triage_gate.create_draft(
        uid=f"compose:{secrets.token_hex(8)}", sender="", mail_subject="",
        to=to, subject=subject, body=body, sensitive=gate.sensitive,
        tags=gate.tags, category="compose", flags=(),
        kind="compose",
        attachment_paths=attachments,
    )
    gap = triage_recipient.related_recipient_gap(
        to, subject, triage_gate.list_drafts(), now_utc=triage_core.utc_now())
    notice = ""
    if gap:
        shown = ", ".join(gap[:5]) + (f" 외 {len(gap) - 5}명" if len(gap) > 5 else "")
        notice = ("\n⚠️ 직전 관련 메일 수신자 중 제외됨: "
                  f"`{shown}` — 의도한 제외인지 확인 후 ✅ 해주세요")
    if post:
        _post_draft_for_approval(draft, notice=notice)
        draft = triage_gate.load_draft(draft["id"])
    else:
        print(
            triage_core.render_approvals_message(
                draft,
                destination=triage_core.ApprovalRenderDestination.CONSOLE,
            )
            + notice
        )
    return draft


def _draft_and_post(
    detail: dict, gate, cls, *, post: bool, instruction: str = "",
    attachments: tuple[str, ...] = (),
) -> list[str]:
    actions: list[str] = []
    to = triage_core.extract_reply_address(detail.get("sender") or "")
    if not to:
        return ["reply-no-address"]
    subject, body, _provider = triage_llm.draft_reply(
        subject=detail.get("subject") or "", sender=detail.get("sender") or "",
        body=detail.get("body") or "", sensitive=gate.sensitive,
        uid_opaque=triage_core.mask_value(detail["uid"]),
        prompt_path=_env_path("TRIAGE_REPLY_PROMPT", str(DEFAULT_REPLY_PROMPT)),
        instruction=instruction,
    )
    draft = triage_gate.create_draft(
        uid=detail["uid"], sender=detail.get("sender") or "",
        mail_subject=detail.get("subject") or "", to=to, subject=subject, body=body,
        sensitive=gate.sensitive, tags=gate.tags, category=cls.category, flags=cls.flags(),
        attachment_paths=attachments,
    )
    actions.append(f"draft:{draft['id']}")
    if post:
        draft = {**draft, "message_id": _post_draft_for_approval(draft)}
        actions.append(f"posted:{draft['message_id']}")
    else:
        print(
            triage_core.render_approvals_message(
                draft,
                destination=triage_core.ApprovalRenderDestination.CONSOLE,
            )
        )
    return actions


def _gate_and_classify(
    uid: str, detail: dict, rules: tuple[triage_sensitivity.TagRule, ...]
) -> tuple[triage_sensitivity.GateResult, triage_core.Classification]:
    subject = detail.get("subject") or ""
    sender = detail.get("sender") or ""
    body = detail.get("body") or ""
    gate = triage_sensitivity.evaluate("\n".join((subject, sender, body)), rules)  # ① FIRST
    cls, _provider = triage_llm.classify(  # ② routed by the step-① verdict
        subject=subject, sender=sender, body=body, sensitive=gate.sensitive,
        uid_opaque=triage_core.mask_value(uid),
        prompt_path=_env_path(
            "TRIAGE_CLASSIFY_PROMPT", str(SKILL_DIR / "prompts/triage-classify-v2.md")
        ),
    )
    return gate, cls


def _process_one(uid: str, rules, *, post: bool) -> tuple[str, bool, str]:
    detail = _get_mail(uid)
    gate, cls = _gate_and_classify(uid, detail, rules)
    if cls.category == "spam":
        return "spam-skip", gate.sensitive, cls.category
    if cls.category != "important":
        return "no-action", gate.sensitive, cls.category
    actions: list[str] = []
    role = triage_recipient.recipient_role(
        str(detail.get("body") or ""), triage_recipient.owner_address()
    )
    if role == "cc" and cls.reply_needed:  # 참조 수신 — 회신 대상 아님 (owner 2026-07-19)
        cls = replace(cls, reply_needed=False)
        actions.append("cc-no-reply")
    if cls.schedule_needed:
        if cls.schedule_text:
            actions.append(_delegate_schedule(cls.schedule_text, triage_core.mask_value(uid)))
        else:
            actions.append("calendar-no-text")
    if cls.todo_needed:
        if cls.todo_text:
            actions.append(_delegate_todo(cls.todo_text, triage_core.mask_value(uid)))
        else:
            actions.append("todo-no-text")
    if cls.reply_needed:
        if _reply_draft_enabled():
            actions += _draft_and_post({**detail, "uid": uid}, gate, cls, post=post)  # ③④
        else:
            subject = detail.get("subject") or "(제목없음)"
            actions.append(
                _delegate_todo(f"메일 회신 필요: {subject}", triage_core.mask_value(uid))
            )
    return ",".join(actions) or "logged", gate.sensitive, cls.category


def run_process(args: argparse.Namespace) -> int:
    if triage_mode.effective_mode() == "no-go":
        raise triage_gate.GateError("mail-mode=no-go — W4-2 파이프라인 비활성(W4-1N 분기)", 3)
    rules = triage_sensitivity.load_rules(_rules_path())
    db = triage_gate.db_path()
    handled = 0
    for mail in _list_mails(args.limit, not args.no_sync):
        uid = str(mail.get("uid") or "")
        if not uid or triage_store.is_processed(db, uid) or triage_gate.has_draft_for(uid):
            continue
        if not triage_store.claim_mail(db, uid, triage_core.utc_now()):
            continue
        opaque = triage_core.mask_value(uid)
        try:
            action, sensitive, category = _process_one(uid, rules, post=not args.no_post)
        except Exception as error:  # noqa: BLE001 — release claim, keep the tick going
            triage_store.release_mail(db, uid)
            print(f"MAIL-FAIL uid={opaque} err={triage_core.redact(str(error))[:200]}",
                  file=sys.stderr)
            continue
        triage_store.record_processed(
            db, uid, category=category, sensitive=sensitive, action=action,
            processed_at=triage_core.utc_now(),
        )
        print(f"MAIL uid={opaque} sensitive={sensitive} category={category} action={action}")
        handled += 1
    print(f"PROCESSED n={handled}")
    return 0
