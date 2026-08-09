#!/usr/bin/env python3
"""Save one note to the dedicated Obsidian write clone (owner opt-in, no per-note gate).

The owner explicitly chose to skip the built-in per-note approval gate for
this pipeline (2026-08-09, Wired digest notes — low-stakes reading material,
not financial/task actions). The gate itself (automation.obsidian_write.
gate_binding) is untouched; this script bypasses it the way the module
already supports — pointing OBSIDIAN_WRITE_DENYLIST at a denylist with no
rule matching ``obsidian_write.note_push``, so evaluate_tool_call sees no
external-effect match and allows the write with no approval record needed.
If OBSIDIAN_WRITE_DENYLIST is unset (falls back to the real repo denylist),
the write reverts to fully gated behavior — this script never weakens the
gate itself, only opts out per-deployment via its documented override knob.
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path


def _load_env_secrets(path: Path = Path.home() / ".env.secrets") -> None:
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

_repo_root = Path(os.environ.get("AUTOPHAGY_REPO_ROOT", Path(__file__).resolve().parents[2]))
if str(_repo_root) not in sys.path:
    sys.path.insert(0, str(_repo_root))

from automation import obsidian_write  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="Save one note to the dedicated Obsidian write clone")
    parser.add_argument("--title", required=True)
    parser.add_argument("--body-file", required=True, help="path to a file containing the note body")
    parser.add_argument("--bucket", default="resource", choices=("project", "area", "resource", "archive"))
    parser.add_argument("--institutional", action="store_true", help="place under 001_KIMM_PARA instead of 000_PARA")
    args = parser.parse_args()

    body = Path(args.body_file).read_text(encoding="utf-8")
    plan = obsidian_write.plan_note(
        args.title, body, institutional=args.institutional, bucket_hint=args.bucket
    )
    config = obsidian_write.load_config()
    try:
        receipt = obsidian_write.write_note(plan, config)
    except obsidian_write.ObsidianWriteError as error:
        print(f"OBSIDIAN-WRITE-FAIL retryable={error.retryable} {error}", file=sys.stderr)
        return 1
    print(f"OBSIDIAN-SAVED relpath={receipt.relpath} sha256={receipt.content_sha256}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
