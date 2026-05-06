"""Prompt-file edit history derived from git.

Reads ``git log --follow`` on the prompt file plus ``git show`` at each
commit to compute the content-hash of every historical revision. Cheap
to compute (single git invocation) and no migration needed.

Future: capture per-call prompt content_hash in trace events so an
iterator can correlate "this run's prompt matched commit X" without
having to re-derive. That's a separate task — needs touching the LLM
call sites.
"""

from __future__ import annotations

import hashlib
import logging
import subprocess
from pathlib import Path

from rumil.atlas.schemas import PromptHistory, PromptHistoryEntry
from rumil.prompts import PROMPTS_DIR

log = logging.getLogger(__name__)

_REPO_ROOT = Path(__file__).resolve().parents[3]


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def content_hash_for_file(path: Path) -> str:
    if not path.exists():
        return ""
    return _sha256(path.read_text(encoding="utf-8"))


def build_prompt_history(name: str, *, max_entries: int = 50) -> PromptHistory | None:
    if not name.endswith(".md"):
        name = f"{name}.md"
    path = PROMPTS_DIR / name
    if not path.exists():
        return None

    rel_path = path.resolve().relative_to(_REPO_ROOT)
    current_hash = content_hash_for_file(path)

    try:
        log_result = subprocess.run(
            [
                "git",
                "-C",
                str(_REPO_ROOT),
                "log",
                "--follow",
                "--no-merges",
                f"-n{max_entries + 1}",
                "--pretty=format:%H%x1f%h%x1f%aI%x1f%an%x1f%s",
                "--",
                str(rel_path),
            ],
            capture_output=True,
            text=True,
            check=True,
        )
    except (subprocess.CalledProcessError, FileNotFoundError) as exc:
        log.warning("git log failed for %s: %s", rel_path, exc)
        return PromptHistory(
            name=name,
            path=str(rel_path),
            current_content_hash=current_hash,
            entries=[],
        )

    entries: list[PromptHistoryEntry] = []
    for raw in log_result.stdout.strip().split("\n"):
        if not raw.strip():
            continue
        parts = raw.split("\x1f")
        if len(parts) != 5:
            continue
        sha, short, ts, author, subject = parts
        try:
            show = subprocess.run(
                ["git", "-C", str(_REPO_ROOT), "show", f"{sha}:{rel_path}"],
                capture_output=True,
                text=True,
                check=True,
            )
            body = show.stdout
        except subprocess.CalledProcessError:
            body = ""
        entries.append(
            PromptHistoryEntry(
                commit_sha=sha,
                commit_short=short,
                commit_ts=ts,
                author=author,
                subject=subject,
                content_hash=_sha256(body) if body else "",
                char_count=len(body),
            )
        )

    truncated = len(entries) > max_entries
    if truncated:
        entries = entries[:max_entries]

    return PromptHistory(
        name=name,
        path=str(rel_path),
        current_content_hash=current_hash,
        entries=entries,
        truncated=truncated,
    )
