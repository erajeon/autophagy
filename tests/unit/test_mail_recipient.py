from __future__ import annotations

import sys
from pathlib import Path

import pytest
from tests.unit._synthetic import OWNER_EMAIL

_REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_REPO / "skills" / "mail" / "scripts"))

import triage_recipient  # noqa: E402

OWNER = OWNER_EMAIL

FRONTMATTER_CC = """---
uid: "8580001"
folder: "inbox"
subject: "외자구매 신청 요청 (synthetic)"
from: "sender@inst.example"
to: "other@example.invalid"
cc: "owner@example.invalid, third@example.invalid"
date: "2026-07-16T08:23:00"
---

# 본문

**To**: other@example.invalid

## Body
synthetic body
"""

FRONTMATTER_TO = FRONTMATTER_CC.replace(
    'to: "other@example.invalid"', 'to: "Owner <owner@example.invalid>"'
).replace('cc: "owner@example.invalid, third@example.invalid"', 'cc: ""')


class TestParseRecipients:
    def test_parses_to_and_cc_addresses(self) -> None:
        to, cc = triage_recipient.parse_recipients(FRONTMATTER_CC)
        assert to == ("other@example.invalid",)
        assert cc == ("owner@example.invalid", "third@example.invalid")

    def test_name_bracket_form_and_empty_cc(self) -> None:
        to, cc = triage_recipient.parse_recipients(FRONTMATTER_TO)
        assert to == ("owner@example.invalid",)
        assert cc == ()

    def test_no_frontmatter_returns_empty(self) -> None:
        assert triage_recipient.parse_recipients("plain body, no frontmatter") == ((), ())

    def test_body_to_line_outside_frontmatter_is_ignored(self) -> None:
        # The `**To**:` rendering inside the body must not leak into parsing.
        text = "---\nuid: \"1\"\nto: \"a@x.example\"\ncc: \"\"\n---\n\n**To**: b@y.example\ncc: c@z.example\n"
        to, cc = triage_recipient.parse_recipients(text)
        assert to == ("a@x.example",)
        assert cc == ()


class TestRecipientRole:
    def test_owner_in_to_is_to(self) -> None:
        assert triage_recipient.recipient_role(FRONTMATTER_TO, OWNER) == "to"

    def test_owner_only_in_cc_is_cc(self) -> None:
        assert triage_recipient.recipient_role(FRONTMATTER_CC, OWNER) == "cc"

    def test_owner_in_both_prefers_to(self) -> None:
        both = FRONTMATTER_CC.replace(
            'to: "other@example.invalid"', 'to: "owner@example.invalid"'
        )
        assert triage_recipient.recipient_role(both, OWNER) == "to"

    def test_case_insensitive_match(self) -> None:
        assert triage_recipient.recipient_role(FRONTMATTER_CC, "OWNER@EXAMPLE.INVALID") == "cc"

    def test_owner_absent_is_unknown(self) -> None:
        assert triage_recipient.recipient_role(FRONTMATTER_CC, "nobody@example.invalid") == "unknown"

    def test_empty_owner_is_unknown(self) -> None:
        assert triage_recipient.recipient_role(FRONTMATTER_CC, "") == "unknown"

    def test_unparsable_body_is_unknown(self) -> None:
        assert triage_recipient.recipient_role("no frontmatter here", OWNER) == "unknown"


class TestOwnerAddress:
    def test_owner_email_config_is_used(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("OWNER_EMAIL", OWNER_EMAIL)
        assert triage_recipient.owner_address() == OWNER_EMAIL

    def test_missing_owner_email_is_empty(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("OWNER_EMAIL", raising=False)
        assert triage_recipient.owner_address() == ""


# --- pipeline integration: cc mail must not auto-draft a reply ---------------

import triage_core  # noqa: E402
import triage_llm  # noqa: E402
import triage_pipeline  # noqa: E402
import triage_sensitivity  # noqa: E402


def _gate_stub(text, rules):  # noqa: ARG001 — signature parity
    return triage_sensitivity.GateResult(sensitive=False, tags=(), matched=())


def _classify_stub(**kwargs):  # noqa: ARG001
    cls = triage_core.Classification(
        category="important", reply_needed=True, schedule_needed=False,
        budget=False, schedule_text="", reason="synthetic",
    )
    return cls, "stub-provider"


def _mail_detail(body: str) -> dict:
    return {
        "uid": "u-role", "subject": "Synthetic subject",
        "sender": "발신자 <sender@inst.example>", "body": body,
    }


def test_process_one_cc_only_mail_skips_reply_draft(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given: an important reply_needed verdict but the owner is only a Cc recipient
    monkeypatch.setenv("OWNER_EMAIL", OWNER)
    monkeypatch.setattr(triage_pipeline, "_get_mail", lambda _uid: _mail_detail(FRONTMATTER_CC))
    monkeypatch.setattr(triage_sensitivity, "evaluate", _gate_stub)
    monkeypatch.setattr(triage_llm, "classify", _classify_stub)
    monkeypatch.setattr(
        triage_pipeline, "_draft_and_post",
        lambda *args, **kwargs: pytest.fail("cc-only mail must not auto-draft a reply"),
    )
    # When: the process path handles the mail
    action, sensitive, category = triage_pipeline._process_one("u-role", (), post=False)
    # Then: the reply draft is suppressed and the suppression is visible in the action
    assert "cc-no-reply" in action
    assert category == "important" and sensitive is False


def test_process_one_to_recipient_sends_todo_reminder_by_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given: the same verdict with the owner as a To recipient, reply-draft paused (default)
    monkeypatch.setenv("OWNER_EMAIL", OWNER)
    monkeypatch.delenv("TRIAGE_REPLY_DRAFT_ENABLED", raising=False)
    monkeypatch.setattr(triage_pipeline, "_get_mail", lambda _uid: _mail_detail(FRONTMATTER_TO))
    monkeypatch.setattr(triage_sensitivity, "evaluate", _gate_stub)
    monkeypatch.setattr(triage_llm, "classify", _classify_stub)
    monkeypatch.setattr(
        triage_pipeline, "_draft_and_post",
        lambda *args, **kwargs: pytest.fail("reply auto-draft is paused by default"),
    )
    reminders: list[str] = []
    monkeypatch.setattr(
        triage_pipeline, "_delegate_todo",
        lambda text, _uid_opaque: reminders.append(text) or "todo:stub",
    )
    # When: the process path handles the mail
    action, _sensitive, _category = triage_pipeline._process_one("u-role", (), post=False)
    # Then: a "회신 필요" todo reminder is delegated instead of drafting a reply
    assert reminders == ["메일 회신 필요: Synthetic subject"]
    assert "todo:stub" in action


def test_process_one_to_recipient_still_drafts_when_reenabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given: the same verdict, but the owner re-enabled reply auto-drafting
    monkeypatch.setenv("OWNER_EMAIL", OWNER)
    monkeypatch.setenv("TRIAGE_REPLY_DRAFT_ENABLED", "1")
    monkeypatch.setattr(triage_pipeline, "_get_mail", lambda _uid: _mail_detail(FRONTMATTER_TO))
    monkeypatch.setattr(triage_sensitivity, "evaluate", _gate_stub)
    monkeypatch.setattr(triage_llm, "classify", _classify_stub)
    drafted: list[str] = []
    monkeypatch.setattr(
        triage_pipeline, "_draft_and_post",
        lambda *args, **kwargs: drafted.append("called") or ["draft:stub"],
    )
    # When: the process path handles the mail
    action, _sensitive, _category = triage_pipeline._process_one("u-role", (), post=False)
    # Then: the reply draft path runs as before the pause
    assert drafted == ["called"]
    assert "draft:stub" in action

NOW = "2026-07-20T09:00:00Z"


def _sent_compose(created: str, to: str, subject: str = "내일 오후 12시 탁구 — 회신 요청",
                  *, kind: str = "compose", status: str = "executed") -> dict:
    return {"kind": kind, "status": status, "created": created,
            "to": to, "subject": subject}


class TestRelatedRecipientGap:
    def test_reports_recipients_dropped_from_recent_related_send(self) -> None:
        drafts = [_sent_compose("2026-07-20T08:00:00Z",
                                "person-alpha@example.invalid, person-beta@example.invalid, person-gamma@example.invalid")]
        gap = triage_recipient.related_recipient_gap(
            "person-alpha@example.invalid, person-beta@example.invalid", "[확정] 내일 오후 3시 탁구", drafts, now_utc=NOW)
        assert gap == ["person-gamma@example.invalid"]

    def test_outside_window_is_ignored(self) -> None:
        drafts = [_sent_compose("2026-07-19T08:00:00Z", "person-alpha@example.invalid, person-gamma@example.invalid")]
        assert triage_recipient.related_recipient_gap(
            "person-alpha@example.invalid", "[확정] 내일 오후 3시 탁구", drafts,
            now_utc=NOW, window_hours=24) == []

    def test_non_executed_or_non_compose_excluded(self) -> None:
        drafts = [
            _sent_compose("2026-07-20T08:00:00Z", "person-alpha@example.invalid, person-gamma@example.invalid", status="pending"),
            _sent_compose("2026-07-20T08:00:00Z", "person-alpha@example.invalid, person-delta@example.invalid", kind="reply"),
        ]
        assert triage_recipient.related_recipient_gap(
            "person-alpha@example.invalid", "[확정] 내일 오후 3시 탁구", drafts, now_utc=NOW) == []

    def test_unrelated_subject_excluded(self) -> None:
        drafts = [_sent_compose("2026-07-20T08:00:00Z", "person-alpha@example.invalid, person-gamma@example.invalid",
                                subject="정전 사전 안내")]
        assert triage_recipient.related_recipient_gap(
            "person-alpha@example.invalid", "[확정] 탁구 일정", drafts, now_utc=NOW) == []

    def test_malformed_created_skipped(self) -> None:
        drafts = [_sent_compose("not-a-date", "person-alpha@example.invalid, person-gamma@example.invalid")]
        assert triage_recipient.related_recipient_gap(
            "person-alpha@example.invalid", "[확정] 내일 오후 3시 탁구", drafts, now_utc=NOW) == []

    def test_case_and_whitespace_insensitive(self) -> None:
        drafts = [_sent_compose("2026-07-20T08:00:00Z", "PERSON-ALPHA@EXAMPLE.INVALID ,  PERSON-GAMMA@EXAMPLE.INVALID")]
        assert triage_recipient.related_recipient_gap(
            "person-alpha@example.invalid, person-gamma@example.invalid", "[확정] 내일 오후 3시 탁구", drafts, now_utc=NOW) == []
