#!/usr/bin/env python3
"""Mail triage process tick — Hermes cron job, no_agent script mode.

Thin wrapper: runs the mounted mail skill CLI `process` subcommand (syncs
new mailon.kr mail, gates+classifies, delegates schedule/todo detections to
the calendar/todo skills, drafts replies and posts them for owner approval —
no send: sending is mail-triage-watch's job once the owner reacts). no_agent
semantics: empty stdout + exit 0 on success (silent tick — actionable
results already surface as separate owner-DM approval posts, not through
this cron's own delivery channel); on failure prints one line and exits 1.
Deployed copy lives at ~/.hermes/scripts/mail_triage_process.py (Hermes
cron sandbox rule, same as mail_triage_watch.py); the skill CLI stays the
single implementation at ~/.hermes/skills/mail/scripts/ — subprocess only.
"""
from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

CLI = Path.home() / ".hermes" / "skills" / "mail" / "scripts" / "triage_cli.py"
LIMIT = "10"
_EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
_LONG_DIGITS = re.compile(r"\d{5,}")


def _redact(text: str) -> str:
    return _LONG_DIGITS.sub("[MASKED-NUM]", _EMAIL.sub("[MASKED-EMAIL]", text))


def main() -> int:
    if not CLI.exists():
        print("mail-triage-process error: mail skill is not mounted")
        return 1
    result = subprocess.run(  # noqa: S603 — fixed argv, agent-owned script
        [sys.executable, str(CLI), "process", "--limit", LIMIT],
        capture_output=True, text=True, timeout=1800, check=False,
        cwd=str(Path.home()),
    )
    if result.returncode == 0:
        return 0  # silent tick — actionable results surface via separate DMs
    tail = (result.stderr or result.stdout).strip().splitlines()
    detail = tail[-1] if tail else f"rc={result.returncode}"
    print(f"mail-triage-process error rc={result.returncode}: {_redact(detail)[:300]}")
    return 1


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as error:  # noqa: BLE001 — cron alert path: one masked line
        print(f"mail-triage-process error: {_redact(str(error))[:300]}")
        sys.exit(1)
