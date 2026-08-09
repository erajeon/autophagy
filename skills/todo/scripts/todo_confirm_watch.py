#!/usr/bin/env python3
"""Todo draft watcher — Hermes cron job, no_agent script mode.

Thin wrapper: runs the mounted todo skill CLI `watch` subcommand (reposts
unposted pending drafts, resolves owner ✅/⛔ reactions, creates approved
Tasks — no auto-drafting: drafts are created only by mail-triage delegation
or a future owner-initiated path). no_agent semantics: empty stdout + exit 0
on success (silent tick); on failure prints one line and exits 1 so the
scheduler records an alert. Deployed copy lives at
~/.hermes/scripts/todo_confirm_watch.py (Hermes cron sandbox rule, same as
mail_triage_watch.py); the skill CLI stays the single implementation at
~/.hermes/skills/todo/scripts/ — no import of it here, subprocess only.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

CLI = Path.home() / ".hermes" / "skills" / "todo" / "scripts" / "todo_cli.py"


def main() -> int:
    if not CLI.exists():
        print("todo-confirm-watch error: todo skill is not mounted")
        return 1
    result = subprocess.run(  # noqa: S603 — fixed argv, agent-owned script
        [sys.executable, str(CLI), "watch"],
        capture_output=True, text=True, timeout=1800, check=False,
        cwd=str(Path.home()),
    )
    if result.returncode == 0:
        return 0  # silent tick — stdout intentionally dropped
    tail = (result.stderr or result.stdout).strip().splitlines()
    detail = tail[-1] if tail else f"rc={result.returncode}"
    print(f"todo-confirm-watch error rc={result.returncode}: {detail[:300]}")
    return 1


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as error:  # noqa: BLE001 — cron alert path: one line
        print(f"todo-confirm-watch error: {str(error)[:300]}")
        sys.exit(1)
