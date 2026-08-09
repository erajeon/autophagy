#!/usr/bin/env python3
"""mail triage CLI (W4-2/W4-6): owner-instruction reply drafting→owner
confirmation→send pipeline. Drafting is owner-initiated (`draft` — 지시문
필수); the cron `watch` tick is the approval/send loop ONLY (repost→✅/⛔
resolution→send — no auto-drafting). Draft pipeline order (fixed):
① deterministic sensitivity gate (constraint 6, BEFORE any LLM)
② classification annotation (glm-main; gate-hit mail → non-GLM tier, NEVER
GLM — never gates an owner-instructed draft) ③ Korean final-text reply draft
(non-GLM tier, instruction-aware v2 prompt) ④ owner gate ⑤ site backend
send ⑥ approvals.jsonl (W0-6 schema). Schedule-needed mail is delegated to
the W3-1 calendar skill via the legacy manual `process` path (draft only —
its own gate confirms; SQLite claim-before-draft keeps its ticks idempotent).

Exit codes: 0 ok | 1 approval absent/invalid (nothing sent) | 2 input rejected
            3 config/env/mode error | 4 mail read failed | 6 send failed

Env: TRIAGE_GATE_DIR, TRIAGE_DB, TRIAGE_APPROVAL_LOG, TRIAGE_MAIL_HOME,
     TRIAGE_MAIL_MODE_FILE, TRIAGE_MAIL_MODE_REPO, TRIAGE_RULES_FILE,
     TRIAGE_CLASSIFY_PROMPT, TRIAGE_REPLY_PROMPT, TRIAGE_CALENDAR_CLI,
     INTEROP_RUNTIME, INTEROP_CONFIG, E2E_TEST_MODE, INTEROP_E2E_SECRET
     (+ test hooks TRIAGE_GLM_BIN / TRIAGE_HERMES_BIN — never in production).
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path


def _load_env_secrets(path: Path = Path.home() / ".env.secrets") -> None:
    """Best-effort ``.env.secrets`` load — Hermes cron does not inject it.

    Same pattern as skills/wiki/scripts/wiki_confirm_reaction_watch.py and
    skills/todo/scripts/todo_cli.py: AUTOPHAGY_REPO_ROOT (needed by
    triage_approval/triage_binding for the mounted-copy deployment under
    ~/.hermes/skills/mail/scripts/) is not reliably present in the cron's
    process environment. Never overrides an already-set variable.
    """
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return
    for raw_line in lines:
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        if key and key not in os.environ:
            os.environ[key] = value.strip().strip('"').strip("'")


_load_env_secrets()

import triage_confirm
import triage_core
import triage_digest
import triage_gate
import triage_llm
import triage_mode
import triage_pipeline
import mail_preflight
import triage_sensitivity
import triage_store
import site_mail_backend
from triage_transport import _get_mail, _rules_path

DEFAULT_REPLY_PROMPT = triage_pipeline.DEFAULT_REPLY_PROMPT


def cmd_process(args: argparse.Namespace) -> int:
    return triage_pipeline.run_process(args)


def cmd_draft(args: argparse.Namespace) -> int:
    """Owner-instruction draft: 지시가 초안 여부를 결정하고 분류는 주석일 뿐이다."""
    if triage_mode.effective_mode() == "no-go":
        raise triage_gate.GateError("mail-mode=no-go — W4-2 파이프라인 비활성(W4-1N 분기)", 3)
    if triage_gate.has_draft_for(args.uid):
        raise triage_gate.GateError(
            f"uid(불투명)={triage_core.mask_value(args.uid)} 초안이 이미 있음 — "
            "먼저 discard 후 다시 시도", 2,
        )
    detail = _get_mail(args.uid)
    rules = triage_sensitivity.load_rules(_rules_path())
    gate, cls = triage_pipeline._gate_and_classify(args.uid, detail, rules)
    actions = triage_pipeline._draft_and_post(
        {**detail, "uid": args.uid}, gate, cls,
        post=not args.no_post, instruction=args.instruction,
        attachments=tuple(getattr(args, "attachment", ()) or ()),
    )
    if "reply-no-address" in actions:
        raise triage_gate.GateError("회신 주소를 찾을 수 없음 — 초안 생성 불가", 2)
    draft_id = actions[0].removeprefix("draft:")
    triage_store.record_processed(
        triage_gate.db_path(), args.uid, category=cls.category, sensitive=gate.sensitive,
        action=f"instr-draft:{draft_id}", processed_at=triage_core.utc_now(),
    )
    posted = int(any(action.startswith("posted:") for action in actions))
    print(f"DRAFTED draft={draft_id} posted={posted}")
    return 0


def cmd_compose(args: argparse.Namespace) -> int:
    if triage_mode.effective_mode() == "no-go":
        raise triage_gate.GateError("mail-mode=no-go — W4-2 파이프라인 비활성(W4-1N 분기)", 3)
    draft = triage_pipeline.compose_and_post(
        args.to, args.subject, args.body, post=not args.no_post,
        attachments=tuple(getattr(args, "attachment", ()) or ()),
    )
    posted = int(bool(draft.get("message_id")))
    print(f"COMPOSED draft={draft['id']} posted={posted}")
    return 0


def cmd_digest(args: argparse.Namespace) -> int:
    if triage_mode.effective_mode() == "no-go":
        raise triage_gate.GateError("mail-mode=no-go — 다이제스트 비활성(W4-1N 분기)", 3)
    return triage_digest.run_digest(
        limit=args.limit, sync=not args.no_sync, dry_run=args.dry_run
    )


def cmd_digest_items(args: argparse.Namespace) -> int:
    items = triage_store.latest_digest_items(triage_gate.db_path(), args.run)
    if not items:
        run_label = f" run={args.run}" if args.run is not None else ""
        raise triage_gate.GateError(f"다이제스트 항목 없음{run_label}", 3)
    for item in items:
        print(
            f"ITEM no={item['item_no']} uid={item['uid']} "
            f"sensitive={int(item['sensitive'])} category={item['category']} "
            f"flags={item['flags']} subject={item['subject']}"
        )
    return 0


def _notify_sent(draft: dict) -> None:
    """Best-effort owner notification after a committed send (owner request 2026-07-20).

    The send is already executed; NO failure here may fail the tick or make a
    committed draft look unsent — hence the broad catch (repo precedent
    run_process).
    """
    try:
        triage_confirm.dm_owner(
            f"✉️ 발송 완료: {draft['subject']} → {draft['to']} (draft {draft['id']})")
    except Exception as error:  # noqa: BLE001 — notification must never break the tick
        print(f"NOTIFY-FAIL draft={draft['id']} "
              f"err={triage_core.redact(str(error))[:120]}", file=sys.stderr)


def cmd_watch(_args: argparse.Namespace) -> int:
    """Production cron tick: repost pending, resolve ✅/⛔, send approved (초안 생성 없음)."""
    if os.environ.get("E2E_TEST_MODE"):
        raise triage_gate.GateError("watch는 프로덕션 전용 — E2E_TEST_MODE 거부", 3)
    mode = triage_mode.effective_mode()
    if mode != "full-go":
        print(f"MODE-SKIP mode={mode} (W4-1N 분기 — 자동 발송 정지)")
        return 0
    owner = triage_confirm.owner_id()
    for draft in triage_gate.list_drafts():
        if draft.get("status") != "pending":
            continue
        if not draft.get("message_id"):
            draft = {**draft, "message_id": triage_pipeline._post_draft_for_approval(draft)}
            print(f"REPOSTED draft={draft['id']} message={draft['message_id']}")
        try:
            action = triage_confirm.resolve_reaction(draft)
        except triage_gate.GateError as error:
            if error.exit_code != 1:
                raise
            continue  # 승인 없음 → pending 유지, 발송 0건
        except OSError as error:  # 429/네트워크 등 일시 오류 — draft 단위 격리, 다음 tick 재시도
            print(f"REACTION-RETRY draft={draft['id']} "
                  f"err={triage_core.redact(str(error))[:120]}", file=sys.stderr)
            continue
        if action == triage_confirm.CANCEL_EMOJI:
            triage_gate.discard_draft(draft["id"])
            triage_confirm.dm_owner("메일 발송 취소됨")
            print(f"CANCELLED draft={draft['id']} method=manual_reaction")
            continue
        if action != triage_confirm.APPROVE_EMOJI:
            continue
        ref = f"reaction:{draft['message_id']}"
        mail_preflight.execute_cli_draft(draft, triage_gate.Approval(
            ref=ref, method="manual_reaction", owner=owner,
        ))
        _notify_sent(draft)
        print(f"SENT draft={draft['id']} method=manual_reaction ref={ref}")
    return 0


def cmd_confirm(args: argparse.Namespace) -> int:
    draft = triage_gate.load_draft(args.draft)
    if args.injection_file:
        ref = triage_confirm.confirm_via_injection(draft, Path(args.injection_file))
        method = "signed_injection_e2e"
    else:
        if os.environ.get("E2E_TEST_MODE"):
            raise triage_gate.GateError(
                "E2E_TEST_MODE인데 --injection-file이 없음 — 모호한 모드 거부", 3
            )
        ref = triage_confirm.confirm_via_reaction(draft)
        method = "manual_reaction"
    mail_preflight.execute_cli_draft(draft, triage_gate.Approval(
        ref=ref, method=method, owner=triage_confirm.owner_id(),
    ))
    _notify_sent(draft)
    print(f"SENT draft={args.draft} method={method} ref={ref}")
    return 0


def cmd_discard(args: argparse.Namespace) -> int:
    triage_gate.discard_draft(args.draft)
    print(f"DISCARDED draft={args.draft}")
    return 0


def cmd_list_drafts(_args: argparse.Namespace) -> int:
    for record in triage_gate.list_drafts():
        print(f"DRAFT id={record['id']} status={record['status']} "
              f"sensitive={record['sensitive']} uid={record['uid_opaque']} "
              f"message={record.get('message_id') or 'unposted'} created={record['created']}")
    return 0


def cmd_sign(args: argparse.Namespace) -> int:
    draft = triage_gate.load_draft(args.draft)
    triage_confirm.sign_injection(
        draft, Path(args.out), args.user_id or None, args.channel_id or None,
        args.forge_signature,
    )
    print(f"SIGNED draft={args.draft} out={args.out} forged={args.forge_signature}")
    return 0


def cmd_mode(_args: argparse.Namespace) -> int:
    print(f"MODE effective={triage_mode.effective_mode()} "
          f"runtime={triage_mode.runtime_mode_file()} seed={triage_mode.repo_mode_file()} "
          f"consecutive_send_failures={triage_store.consecutive_send_failures(triage_gate.db_path())}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="mail-triage", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    process = sub.add_parser(
        "process",
        help="신규 메일 triage (게이트→분류→초안→게시) (legacy 수동 — cron은 더 이상 자동 초안 생성 안 함)",
    )
    process.add_argument("--limit", type=int, default=10)
    process.add_argument("--no-sync", action="store_true", help="site backend 동기화 생략")
    process.add_argument("--no-post", action="store_true", help="초안만 만들고 승인 메시지 게시 생략")
    process.set_defaults(func=cmd_process)

    draft = sub.add_parser("draft", help="소유자 지시 기반 회신 초안 (분류는 주석 — 게이트 아님)")
    draft.add_argument("--uid", required=True)
    draft.add_argument("--instruction", required=True, help="회신에 반영할 소유자 지시문")
    draft.add_argument(
        "--attachment", action="append", default=[], metavar="PATH",
        help="첨부할 로컬 파일 경로 (여러 번 지정 가능)",
    )
    draft.add_argument("--no-post", action="store_true", help="초안만 만들고 승인 메시지 게시 생략")
    draft.set_defaults(func=cmd_draft)

    compose = sub.add_parser(
        "compose", help="새 메일 작성 초안 — 소유자 확정 게이트 (같은 watch cron 재사용)"
    )
    compose.add_argument("--to", required=True)
    compose.add_argument("--subject", required=True)
    compose.add_argument("--body", required=True)
    compose.add_argument(
        "--attachment", action="append", default=[], metavar="PATH",
        help="첨부할 로컬 파일 경로 (여러 번 지정 가능)",
    )
    compose.add_argument("--no-post", action="store_true", help="초안만 만들고 승인 메시지 게시 생략")
    compose.set_defaults(func=cmd_compose)

    digest = sub.add_parser("digest", help="site 메일 다이제스트 개인 메시지 생성 (읽기 전용)")
    digest.add_argument("--limit", type=int, default=20)
    digest.add_argument("--no-sync", action="store_true", help="site backend 동기화 생략")
    digest.add_argument("--dry-run", action="store_true", help="전송·저장 없이 다이제스트 미리보기")
    digest.set_defaults(func=cmd_digest)

    digest_items = sub.add_parser("digest-items", help="최근 다이제스트 항목 조회 (마스킹)")
    digest_items.add_argument("--run", type=int, default=None, help="조회할 다이제스트 run id")
    digest_items.set_defaults(func=cmd_digest_items)

    watch = sub.add_parser("watch", help="프로덕션 cron tick: 승인/취소 처리 + 승인건 발송 (자동 초안 생성 없음)")
    watch.add_argument("--limit", type=int, default=10, help=argparse.SUPPRESS)
    watch.add_argument("--no-sync", action="store_true", help=argparse.SUPPRESS)
    watch.add_argument("--no-post", action="store_true", help=argparse.SUPPRESS)
    watch.set_defaults(func=cmd_watch)

    confirm = sub.add_parser("confirm", help="소유자 승인 검증 후에만 발송")
    confirm.add_argument("--draft", required=True)
    confirm.add_argument("--injection-file", default="")
    confirm.set_defaults(func=cmd_confirm)

    discard = sub.add_parser("discard", help="초안 폐기 (승인 거부, 발송 0건)")
    discard.add_argument("--draft", required=True)
    discard.set_defaults(func=cmd_discard)

    sub.add_parser("list-drafts", help="초안 목록 (마스킹)").set_defaults(func=cmd_list_drafts)
    sub.add_parser("mode", help="유효 mail-mode + 실패 카운터").set_defaults(func=cmd_mode)

    sign = sub.add_parser("sign", help="E2E 전용: 서명된 주입 승인 생성")
    sign.add_argument("--draft", required=True)
    sign.add_argument("--out", required=True)
    sign.add_argument("--user-id", default="")
    sign.add_argument("--channel-id", default="")
    sign.add_argument("--forge-signature", action="store_true")
    sign.set_defaults(func=cmd_sign)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    try:
        return int(args.func(args))
    except triage_gate.GateError as error:
        print(f"GATE-REFUSED {error}", file=sys.stderr)
        return error.exit_code
    except triage_llm.PatentRoutingError as error:
        print(f"ROUTING-REFUSED {error}", file=sys.stderr)
        return 3
    except triage_llm.LlmCallError as error:
        print(f"LLM-FAIL {triage_core.redact(str(error))[:300]}", file=sys.stderr)
        return 3
    except triage_core.LlmParseError as error:
        print(f"LLM-PARSE-FAIL {triage_core.redact(str(error))[:300]}", file=sys.stderr)
        return 2
    except triage_core.AttachmentPolicyError as error:
        print(
            f"ATTACHMENT-REFUSED error_code={error.error_code} retryable=false {error}",
            file=sys.stderr,
        )
        return 2
    except site_mail_backend.BackendUnavailable as error:
        print(f"BACKEND-REFUSED {error}", file=sys.stderr)
        return 3
    except FileNotFoundError as error:
        print(f"GATE-REFUSED 파일 없음: {error.filename}", file=sys.stderr)
        return 3


if __name__ == "__main__":
    raise SystemExit(main())
