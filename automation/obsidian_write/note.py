"""Deterministic PARA placement and Obsidian callout rendering."""

from __future__ import annotations

import hashlib
import unicodedata
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Final

from automation import config_env

from .config import ObsidianWriteError

_PERSONAL_PARA_ROOT: Final = PurePosixPath("000_PARA")
_KIMM_PARA_ROOT: Final = PurePosixPath("001_KIMM_PARA")
_UNCLASSIFIED_INBOX: Final = _PERSONAL_PARA_ROOT / "Area" / "000_정리되지않은생각들"
_BUCKET_NAMES: Final = (
    ("project", "Project"),
    ("area", "Area"),
    ("resource", "Resource"),
    ("archive", "Archive"),
)
_FILENAME_LIMIT: Final = 80


@dataclass(frozen=True, slots=True)
class NotePlan:
    """A normalized, deterministic destination and the note's source content."""

    relpath: PurePosixPath
    title: str
    body: str
    tags: tuple[str, ...] = ()


def plan_note(
    title: str,
    body: str,
    *,
    institutional: bool,
    bucket_hint: str | None,
    subfolder: str | None = None,
    tags: tuple[str, ...] = (),
    date_prefix: str | None = None,
) -> NotePlan:
    """Normalize a note into one stable PARA path without writing it.

    ``subfolder`` nests further under the bucket (e.g. "Wired/Article") for
    callers that need a second structural axis. ``date_prefix`` (e.g. a
    YYMMDD string the caller computed) is prepended to the filename so notes
    sort chronologically; it is not derived here to keep this function pure.
    """
    normalized_title = _normalize_title(title)
    bucket = _recognized_bucket(bucket_hint)
    parent = _note_parent(institutional, bucket, subfolder)
    filename = _filename_for_title(normalized_title, date_prefix)
    return NotePlan(parent / filename, normalized_title, body.strip(), tags)


def render_note(plan: NotePlan, *, created: str, modified: str) -> str:
    """Render the vault's heading-and-callout format without YAML frontmatter."""
    root_tag = "#KIMM" if plan.relpath.parts[0] == _KIMM_PARA_ROOT.name else "#personal"
    tag_line = " ".join((root_tag, *plan.tags))
    return (
        f"# {plan.title}\n\n"
        ">[!info]\n"
        f"> Author: {config_env.owner_email()}\n"
        f"> Created: {created}\n"
        f"> Modified: {modified}\n"
        f"> Location: {plan.relpath.parent.as_posix()}\n"
        f"> Tag: {tag_line}\n\n"
        f"{plan.body}\n"
    )


def _normalize_title(title: str) -> str:
    normalized = " ".join(unicodedata.normalize("NFKC", title).split())
    if not normalized:
        raise ObsidianWriteError("Obsidian note title is empty", False)
    return normalized


def _recognized_bucket(bucket_hint: str | None) -> str | None:
    if bucket_hint is None:
        return None
    normalized_hint = unicodedata.normalize("NFKC", bucket_hint).strip().casefold()
    for hint, bucket in _BUCKET_NAMES:
        if normalized_hint == hint:
            return bucket
    return None


def _note_parent(institutional: bool, bucket: str | None, subfolder: str | None) -> PurePosixPath:
    if bucket is None:
        return _UNCLASSIFIED_INBOX
    root = _KIMM_PARA_ROOT if institutional else _PERSONAL_PARA_ROOT
    parent = root / bucket
    if subfolder:
        for segment in _sanitize_subfolder(subfolder):
            parent = parent / segment
    return parent


def _sanitize_subfolder(subfolder: str) -> list[str]:
    segments = []
    for raw_segment in subfolder.split("/"):
        cleaned = "".join(
            character if character.isalnum() or character in {" ", "-", "_"} else " "
            for character in raw_segment
        )
        segment = "-".join(cleaned.split())[:_FILENAME_LIMIT].strip("-_")
        if segment:
            segments.append(segment)
    return segments


def _filename_for_title(title: str, date_prefix: str | None) -> str:
    filename_stem = "".join(
        character if character.isalnum() or character in {" ", "-", "_"} else " "
        for character in title
    )
    normalized_stem = "-".join(filename_stem.split())[:_FILENAME_LIMIT].strip("-_")
    safe_stem = normalized_stem or "note"
    digest = hashlib.sha256(title.encode("utf-8")).hexdigest()[:12]
    prefix = f"{date_prefix}_" if date_prefix else ""
    return f"{prefix}{safe_stem}--{digest}.md"
