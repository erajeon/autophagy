"""Read-only approval-surface migration drain audit."""
from __future__ import annotations

import argparse
import hashlib
import json
import stat as stat_module
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from types import MappingProxyType
from typing import Final, Protocol, TypeAlias, TypedDict, assert_never

from automation import config_env
from automation.interop.approval_surface import (
    POLICY_VERSION,
    ApprovalKind,
    ApprovalSurface,
    ApprovalSurfaceError,
    surface_at_policy,
)

JsonValue: TypeAlias = str | int | float | bool | None | list["JsonValue"] | dict[str, "JsonValue"]


class JsonLoader(Protocol):
    def __call__(self, raw: str, /) -> JsonValue: ...


class RecordPayload(TypedDict):
    id: str
    kind: str
    surface: str
    policy_version: int
    state: str


class FlowPayload(TypedDict):
    root: str
    missing: bool
    total: int
    non_terminal: int
    guild_bound_non_terminal: int
    blocking: int
    records: list[RecordPayload]


class AuditPayload(TypedDict):
    policy_version: int
    flows: dict[str, FlowPayload]


class Flow(StrEnum):
    MAIL = "mail"
    BUDGET = "budget"
    PATENT = "patent"
    REPAIR = "repair"
    DRIVE = "drive"


class AuditRecordError(RuntimeError):
    pass


_JSON_LOADS: Final[JsonLoader] = json.loads
_TERMINAL_STATES: Final = frozenset({"approved", "cancelled", "consumed", "executed", "sent", "blocked", "superseded", "failed"})
_DEFAULT_KINDS: Final[Mapping[Flow, ApprovalKind]] = MappingProxyType({
    Flow.MAIL: ApprovalKind.MAIL_REPLY,
    Flow.BUDGET: ApprovalKind.BUDGET_MAIL,
    Flow.PATENT: ApprovalKind.PATENT_EXPORT,
    Flow.REPAIR: ApprovalKind.REPAIR,
    Flow.DRIVE: ApprovalKind.DRIVE_ARCHIVE,
})
_KIND_ALIASES: Final[Mapping[Flow, Mapping[str, ApprovalKind]]] = MappingProxyType({
    Flow.MAIL: MappingProxyType({"mail-reply": ApprovalKind.MAIL_REPLY, "reply": ApprovalKind.MAIL_REPLY, "mail-compose": ApprovalKind.MAIL_COMPOSE, "compose": ApprovalKind.MAIL_COMPOSE}),
    Flow.BUDGET: MappingProxyType({"budget-mail": ApprovalKind.BUDGET_MAIL}),
    Flow.PATENT: MappingProxyType({"patent-export": ApprovalKind.PATENT_EXPORT}),
    Flow.REPAIR: MappingProxyType({"repair": ApprovalKind.REPAIR}),
    Flow.DRIVE: MappingProxyType({"drive-archive": ApprovalKind.DRIVE_ARCHIVE}),
})
DEFAULT_ROOTS: Mapping[Flow, Path | None] = MappingProxyType({
    Flow.MAIL: Path("~/.hermes/mail-triage/drafts").expanduser(),
    Flow.BUDGET: Path("~/.hermes/budget-gate/drafts").expanduser(),
    Flow.PATENT: Path("~/.hermes/patent-export").expanduser(),
    Flow.REPAIR: None,
    Flow.DRIVE: Path("~/.hermes/drive-archive/pending").expanduser(),
})


def _default_roots() -> Mapping[Flow, Path]:
    return {
        flow: config_env.private_root() / "repair/pending" if root is None else root
        for flow, root in DEFAULT_ROOTS.items()
    }


@dataclass(frozen=True, slots=True)
class AuditRecord:
    record_id: str
    kind: str
    surface: str
    policy_version: int
    state: str

    def to_json(self) -> RecordPayload:
        return {
            "id": self.record_id,
            "kind": self.kind,
            "surface": self.surface,
            "policy_version": self.policy_version,
            "state": self.state,
        }


@dataclass(frozen=True, slots=True)
class FlowAudit:
    root: Path
    missing: bool
    total: int
    non_terminal: int
    guild_bound_non_terminal: int
    blocking: int
    records: tuple[AuditRecord, ...]

    def to_json(self) -> FlowPayload:
        return {
            "root": str(self.root),
            "missing": self.missing,
            "total": self.total,
            "non_terminal": self.non_terminal,
            "guild_bound_non_terminal": self.guild_bound_non_terminal,
            "blocking": self.blocking,
            "records": [record.to_json() for record in self.records],
        }


@dataclass(frozen=True, slots=True)
class AuditReport:
    flows: Mapping[Flow, FlowAudit]

    def to_json(self) -> AuditPayload:
        return {
            "policy_version": POLICY_VERSION,
            # Only the flows this run covered. Enum order is kept so the payload
            # stays deterministic for the evidence artifacts that quote it.
            "flows": {flow.value: self.flows[flow].to_json() for flow in Flow if flow in self.flows},
        }


@dataclass(frozen=True, slots=True)
class StateRoot:
    flow: Flow
    root: Path


def _kind(flow: Flow, raw: JsonValue) -> ApprovalKind:
    if raw is None:
        return _DEFAULT_KINDS[flow]
    if not isinstance(raw, str):
        raise AuditRecordError("record kind is invalid")
    kind = _KIND_ALIASES[flow].get(raw.casefold().replace("_", "-"))
    if kind is None:
        raise AuditRecordError("record kind is unsupported")
    return kind


def _legacy_surface(kind: ApprovalKind, raw_channel: JsonValue) -> tuple[ApprovalSurface, int]:
    match raw_channel:
        case None | "":
            return surface_at_policy(kind, 0), 0
        case "dm":
            return ApprovalSurface.OWNER_DM, 0
        case "approvals":
            return ApprovalSurface.SKILL_APPROVALS, 0
        case str() as channel_id:
            if not channel_id.isdigit():
                raise AuditRecordError("legacy channel is invalid")
            return surface_at_policy(kind, 0), 0
        case unreachable:
            assert_never(unreachable)


def _binding(record: dict[str, JsonValue], kind: ApprovalKind) -> tuple[ApprovalSurface, int]:
    raw_surface = record.get("surface")
    raw_version = record.get("policy_version")
    if raw_surface is None and raw_version is None:
        return _legacy_surface(kind, record.get("channel_id"))
    if not isinstance(raw_surface, str) or not isinstance(raw_version, int) or isinstance(raw_version, bool):
        raise AuditRecordError("stored binding is incomplete")
    channel_id = record.get("channel_id")
    if not isinstance(channel_id, str) or not channel_id.isdigit():
        raise AuditRecordError("stored channel is invalid")
    try:
        surface = ApprovalSurface(raw_surface)
        expected = surface_at_policy(kind, raw_version)
    except (ApprovalSurfaceError, ValueError) as error:
        raise AuditRecordError("stored binding is invalid") from error
    if surface is not expected:
        raise AuditRecordError("stored binding contradicts policy")
    return surface, raw_version


def _state(record: dict[str, JsonValue]) -> tuple[str, bool]:
    raw = record.get("state", record.get("status"))
    if raw is None:
        return "pending", True
    if not isinstance(raw, str) or not raw:
        raise AuditRecordError("record state is invalid")
    normalized = raw.casefold().replace("_", "-")
    if normalized in {"pending", "bound-pending"}:
        return raw, True
    if normalized in _TERMINAL_STATES:
        return raw, False
    raise AuditRecordError("record state is unknown")


def _record_id(record: dict[str, JsonValue], path: Path) -> str:
    raw = record.get("id")
    if isinstance(raw, str) and raw:
        return raw
    return hashlib.sha256(str(path).encode("utf-8")).hexdigest()[:16]


def _blocking(path: Path) -> AuditRecord:
    return AuditRecord(hashlib.sha256(str(path).encode("utf-8")).hexdigest()[:16], "", "", 0, "blocking")


def _classify(flow: Flow, path: Path, record: dict[str, JsonValue]) -> tuple[AuditRecord, bool]:
    kind = _kind(flow, record.get("kind"))
    surface, policy_version = _binding(record, kind)
    state, non_terminal = _state(record)
    return AuditRecord(_record_id(record, path), kind.value, surface.value, policy_version, state), non_terminal


def _paths(flow: Flow, root: Path) -> tuple[tuple[Path, ...], bool]:
    # `Path.exists()`/`is_dir()` swallow PermissionError on newer CPython (the
    # ignored-errno set pathlib treats as "doesn't exist" grew to match
    # os.path.exists()'s always-swallow behavior), which would silently turn an
    # unreadable root into `missing` instead of the `blocking` fail-closed path
    # below. Stat directly so only genuine absence is treated as `missing`.
    try:
        mode = root.stat().st_mode
    except (FileNotFoundError, NotADirectoryError):
        return (), True
    if not stat_module.S_ISDIR(mode):
        return (root,), False
    match flow:
        case Flow.MAIL:
            return tuple(sorted(path for directory in (root / "public", root / "sensitive") if directory.is_dir() for path in directory.glob("*.json"))), False
        case Flow.PATENT:
            return tuple(path for path in sorted(root.glob("*.json")) if path.name != "config.json"), False
        case Flow.BUDGET | Flow.REPAIR | Flow.DRIVE:
            return tuple(sorted(root.glob("*.json"))), False
        case unreachable:
            assert_never(unreachable)


def audit_flow(flow: Flow, root: Path) -> FlowAudit:
    try:
        paths, missing = _paths(flow, root)
    except OSError:
        # The root itself is unreadable -- repair's state is ops-owned, so the agent
        # account cannot even stat it. Fail closed the same way an unreadable RECORD
        # does: a gate that raises cannot report "clean", and this must never be
        # mistaken for `missing`, which means the root genuinely is not there.
        return FlowAudit(root, False, 1, 0, 0, 1, (_blocking(root),))
    records: list[AuditRecord] = []
    non_terminal = 0
    guild_bound = 0
    blocking = 0
    for path in paths:
        try:
            raw = _JSON_LOADS(path.read_text(encoding="utf-8"))
            if not isinstance(raw, dict):
                raise AuditRecordError("record is not an object")
            record, is_non_terminal = _classify(flow, path, raw)
        except (AuditRecordError, OSError, json.JSONDecodeError):
            records.append(_blocking(path))
            blocking += 1
            continue
        records.append(record)
        if is_non_terminal:
            non_terminal += 1
            if record.surface == ApprovalSurface.SKILL_APPROVALS.value:
                guild_bound += 1
    return FlowAudit(root, missing, len(records), non_terminal, guild_bound, blocking, tuple(records))


def audit_roots(roots: Mapping[Flow, Path]) -> AuditReport:
    # Iterate what the caller asked for, not every Flow: state is split by account
    # (repair is ops-owned, the rest agent-owned), so a report must be able to cover
    # exactly the flows the running account can actually see.
    return AuditReport({flow: audit_flow(flow, root) for flow, root in roots.items()})


def _flow_name(raw: str) -> Flow:
    try:
        return Flow(raw)
    except ValueError as error:
        raise argparse.ArgumentTypeError(f"unknown approval flow: {raw}") from error


def _state_root(raw: str) -> StateRoot:
    name, separator, path = raw.partition("=")
    if not separator or not path:
        raise argparse.ArgumentTypeError("--state-root requires FLOW=PATH")
    try:
        return StateRoot(Flow(name), Path(path).expanduser())
    except ValueError as error:
        raise argparse.ArgumentTypeError(f"unknown approval flow: {name}") from error


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--state-root", action="append", default=[], type=_state_root)
    parser.add_argument("--flow", action="append", default=[], type=_flow_name)
    parser.add_argument("--fail-on-guild-bound", action="store_true")
    args = parser.parse_args(argv)
    selected = tuple(dict.fromkeys(args.flow)) or tuple(Flow)
    defaults = _default_roots()
    roots = {flow: defaults[flow] for flow in selected}
    for override in args.state_root:
        if override.flow not in roots:
            parser.error(f"--state-root {override.flow.value}= names a flow excluded by --flow")
        roots[override.flow] = override.root
    report = audit_roots(roots)
    print(json.dumps(report.to_json(), sort_keys=True))
    has_blocker = any(item.guild_bound_non_terminal > 0 or item.blocking > 0 for item in report.flows.values())
    return 1 if args.fail_on_guild_bound and has_blocker else 0


if __name__ == "__main__":
    raise SystemExit(main())
