"""Pure logic for the W4-2 mail triage pipeline: LLM response parsing, prompt
building, masking, sanitized approval rendering, and external-effect hashing.

No I/O, no subprocess, no network — everything here is pytest-able.

Pipeline order contract (constraint 6): the deterministic sensitivity gate
(triage_sensitivity) runs FIRST on subject+sender+full body; only then may an
LLM see mail content, and a sensitivity hit forces the non-GLM tier.
"""

from __future__ import annotations

import hashlib
import json
import mimetypes
import re
import shlex
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import assert_never

PIPELINE_VERSION = "mail-triage-v1"
CATEGORIES = ("important", "normal", "spam")
FLAG_KEYS = ("reply_needed", "schedule_needed", "budget", "todo_needed")
PROMPT_BODY_LIMIT = 6000
_PROMPT_MARKER = "<<<PROMPT>>>"
INSTRUCTION_DEFAULT = "(별도 지시 없음)"
MAX_ATTACHMENT_COUNT = 10
MAX_ATTACHMENT_BYTES = 25 * 1024 * 1024
MAX_ATTACHMENT_TOTAL_BYTES = 25 * 1024 * 1024
BLOCKED_ATTACHMENT_SUFFIXES = frozenset(
    {".bat", ".cmd", ".com", ".exe", ".js", ".msi", ".ps1", ".scr"}
)

EXTERNAL_EFFECT_TOOL = "python3"
EXTERNAL_EFFECT_RULE_ID = "site_mail_backend_send"
EXTERNAL_EFFECT_TARGET_ID = f"tool:{EXTERNAL_EFFECT_RULE_ID}:{EXTERNAL_EFFECT_TOOL}"

_EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
_LONG_DIGITS = re.compile(r"\d{5,}")
_ADDR = re.compile(r"<?([A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,})>?")


class LlmParseError(ValueError):
    """The LLM response does not contain the required JSON contract."""


class AttachmentPolicyError(ValueError):
    """Stable, path-safe attachment validation failure."""

    def __init__(self, message: str, error_code: str = "attachment_invalid") -> None:
        super().__init__(message)
        self.error_code = error_code


@dataclass(frozen=True, slots=True)
class Classification:
    """Validated triage verdict for one mail."""

    category: str
    reply_needed: bool
    schedule_needed: bool
    budget: bool
    schedule_text: str
    reason: str
    todo_needed: bool = False
    todo_text: str = ""

    def flags(self) -> tuple[str, ...]:
        pairs = zip(
            FLAG_KEYS,
            (self.reply_needed, self.schedule_needed, self.budget, self.todo_needed),
        )
        return tuple(key for key, value in pairs if value)


def first_json_object(raw: str) -> dict:
    """Extract the first balanced JSON object from raw LLM text."""
    start = raw.find("{")
    if start < 0:
        raise LlmParseError("no JSON object in LLM response")
    depth, end, in_string, escape = 0, -1, False, False
    for index in range(start, len(raw)):
        char = raw[index]
        if in_string:
            if escape:
                escape = False
            elif char == "\\":
                escape = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                end = index
                break
    if end < 0:
        raise LlmParseError("unbalanced JSON object in LLM response")
    try:
        payload = json.loads(raw[start : end + 1])
    except json.JSONDecodeError as error:
        raise LlmParseError(f"invalid JSON: {error}") from error
    if not isinstance(payload, dict):
        raise LlmParseError("LLM response JSON is not an object")
    return payload


def _json_bool(value: object) -> bool:
    """Coerce an LLM JSON field to bool WITHOUT Python truthiness traps.

    glm-5.2 sometimes emits booleans as strings; ``bool("false")`` is ``True``,
    which would spuriously trip a flag (e.g. delegate a calendar draft). Only a
    real JSON ``true`` or the string ``"true"`` (case-insensitive) is True.
    """
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() == "true"
    return False


def parse_classification(raw: str) -> Classification:
    payload = first_json_object(raw)
    category = str(payload.get("category") or "").strip().lower()
    if category not in CATEGORIES:
        raise LlmParseError(f"category must be one of {CATEGORIES}, got {category!r}")
    return Classification(
        category=category,
        reply_needed=_json_bool(payload.get("reply_needed")),
        schedule_needed=_json_bool(payload.get("schedule_needed")),
        budget=_json_bool(payload.get("budget")),
        schedule_text=str(payload.get("schedule_text") or "").strip(),
        reason=str(payload.get("reason") or "").strip(),
        todo_needed=_json_bool(payload.get("todo_needed")),
        todo_text=str(payload.get("todo_text") or "").strip(),
    )


def parse_reply(raw: str) -> tuple[str, str]:
    """Return (subject, body) from the reply-draft LLM response."""
    payload = first_json_object(raw)
    subject = str(payload.get("subject") or "").strip()
    body = str(payload.get("body") or "").strip()
    if not body:
        raise LlmParseError("reply draft has an empty body")
    return subject, body


def parse_digest_summary(raw: str) -> str:
    """Return the one-line summary from the digest-summary LLM response."""
    payload = first_json_object(raw)
    summary = str(payload.get("summary") or "").strip()
    if not summary:
        raise LlmParseError("digest summary is empty")
    return summary


def load_prompt_template(path) -> str:
    """Prompt body below the marker LINE (line-anchored — W2-3 lesson)."""
    lines = path.read_text(encoding="utf-8").splitlines()
    for index, line in enumerate(lines):
        if line.strip() == _PROMPT_MARKER:
            return "\n".join(lines[index + 1 :]).strip()
    raise ValueError(f"prompt file missing {_PROMPT_MARKER} line: {path}")


def build_prompt(
    template: str, *, subject: str, sender: str, body: str, instruction: str = ""
) -> str:
    for placeholder in ("{{SUBJECT}}", "{{SENDER}}", "{{BODY}}"):
        if placeholder not in template:
            raise ValueError(f"prompt template missing {placeholder}")
    if "{{INSTRUCTION}}" in template:
        template = template.replace(
            "{{INSTRUCTION}}", instruction.strip() or INSTRUCTION_DEFAULT
        )
    elif instruction.strip():
        # Fail closed: an owner instruction must never be silently dropped.
        raise ValueError("prompt template missing {{INSTRUCTION}} for a non-empty instruction")
    return (
        template.replace("{{SUBJECT}}", subject)
        .replace("{{SENDER}}", sender)
        .replace("{{BODY}}", body[:PROMPT_BODY_LIMIT])
    )


def mask_value(value: str, salt: str = "") -> str:
    """Opaque id parity with the W4-1 wrapper masking (`sha256:<hex16>`)."""
    digest = hashlib.sha256((salt + value).encode("utf-8")).hexdigest()
    return f"sha256:{digest[:16]}"


def redact(text: str) -> str:
    """Mask emails and long digit runs in error/report lines."""
    return _LONG_DIGITS.sub("[MASKED-NUM]", _EMAIL.sub("[MASKED-EMAIL]", text))


def extract_reply_address(sender: str) -> str:
    """Bare address from a `Name <addr>` / `addr` sender string ('' if none)."""
    match = _ADDR.search(sender or "")
    return match.group(1) if match else ""


def reply_subject(llm_subject: str, mail_subject: str) -> str:
    if llm_subject:
        return llm_subject
    base = (mail_subject or "").strip()
    return base if base.lower().startswith("re:") else f"Re: {base}".strip()


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def build_attachment_manifest(paths: tuple[str | Path, ...]) -> list[dict]:
    """Validate private local files and return an approval-bound manifest.

    File content and source paths remain in the mode-700 draft store. Only the
    safe display metadata is rendered to Discord or audit surfaces.
    """
    if len(paths) > MAX_ATTACHMENT_COUNT:
        raise AttachmentPolicyError(
            f"첨부는 최대 {MAX_ATTACHMENT_COUNT}개까지 지원합니다",
            "attachment_unsupported",
        )
    manifest: list[dict] = []
    total = 0
    for raw_path in paths:
        try:
            path = Path(raw_path).expanduser().resolve(strict=True)
        except (FileNotFoundError, OSError) as error:
            raise AttachmentPolicyError("첨부파일을 읽을 수 없습니다") from error
        if not path.is_file():
            raise AttachmentPolicyError("첨부 대상이 일반 파일이 아닙니다")
        display_name = path.name
        if not display_name or any(ord(char) < 32 for char in display_name):
            raise AttachmentPolicyError("첨부파일 이름이 올바르지 않습니다")
        if path.suffix.lower() in BLOCKED_ATTACHMENT_SUFFIXES:
            raise AttachmentPolicyError(
                "보안 정책상 지원하지 않는 첨부파일 형식입니다",
                "attachment_unsupported",
            )
        try:
            size_bytes = path.stat().st_size
        except OSError as error:
            raise AttachmentPolicyError("첨부파일을 읽을 수 없습니다") from error
        if size_bytes > MAX_ATTACHMENT_BYTES:
            raise AttachmentPolicyError(
                f"첨부파일 한 개의 최대 크기는 {MAX_ATTACHMENT_BYTES}바이트입니다",
                "attachment_unsupported",
            )
        total += size_bytes
        if total > MAX_ATTACHMENT_TOTAL_BYTES:
            raise AttachmentPolicyError(
                f"전체 첨부파일의 최대 크기는 {MAX_ATTACHMENT_TOTAL_BYTES}바이트입니다",
                "attachment_unsupported",
            )
        try:
            content_sha256 = _file_sha256(path)
        except OSError as error:
            raise AttachmentPolicyError("첨부파일을 읽을 수 없습니다") from error
        mime_type = mimetypes.guess_type(display_name, strict=False)[0] or "application/octet-stream"
        manifest.append(
            {
                "source_path_private": str(path),
                "display_name": display_name,
                "size_bytes": size_bytes,
                "mime_type": mime_type,
                "sha256": content_sha256,
            }
        )
    return manifest


def attachment_manifest_sha256(manifest: list[dict]) -> str:
    """Digest upload-relevant metadata without exposing the private source path."""
    public_manifest = [
        {
            "display_name": item["display_name"],
            "size_bytes": item["size_bytes"],
            "mime_type": item["mime_type"],
            "sha256": item["sha256"],
        }
        for item in manifest
    ]
    canonical = json.dumps(
        public_manifest, ensure_ascii=False, separators=(",", ":"), sort_keys=True
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def verify_attachment_manifest(manifest: list[dict], expected_sha256: str) -> None:
    """Re-read approved files immediately before upload and fail on any drift."""
    try:
        current = build_attachment_manifest(
            tuple(item["source_path_private"] for item in manifest)
        )
    except (KeyError, TypeError) as error:
        raise AttachmentPolicyError("첨부 manifest 형식이 올바르지 않습니다") from error
    if current != manifest or attachment_manifest_sha256(current) != expected_sha256:
        raise AttachmentPolicyError("승인 후 첨부파일이 변경되어 발송을 중단했습니다")


def external_effect_action_hash(argv: tuple[str, ...]) -> str:
    """Hash-parity with automation.interop.external_effect_gate._action_hash."""
    payload = {
        "action": "external_effect.tool_call",
        "arguments": {"command": shlex.join(argv)},
        "target_id": EXTERNAL_EFFECT_TARGET_ID,
        "tool_name": EXTERNAL_EFFECT_TOOL,
    }
    canonical = json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    return f"sha256:{hashlib.sha256(canonical.encode('utf-8')).hexdigest()}"


def draft_sha256(record: dict) -> str:
    """Content hash binding a draft to the exact reply it will send."""
    bound = {
        key: record[key]
        for key in ("argv", "body", "sensitive", "subject", "to", "uid")
    }
    # Keep legacy/no-attachment draft hashes byte-for-byte compatible while
    # binding every new attachment draft to its full manifest.
    if "attachments" in record:
        bound["attachments"] = record["attachments"]
        bound["attachment_manifest_sha256"] = record["attachment_manifest_sha256"]
    if record.get("provider") == "gmail":
        bound["approval_action_hash"] = record["approval_action_hash"]
        bound["gmail_approval_snapshot"] = record["gmail_approval_snapshot"]
        bound["provider"] = "gmail"
        bound["reply_target"] = record["reply_target"]
        bound["sender_account"] = record["sender_account"]
    elif record.get("provider") == "site":
        bound["provider"] = "site"
        bound["site_backend_config_sha256"] = record["site_backend_config_sha256"]
    canonical = json.dumps(bound, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


class ApprovalRenderDestination(StrEnum):
    """The output surface that determines sensitive reply disclosure."""

    CONSOLE = "console"
    OWNER_DM = "owner-dm"


def render_approvals_message(
    draft: dict,
    *,
    destination: ApprovalRenderDestination = ApprovalRenderDestination.CONSOLE,
    instruction: str = "",
) -> str:
    """Render a draft for an explicit destination with console-safe defaults.

    Sensitive reply subjects and bodies are disclosed only for ``OWNER_DM``.
    ``CONSOLE`` is the default and emits metadata plus the draft hash only.
    """
    if draft.get("provider") == "gmail":
        lines = [
            "[mail-triage] Gmail 발송 승인 요청 (DM 확정)",
            f"- 발신 계정: `{draft['sender_account']}`",
            f"- 작업: `{draft['gmail_approval_snapshot']['action_kind']}`",
            f"- 수신자: `{draft['to']}`",
            f"- 회신 대상: `{draft['reply_target'] or '-'}`",
            f"- 제목: `{draft['subject']}`",
            "- 본문:",
            "```",
            draft["body"],
            "```",
        ]
    elif draft.get("kind") == "compose":
        lines = [
            "[mail-triage] 새 메일 발송 승인 요청 (DM 확정)",
            f"- To: `{draft['to']}`",
            f"- 제목: `{draft['subject']}`",
            "- 본문:",
            "```",
            draft["body"],
            "```",
        ]
    elif draft["sensitive"]:
        lines = [
            "[mail-triage] 민감 메일 회신 발송 승인 요청",
            f"- 유형: {draft['category']} / 태그: {', '.join(draft['tags']) or '-'}"
            f" / 플래그: {', '.join(draft['flags']) or '-'}",
            f"- 발신(마스킹): `{draft['sender_masked']}`",
            f"- 메일(불투명 id): `{draft['uid_opaque']}`",
        ]
        match destination:
            case ApprovalRenderDestination.OWNER_DM:
                lines.extend([
                    f"- 회신 제목: {draft['subject']}",
                    "- 회신 본문:",
                    "```",
                    draft["body"],
                    "```",
                ])
            case ApprovalRenderDestination.CONSOLE:
                pass
            case unreachable:
                assert_never(unreachable)
    else:
        preview = draft["body"][:600]
        lines = [
            "[mail-triage] 수신메일 회신 발송 승인 요청",
            f"- 분류: {draft['category']} / 플래그: {', '.join(draft['flags']) or '-'}",
            f"- 발신(마스킹): `{draft['sender_masked']}`",
            f"- 원문 제목: {draft['mail_subject']}",
            f"- 회신 제목: {draft['subject']}",
            "- 회신 본문:",
            "```",
            preview + ("…" if len(draft["body"]) > 600 else ""),
            "```",
    ]
    attachments = draft.get("attachments") or []
    if attachments:
        if draft.get("provider") == "gmail":
            lines.append(f"- 첨부: {len(attachments)}개")
            for item in attachments:
                safe_name = str(item["display_name"]).replace("`", "'")
                lines.append(
                    f"  - `{safe_name}` · {item['size_bytes']} bytes · `{item['sha256']}`"
                )
        elif draft.get("sensitive") and draft.get("kind") != "compose":
            lines.append(f"- 첨부: {len(attachments)}개")
        else:
            lines.append(f"- 첨부: {len(attachments)}개")
            for item in attachments:
                safe_name = str(item["display_name"]).replace("`", "'")
                lines.append(
                    f"  - `{safe_name}` · {item['size_bytes']} bytes · `{item['mime_type']}`"
                )
    lines.append(f"- draft: `{draft['id']}` sha256: `{draft['sha256']}`")
    if draft.get("provider") == "gmail":
        lines.append(f"- action hash: `{draft['approval_action_hash']}`")
    if instruction:
        lines.append(f"- 반응(기본): {instruction}")
    return "\n".join(lines)


def utc_now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
