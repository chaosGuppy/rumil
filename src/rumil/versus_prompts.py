"""Pure prompt-rendering helpers shared between rumil and versus.

Carved out of :mod:`rumil.versus_bridge` so versus's OpenRouter judge can
read the same shell + dimension prompts without dragging in the rumil
DB / orchestrator / sdk-agent modules at import time. The bridge
re-exports everything here for back-compat.
"""

import hashlib
from collections.abc import Sequence

from rumil.prompts import PROMPTS_DIR as _PROMPTS_DIR

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
    """Return the 7-point label found in ``text``, or None if absent.

    When a model reasons through the labels before emitting its verdict
    ("B is arguably strongly preferred on grounding, but overall A
    somewhat preferred"), the verdict is the LAST label in the text,
    not the first one that happens to match. We scan for every label
    and pick the latest occurrence; ties (same start position) are
    broken by preferring the longer label so "B somewhat preferred"
    wins over a prefix-matching "B slightly preferred" — the labels
    don't actually share prefixes today, but pinning the tiebreak
    avoids a silent regression if they ever do.
    """
    lower = text.lower()
    best_pos = -1
    best_label: str | None = None
    for label in PREFERENCE_LABELS:
        pos = lower.rfind(label.lower())
        if pos == -1:
            continue
        if pos > best_pos or (pos == best_pos and len(label) > len(best_label or "")):
            best_pos = pos
            best_label = label
    return best_label


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


# Substitution dicts for the versus-judge-shell template. Edits to the shell
# file (preference scale, dimension framing, output format) propagate to both
# modes; edits to a dict change only that mode. Keep _TOOLS_VARS byte-equivalent
# to the pre-split shell content so ws/orch prompt hashes stay stable.
_BLIND_VARS: dict[str, str] = {
    "location_desc": (
        "The essay opening and the two continuations are inlined in the user message below."
    ),
    "tool_section": "",
    "output_extras": "",
    "convergence_section": "",
}

_TOOLS_VARS: dict[str, str] = {
    "location_desc": (
        "The essay opening and the two continuations are included in "
        "the question page at the scope of this call. Use `load_page` "
        "on the scope question to read them if they aren't already in "
        "your context."
    ),
    "tool_section": (
        "\n## Optional: use workspace material\n\n"
        "If the essay's subject matter overlaps with material in the "
        "active workspace, you have three tools available. Using them "
        "is optional — a judgment grounded purely in the two texts is "
        "fine. But where relevant workspace pages exist (prior claims "
        "on the topic, established positions, source material), "
        "consulting them can make the judgment better-grounded.\n\n"
        '- `explore_subgraph({"page_id": "..."})` — render a subtree '
        "of the workspace graph rooted at the given page.\n"
        '- `load_page({"page_id": "...", "detail": "content" | '
        '"abstract"})` — load one page\'s full content or abstract.\n'
        '- `search_workspace({"query": "..."})` — semantic search '
        "across the workspace. Useful for finding whether a topic the "
        "continuation engages with has existing coverage.\n\n"
        "Cite workspace pages by their short ID when they bear on the "
        "judgment.\n"
    ),
    "output_extras": "; reference workspace page IDs when you use them",
    "convergence_section": (
        "\n## Convergence\n\n"
        "Don't stop until your sense of the difference between A and "
        "B has converged. But also don't over-explore — for short "
        "essay continuations, one careful pass through each text, "
        "optionally plus a small number of workspace lookups, is "
        "usually enough."
    ),
}


def build_system_prompt(task_body: str, *, with_tools: bool = False) -> str:
    """Compose the versus-judge shell with the task body slotted in.

    ``with_tools=True`` produces the ws/orch shell (workspace-exploration
    tools advertised, scope-question read instructions). ``with_tools=False``
    (default) is the blind shell used by tool-less paths: the pair lives
    inline in the user message, no tool guidance.
    """
    shell = (_PROMPTS_DIR / "versus-judge-shell.md").read_text()
    vars_ = _TOOLS_VARS if with_tools else _BLIND_VARS
    return shell.format(task_body=task_body, **vars_)


def compute_prompt_hash(task_body: str, *, with_tools: bool = False) -> str:
    """Return a short hash of the *composed* system prompt.

    Hashes the rendered output, not the raw shell, so blind and tools modes
    fork into independent hash spaces — editing tool-only content doesn't
    shift blind hashes, and editing common content shifts both correctly.
    8 hex chars is enough to distinguish prompt versions without cluttering
    the key.
    """
    composed = build_system_prompt(task_body, with_tools=with_tools)
    return hashlib.sha256(composed.encode()).hexdigest()[:8]
