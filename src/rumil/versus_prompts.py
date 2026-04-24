"""Pure prompt-rendering helpers shared between rumil and versus.

Carved out of :mod:`rumil.versus_bridge` so versus's OpenRouter judge can
read the same shell + dimension prompts without dragging in the rumil
DB / orchestrator / sdk-agent modules at import time. The bridge
re-exports everything here for back-compat.
"""

from __future__ import annotations

import hashlib
from collections.abc import Sequence
from pathlib import Path

_PROMPTS_DIR = Path(__file__).resolve().parent.parent.parent / "prompts"


PREFERENCE_LABELS: Sequence[str] = (
    "A strongly preferred",
    "A somewhat preferred",
    "A slightly preferred",
    "Approximately indifferent between A and B",
    "B slightly preferred",
    "B somewhat preferred",
    "B strongly preferred",
)

_LABEL_TO_VERDICT = {
    "A strongly preferred": "A",
    "A somewhat preferred": "A",
    "A slightly preferred": "A",
    "Approximately indifferent between A and B": "tie",
    "B slightly preferred": "B",
    "B somewhat preferred": "B",
    "B strongly preferred": "B",
}


def extract_preference(text: str) -> str | None:
    """Return the 7-point label found in ``text``, or None if absent."""
    lower = text.lower()
    for label in PREFERENCE_LABELS:
        if label.lower() in lower:
            return label
    return None


def label_to_verdict(label: str | None) -> str | None:
    if label is None:
        return None
    return _LABEL_TO_VERDICT.get(label)


def get_rumil_dimension_body(name: str) -> str:
    """Load the essay-adapted rumil dimension prompt at ``prompts/versus-<name>.md``.

    ``name`` uses the same keys as :class:`rumil.run_eval.agents.EvalAgentSpec`
    (e.g. ``general_quality``, ``grounding``); underscores are converted to
    hyphens when resolving the file name.
    """
    path = _PROMPTS_DIR / f"versus-{name.replace('_', '-')}.md"
    if not path.is_file():
        raise ValueError(f"no essay-adapted dimension prompt for '{name}' (expected {path})")
    return path.read_text()


def build_system_prompt(task_body: str) -> str:
    """Compose the versus-judge shell with the task body slotted in."""
    shell = (_PROMPTS_DIR / "versus-judge-shell.md").read_text()
    return shell.replace("{task_body}", task_body)


def compute_prompt_hash(task_body: str) -> str:
    """Return a short hash of the composed system prompt.

    Covers both the shell and the task body, so any edit to either file
    invalidates judge_model dedup keys naturally -- mirroring versus's
    ``prefix_config_hash`` / ``sampling_hash`` discipline. 8 hex chars is
    enough to distinguish prompt versions without cluttering the key.
    """
    shell = (_PROMPTS_DIR / "versus-judge-shell.md").read_text()
    return hashlib.sha256((shell + task_body).encode()).hexdigest()[:8]
