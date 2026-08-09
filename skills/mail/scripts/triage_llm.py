"""LLM routing for mail triage (W4-2, constraint 6).

Routing contract:
- non-sensitive classification -> LiteLLM ``glm-main`` (loopback gateway).
- sensitivity-gate HIT (patent etc.) -> the non-GLM quality tier, NEVER GLM.
- reply drafts (Korean final text) -> ALWAYS the non-GLM quality tier.

The plan's "sonnet-5" tier is realized as ``hermes -z … --provider
openai-codex -m gpt-5.4`` per the v2.2 model-policy reinterpretation
(Anthropic deferred; sonnet-5 mentions map to the openai-codex tier).
``call_glm`` refuses sensitive input outright, and LiteLLM's deployed
PatentSensitiveGlmBlocker (HTTP 403) is the fail-closed second layer.

Every call appends one masked line to the routing log (provider/model/
purpose/opaque uid) — the auditable GLM-call-count surface for QA.

Test hooks (never set in production units):
  TRIAGE_GLM_BIN     stub command: prompt on stdin, response on stdout.
  TRIAGE_HERMES_BIN  overrides the hermes binary for the codex one-shot.
"""

from __future__ import annotations

import fcntl
import json
import os
import shutil
import subprocess
import urllib.request
from pathlib import Path

import triage_core

GLM_MODEL = "glm-main"
NON_GLM_PROVIDER = "openai-codex"
ENV_SECRETS = Path.home() / ".env.secrets"


class PatentRoutingError(RuntimeError):
    """Raised when sensitivity-gate-hit text is about to reach a GLM tier."""


class LlmCallError(RuntimeError):
    """Raised when an LLM call fails at the transport level."""


def codex_model() -> str:
    return os.environ.get("TRIAGE_CODEX_MODEL", "gpt-5.4")


def _log_path() -> Path:
    return Path(
        os.environ.get("TRIAGE_LLM_LOG", "~/.hermes/mail-triage/logs/llm-calls.jsonl")
    ).expanduser()


def _log_call(*, provider: str, model: str, purpose: str, uid_opaque: str, sensitive: bool) -> None:
    path = _log_path()
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    record = {
        "model": model,
        "provider": provider,
        "purpose": purpose,
        "sensitive": sensitive,
        "timestamp": triage_core.utc_now(),
        "uid": uid_opaque,
    }
    with path.open("a", encoding="utf-8") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        handle.write(json.dumps(record, ensure_ascii=False, separators=(",", ":"), sort_keys=True) + "\n")
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    path.chmod(0o600)


def _litellm_key() -> str:
    key = os.environ.get("LITELLM_AGENT_KEY", "")
    if key:
        return key
    try:
        for line in ENV_SECRETS.read_text(encoding="utf-8").splitlines():
            if line.startswith("LITELLM_AGENT_KEY="):
                return line.split("=", 1)[1].strip()
    except OSError:
        pass
    raise LlmCallError("LITELLM_AGENT_KEY 없음 — glm-main 호출 불가")


def call_glm(prompt: str, *, sensitive: bool, timeout: float = 180.0) -> str:
    """Call LiteLLM glm-main for NON-sensitive triage only (fail closed)."""
    if sensitive:
        raise PatentRoutingError("sensitivity-gate hit — GLM tier is forbidden for this mail")
    stub = os.environ.get("TRIAGE_GLM_BIN", "")
    if stub:
        completed = subprocess.run(  # noqa: S603 — explicit test hook
            [stub], input=prompt.encode("utf-8"), capture_output=True,
            timeout=timeout, check=False,
        )
        if completed.returncode != 0:
            raise LlmCallError(f"glm stub failed rc={completed.returncode}")
        return completed.stdout.decode("utf-8", errors="replace")
    base_url = os.environ.get("TRIAGE_LITELLM_BASE_URL", "http://127.0.0.1:4000/v1")
    request = urllib.request.Request(
        f"{base_url.rstrip('/')}/chat/completions",
        data=json.dumps(
            {
                "model": GLM_MODEL,
                "messages": [{"role": "user", "content": prompt}],
                "temperature": 0,
                "metadata": {"tags": ["mail-triage"]},
            }
        ).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {_litellm_key()}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310
            payload = json.loads(response.read().decode("utf-8"))
    except OSError as error:
        raise LlmCallError(f"glm-main 호출 실패: {triage_core.redact(str(error))[:200]}") from None
    return payload["choices"][0]["message"]["content"]


def call_codex(prompt: str, *, timeout: float = 600.0) -> str:
    """Non-GLM quality tier via the hermes openai-codex one-shot.

    ``-t todo`` is load-bearing (W2-3): without an explicit harmless toolset
    the one-shot agent gets file/terminal tools and has edited local files.
    """
    binary = os.environ.get("TRIAGE_HERMES_BIN", "")
    if not binary:
        binary = shutil.which("hermes") or os.path.expanduser("~/.local/bin/hermes")
    completed = subprocess.run(  # noqa: S603
        [binary, "-z", prompt, "--provider", NON_GLM_PROVIDER, "-m", codex_model(), "-t", "todo"],
        capture_output=True, timeout=timeout,
        cwd=os.path.expanduser("~"), check=False,
    )
    if completed.returncode != 0:
        stderr = completed.stderr.decode("utf-8", errors="replace")[-300:]
        raise LlmCallError(
            f"codex one-shot failed rc={completed.returncode}: {triage_core.redact(stderr)}"
        )
    return completed.stdout.decode("utf-8", errors="replace")


def classify(
    *, subject: str, sender: str, body: str, sensitive: bool, uid_opaque: str, prompt_path: Path
) -> tuple[triage_core.Classification, str]:
    """Step-2 classification. Always the non-GLM tier (2026-08-09 owner decision).

    glm-main's LiteLLM model group is wired to an Anthropic key the owner
    deliberately deactivated as a cost stopgap (never reactivated — see the
    deferred LiteLLM-stack cleanup); every non-sensitive classify call was
    401ing. Routing everything through the owner's own ChatGPT Codex
    subscription (same as reply drafting and the Wired digest) removes the
    LiteLLM/Anthropic-key dependency entirely, at no extra API cost.
    """
    prompt = triage_core.build_prompt(
        triage_core.load_prompt_template(prompt_path),
        subject=subject, sender=sender, body=body,
    )
    raw, provider, model = call_codex(prompt), NON_GLM_PROVIDER, codex_model()
    _log_call(provider=provider, model=model, purpose="classify",
              uid_opaque=uid_opaque, sensitive=sensitive)
    return triage_core.parse_classification(raw), provider


def draft_reply(
    *, subject: str, sender: str, body: str, sensitive: bool, uid_opaque: str,
    prompt_path: Path, instruction: str = "",
) -> tuple[str, str, str]:
    """Step-3 Korean final-text reply — ALWAYS the non-GLM quality tier."""
    prompt = triage_core.build_prompt(
        triage_core.load_prompt_template(prompt_path),
        subject=subject, sender=sender, body=body, instruction=instruction,
    )
    raw = call_codex(prompt)
    _log_call(provider=NON_GLM_PROVIDER, model=codex_model(), purpose="draft_reply",
              uid_opaque=uid_opaque, sensitive=sensitive)
    llm_subject, reply_body = triage_core.parse_reply(raw)
    return triage_core.reply_subject(llm_subject, subject), reply_body, NON_GLM_PROVIDER


def summarize(
    *, subject: str, sender: str, body: str, sensitive: bool, uid_opaque: str, prompt_path: Path
) -> str:
    """One-line Korean digest summary. Route by the step-1 sensitivity verdict."""
    prompt = triage_core.build_prompt(
        triage_core.load_prompt_template(prompt_path),
        subject=subject, sender=sender, body=body,
    )
    if sensitive:
        raw, provider, model = call_codex(prompt), NON_GLM_PROVIDER, codex_model()
    else:
        raw, provider, model = call_glm(prompt, sensitive=False), GLM_MODEL, GLM_MODEL
    _log_call(provider=provider, model=model, purpose="digest_summary",
              uid_opaque=uid_opaque, sensitive=sensitive)
    return triage_core.parse_digest_summary(raw)
