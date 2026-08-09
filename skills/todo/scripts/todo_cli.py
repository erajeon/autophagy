"""Approval-gated Google Tasks writer with a mandatory post-write re-read.

Before this module, Google Tasks had no repo-side code at all: the agent reached
it by typing ``gws tasks tasks insert`` into a terminal tool, which matched no
denylist rule and therefore looked like a *read* to the production gate. That is
how a mis-transcribed personal name was written to an external system with no
owner ✅.

Two invariants close that hole, in this order:

1. **No write without an owner approval record.** The exact argv that will run is
   frozen first, canonicalized into a ``ToolCall``, and handed to the unmodified
   ``automation.interop.external_effect_gate``. One ✅ authorizes one argv — a
   different title yields a different ``action_hash`` and is refused. No new
   approval surface, watcher or resolver is introduced here; the existing gate
   record store is the only authority.
2. **No success claim without proof.** After ``insert`` the task is RE-READ with
   ``gws tasks tasks get`` and the stored title/id are compared to what was sent.
   A mismatch, an empty response, or a failed re-read raises — never a silent OK.

``create_task`` is deliberately the single write path so the personal-name
preflight guard can wrap exactly one function.

Env: TODO_APPROVAL_LOG, TODO_DENYLIST, TODO_OWNER_ID, TODO_GWS_BIN,
     AUTOPHAGY_REPO_ROOT, INTEROP_CONFIG, E2E_TEST_MODE.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess  # noqa: S404 — gws CLI is the only supported Tasks transport
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from importlib import import_module
from pathlib import Path
from types import ModuleType
from typing import TYPE_CHECKING, Any


def _load_env_secrets(path: Path = Path.home() / ".env.secrets") -> None:
    """Best-effort ``.env.secrets`` load — Hermes cron does not inject it.

    Same pattern as skills/wiki/scripts/wiki_confirm_reaction_watch.py: this
    CLI is invoked both as a mail-triage delegation subprocess and as a cron
    watch subprocess, and neither caller's environment reliably carries
    AUTOPHAGY_REPO_ROOT — so this module is self-sufficient regardless of
    caller. Never overrides an already-set variable.
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

import todo_confirm
import todo_gate

if TYPE_CHECKING:
    from automation.interop.external_effect_gate import ApprovalContext, ExternalEffectDecision

GWS_TIMEOUT_S = 120
CommandRunner = Callable[[list[str]], dict[str, Any]]


class TodoError(RuntimeError):
    """A todo operation failed and must not be reported as success."""

    def __init__(self, message: str, exit_code: int = 1) -> None:
        super().__init__(message)
        self.exit_code = exit_code


class ApprovalRequiredError(TodoError):
    """The write was refused: no owner approval record binds this exact argv."""

    def __init__(self, message: str) -> None:
        super().__init__(message, 4)


class VerificationFailedError(TodoError):
    """The post-write re-read did not prove the task we asked for exists."""

    def __init__(self, message: str) -> None:
        super().__init__(message, 6)


class EntityClarificationError(TodoError):
    """An owner-facing entity clarify must be emitted once on stdout."""

    def __init__(self, message: str, should_render: bool) -> None:
        super().__init__(message, 6)
        self.should_render = should_render


@dataclass(frozen=True, slots=True)
class TaskRequest:
    """One Google Tasks entry as the owner asked for it."""

    tasklist: str
    title: str
    notes: str | None = None
    due: str | None = None


@dataclass(frozen=True, slots=True)
class CreatedTask:
    """A task whose existence was re-read from the API after the write."""

    task_id: str
    title: str
    tasklist: str
    action_hash: str
    verified: bool


def repo_root() -> Path:
    env = os.environ.get("AUTOPHAGY_REPO_ROOT")
    if env:
        return Path(env).expanduser()
    return Path(__file__).resolve().parents[3]


def gate_module() -> ModuleType:
    """Import the production gate; refuse the write when it is unreachable."""
    root = repo_root()
    if str(root) not in sys.path:
        sys.path.insert(0, str(root))
    try:
        return import_module("automation.interop.external_effect_gate")
    except ImportError as error:
        raise TodoError(
            f"외부효과 게이트 모듈 불가 (AUTOPHAGY_REPO_ROOT={root}) — 쓰기 거부", 3
        ) from error


def denylist_path() -> Path:
    override = os.environ.get("TODO_DENYLIST")
    if override:
        return Path(override).expanduser()
    return repo_root() / "configs" / "external-effect-tools.yaml"


def approval_log() -> Path:
    default = str(Path.home() / ".hermes" / "todo" / "approvals.jsonl")
    return Path(os.environ.get("TODO_APPROVAL_LOG", default)).expanduser()


def owner_id() -> str:
    env = os.environ.get("TODO_OWNER_ID")
    if env:
        return env
    config = Path(os.environ.get("INTEROP_CONFIG", "~/.hermes/interop/config.json")).expanduser()
    try:
        value = json.loads(config.read_text(encoding="utf-8")).get("owner_id")
    except (OSError, json.JSONDecodeError) as error:
        raise TodoError(f"interop config를 읽을 수 없습니다: {config}", 3) from error
    if not isinstance(value, str) or not value:
        raise TodoError("interop config에 owner_id가 없습니다 (fail-closed)", 3)
    return value


def approval_context() -> ApprovalContext:
    return gate_module().ApprovalContext(
        approval_log=approval_log(),
        owner_id=owner_id(),
        e2e_test_mode=os.environ.get("E2E_TEST_MODE") == "1",
    )


def _compact(payload: dict[str, Any]) -> str:
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def insert_argv(request: TaskRequest) -> tuple[str, ...]:
    """Freeze the exact create command; argv[0] stays 'gws' so the hash is host-independent."""
    body: dict[str, Any] = {"title": request.title}
    if request.notes:
        body["notes"] = request.notes
    if request.due:
        body["due"] = request.due
    return (
        "gws", "tasks", "tasks", "insert",
        "--params", _compact({"tasklist": request.tasklist}),
        "--json", _compact(body),
    )


def get_argv(tasklist: str, task_id: str) -> tuple[str, ...]:
    return (
        "gws", "tasks", "tasks", "get",
        "--params", _compact({"task": task_id, "tasklist": tasklist}),
    )


def list_argv(tasklist: str) -> tuple[str, ...]:
    return (
        "gws", "tasks", "tasks", "list",
        "--params", _compact({"maxResults": 50, "tasklist": tasklist}),
    )


def build_tool_call(argv: Sequence[str]) -> Any:
    """Canonicalize argv the same way the Hermes terminal tool call is canonicalized."""
    return gate_module().ToolCall(tool_name="gws", arguments={"command": " ".join(argv)})


def evaluate(argv: Sequence[str], *, context: ApprovalContext) -> ExternalEffectDecision:
    """Run the production gate over this exact argv (read-only; no side effects)."""
    module = gate_module()
    return module.evaluate_tool_call(
        build_tool_call(argv), module.load_denylist(denylist_path()), context
    )


def gws_bin() -> str:
    override = os.environ.get("TODO_GWS_BIN", "")
    if override:
        return override
    found = shutil.which("gws") or os.path.expanduser("~/.local/bin/gws")
    if not Path(found).exists():
        raise TodoError("gws CLI를 찾을 수 없습니다 (TODO_GWS_BIN 설정 필요)", 3)
    return found


def run_gws(argv: Sequence[str]) -> dict[str, Any]:
    """Execute a frozen gws argv with an explicitly propagated child env."""
    command = [gws_bin(), *argv[1:]]
    env = dict(os.environ)
    env.setdefault("PATH", "/usr/local/bin:/usr/bin:/bin")
    proc = subprocess.run(  # noqa: S603
        command, capture_output=True, text=True, timeout=GWS_TIMEOUT_S, check=False, env=env
    )
    if proc.returncode != 0:
        raise TodoError(
            f"gws 실행 실패 rc={proc.returncode}: {proc.stderr.strip()[:200]}", 5
        )
    decoded, _ = json.JSONDecoder().raw_decode(proc.stdout.strip() or "{}")
    if not isinstance(decoded, dict):
        raise TodoError("gws 응답이 객체가 아닙니다", 5)
    return decoded


def create_task(
    request: TaskRequest, *, runner: CommandRunner | None = None, context: ApprovalContext | None = None
) -> CreatedTask:
    """THE single Google Tasks write path — gate first, write, then prove by re-read."""
    execute = runner if runner is not None else run_gws
    ctx = context if context is not None else approval_context()
    adapter = import_module("todo_preflight")
    try:
        result = adapter.create_task(
            adapter.TodoPreflightBindings(
                request,
                execute,
                ctx,
                insert_argv,
                get_argv,
                lambda argv, bound_context: evaluate(argv, context=bound_context),
            )
        )
    except adapter.TodoPreflightError as error:
        if error.should_render:
            raise EntityClarificationError(str(error), True) from None
        if error.exit_code == 4:
            raise ApprovalRequiredError(str(error)) from None
        if error.exit_code == 6:
            raise VerificationFailedError(str(error)) from None
        raise TodoError(str(error), error.exit_code) from None
    return CreatedTask(result.task_id, result.title, request.tasklist, result.action_hash, True)


def _cmd_plan(args: argparse.Namespace) -> int:
    request = TaskRequest(args.tasklist, args.title, args.notes, args.due)
    decision = evaluate(insert_argv(request), context=approval_context())
    print(
        f"PLAN tasklist={request.tasklist} external_effect={decision.external_effect} "
        f"approved={decision.allowed} hash={decision.action_hash} target={decision.target_id}"
    )
    return 0


def _cmd_create(args: argparse.Namespace) -> int:
    created = create_task(TaskRequest(args.tasklist, args.title, args.notes, args.due))
    print(f"CREATED id={created.task_id} tasklist={created.tasklist} hash={created.action_hash}")
    print(f"VERIFIED reread=tasks.tasks.get id={created.task_id} title_match=true")
    return 0


def _cmd_list(args: argparse.Namespace) -> int:
    items = run_gws(list_argv(args.tasklist)).get("items", [])
    rows = items if isinstance(items, list) else []
    for item in rows:
        if isinstance(item, dict):
            print(f"TASK id={item.get('id', '')} status={item.get('status', '')}")
    print(f"LISTED tasklist={args.tasklist} count={len(rows)}")
    return 0


def _execute_confirmed_draft(draft: dict[str, Any]) -> CreatedTask:
    """Bind an owner-approved draft to a fresh gate record, then reuse create_task.

    Calls the unmodified, already-audited insert+re-read-verify path — this
    function's only new responsibility is writing the one approval record
    that path requires (see todo_gate.append_manual_reaction_approval).
    """
    request = TaskRequest(
        draft["tasklist"], draft["title"], draft.get("notes") or None, draft.get("due") or None
    )
    decision = evaluate(insert_argv(request), context=approval_context())
    todo_gate.append_manual_reaction_approval(
        action_hash=decision.action_hash,
        target_id=decision.target_id,
        owner_id=todo_confirm.owner_id(),
        message_ref=draft["message_id"],
    )
    return create_task(request)


def _cmd_draft_create(args: argparse.Namespace) -> int:
    draft = todo_gate.create_draft(
        title=args.title, notes=args.notes or "", due=args.due or "", tasklist=args.tasklist
    )
    channel_id, message_id = todo_confirm.post_confirmation_message(draft)
    draft = todo_gate.set_message_id(draft, message_id, channel_id)
    print(f"DRAFT-CREATED id={draft['id']} message={message_id}")
    return 0


def _cmd_draft_confirm(args: argparse.Namespace) -> int:
    draft = todo_gate.load_draft(args.draft)
    action = todo_confirm.resolve_reaction(draft)
    if action == todo_confirm.CANCEL_EMOJI:
        todo_gate.discard_draft(draft["id"])
        print(f"DISCARDED draft={draft['id']} method=manual_reaction")
        return 0
    if action != todo_confirm.APPROVE_EMOJI:
        raise todo_gate.GateError("소유자 확정 반응이 없습니다", 1)
    task = _execute_confirmed_draft(draft)
    todo_gate.mark_executed(draft, task_id=task.task_id)
    print(f"EXECUTED draft={draft['id']} task={task.task_id} method=manual_reaction")
    return 0


def _cmd_draft_discard(args: argparse.Namespace) -> int:
    todo_gate.discard_draft(args.draft)
    print(f"DISCARDED draft={args.draft}")
    return 0


def _cmd_list_drafts(_args: argparse.Namespace) -> int:
    for record in todo_gate.list_drafts():
        print(
            f"DRAFT id={record['id']} status={record['status']} title={record['title']} "
            f"tasklist={record['tasklist']} message={record.get('message_id') or 'unposted'} "
            f"created={record['created']}"
        )
    return 0


def _cmd_watch(_args: argparse.Namespace) -> int:
    """Production cron tick: repost pending, resolve ✅/⛔, create approved."""
    for draft in todo_gate.list_drafts():
        if draft.get("status") != "pending":
            continue
        if not draft.get("message_id"):
            channel_id, message_id = todo_confirm.post_confirmation_message(draft)
            draft = todo_gate.set_message_id(draft, message_id, channel_id)
            print(f"REPOSTED draft={draft['id']} message={message_id}")
        try:
            action = todo_confirm.resolve_reaction(draft)
        except todo_gate.GateError as error:
            if error.exit_code != 1:
                raise
            continue  # 승인 없음 → pending 유지
        except OSError as error:  # 429/네트워크 등 일시 오류 — draft 단위 격리, 다음 tick 재시도
            print(f"REACTION-RETRY draft={draft['id']} err={error}", file=sys.stderr)
            continue
        if action == todo_confirm.CANCEL_EMOJI:
            todo_gate.discard_draft(draft["id"])
            todo_confirm.dm_owner(f"할일 생성 취소됨: {draft['title']}")
            print(f"CANCELLED draft={draft['id']} method=manual_reaction")
            continue
        if action != todo_confirm.APPROVE_EMOJI:
            continue
        task = _execute_confirmed_draft(draft)
        todo_gate.mark_executed(draft, task_id=task.task_id)
        todo_confirm.dm_owner(f"✅ 할일 생성 완료: {draft['title']} (task {task.task_id})")
        print(f"EXECUTED draft={draft['id']} task={task.task_id} method=manual_reaction")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="todo_cli", description="승인 게이트 경유 Google Tasks")
    subparsers = parser.add_subparsers(dest="command", required=True)
    for name, handler in (("plan", _cmd_plan), ("create", _cmd_create)):
        sub = subparsers.add_parser(name)
        sub.add_argument("--title", required=True)
        sub.add_argument("--tasklist", default="@default")
        sub.add_argument("--notes", default=None)
        sub.add_argument("--due", default=None)
        sub.set_defaults(handler=handler)
    listing = subparsers.add_parser("list")
    listing.add_argument("--tasklist", default="@default")
    listing.set_defaults(handler=_cmd_list)

    draft_create = subparsers.add_parser("draft-create", help="생성 초안 + 승인 DM 게시 (Tasks에 아무것도 쓰지 않음)")
    draft_create.add_argument("--title", required=True)
    draft_create.add_argument("--tasklist", default="@default")
    draft_create.add_argument("--notes", default=None)
    draft_create.add_argument("--due", default=None)
    draft_create.set_defaults(handler=_cmd_draft_create)

    confirm = subparsers.add_parser("confirm", help="소유자 확인 검증 후에만 생성")
    confirm.add_argument("--draft", required=True)
    confirm.set_defaults(handler=_cmd_draft_confirm)

    discard = subparsers.add_parser("discard", help="초안 폐기 (확인 거부)")
    discard.add_argument("--draft", required=True)
    discard.set_defaults(handler=_cmd_draft_discard)

    subparsers.add_parser("list-drafts", help="초안 목록").set_defaults(handler=_cmd_list_drafts)
    subparsers.add_parser(
        "watch", help="프로덕션 cron tick: 승인/취소 처리 + 승인건 생성"
    ).set_defaults(handler=_cmd_watch)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return int(args.handler(args))
    except EntityClarificationError as error:
        if error.should_render:
            print(error)
        return error.exit_code
    except TodoError as error:
        print(f"TODO-FAIL {error}", file=sys.stderr)
        return error.exit_code
    except todo_gate.GateError as error:
        print(f"GATE-REFUSED {error}", file=sys.stderr)
        return error.exit_code


if __name__ == "__main__":
    raise SystemExit(main())
