"""Run type configurations for the orchestrator.

A run type is a dict combining prompt files, tool set, context layers, and
runner parameters. Adding a new run type = adding a dict entry + a prompt file.
"""

import sqlite3
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from orchestrator import tools
from orchestrator.prioritizer import (
    get_branch_run_history,
    has_cascade_suggestions,
    has_judgement,
    has_siblings,
)

PROMPTS_DIR = Path(__file__).parent / "prompts"


def _load_prompt(name: str) -> str:
    return (PROMPTS_DIR / name).read_text(encoding="utf-8")


RUN_TYPES: dict[str, dict[str, Any]] = {
    "explore": {
        "description": "Generative: expand and strengthen a branch by adding missing content",
        "prompts": ["preamble.md", "explore.md"],
        "tool_set": "explore",
        "context_layers": ["root", "ancestors", "branch", "health", "worldview", "history", "pending"],
        "user_task": "Assess this branch and make improvements. Start by inspecting, then act.",
        "runner": {
            "max_rounds": 8,
            "temperature": 0.5,
        },
    },
    "evaluate": {
        "description": "Evaluative: assess importance levels, scores, and structure — no new nodes",
        "prompts": ["preamble.md", "evaluate.md"],
        "tool_set": "evaluate",
        "context_layers": ["root", "branch", "worldview", "health", "siblings", "history", "cascades"],
        "user_task": "Assess this branch and make improvements. Start by inspecting, then act.",
        "runner": {
            "max_rounds": 6,
            "temperature": 0.3,
        },
    },
    "cross_check": {
        "description": "Cross-branch: find tensions, redundancies, shared assumptions",
        "prompts": ["preamble.md", "cross_check.md"],
        "tool_set": "cross_check",
        "context_layers": ["root", "all_branches_shallow", "worldview", "pending"],
        "user_task": (
            "Compare all branches for tensions, redundancies, shared assumptions, "
            "and coverage gaps. Check whether the L0 band tells a coherent story."
        ),
        "runner": {
            "max_rounds": 6,
            "temperature": 0.4,
        },
    },
    "ingest": {
        "description": "Extract evidence from an ingested source into a target branch",
        "prompts": ["preamble.md", "ingest.md"],
        "tool_set": "explore",
        "context_layers": ["root", "ancestors", "branch", "worldview"],
        "runner": {
            "max_rounds": 8,
            "temperature": 0.3,
        },
    },
}


def resolve_run_type(run_type_name: str) -> dict[str, Any]:
    """Resolve a run type name into a fully-assembled config.

    Returns dict with:
        system_prompt: str — concatenated prompt files
        tools: list[dict] — tool definitions for this run type
        context_layers: list[str] — which context layers to include
        max_rounds, temperature, etc. — runner parameters
    """
    config = RUN_TYPES.get(run_type_name)
    if not config:
        available = ", ".join(RUN_TYPES)
        raise ValueError(f"Unknown run type '{run_type_name}'. Available: {available}")

    system_prompt = "\n\n---\n\n".join(_load_prompt(p) for p in config["prompts"])

    tool_set_name = config["tool_set"]
    tool_set = tools.TOOL_SETS.get(tool_set_name)
    if not tool_set:
        raise ValueError(f"Unknown tool set '{tool_set_name}'")

    return {
        "system_prompt": system_prompt,
        "tools": tool_set,
        "context_layers": config["context_layers"],
        "user_task": config.get("user_task", "Assess this branch and make improvements."),
        **config["runner"],
    }


def list_run_types() -> list[dict[str, str]]:
    """List available run types with descriptions."""
    return [
        {"name": name, "description": cfg["description"]}
        for name, cfg in RUN_TYPES.items()
    ]


STRATEGIES = ("auto", "explore-only", "evaluate-only", "alternate")


def decide_run_type(
    conn: sqlite3.Connection,
    ws_id: str,
    branch_id: str,
    strategy: str,
    *,
    step_index: int = 0,
) -> str:
    """Pick a run type for a branch based on strategy and branch state.

    Strategies:
        auto — inspect branch history to decide
        explore-only / evaluate-only — fixed
        alternate — even steps explore, odd steps evaluate
    """
    if strategy == "explore-only":
        return "explore"
    if strategy == "evaluate-only":
        return "evaluate"
    if strategy == "alternate":
        return "explore" if step_index % 2 == 0 else "evaluate"

    history = get_branch_run_history(conn, ws_id, branch_id)

    if history["last_explore"] is None:
        return "explore"

    if history["last_evaluate"] is None:
        return "evaluate"

    from orchestrator.context import get_branch_health

    health = get_branch_health(conn, branch_id)
    if health["no_credence"] > 2 or health["leafs_without_content"] > 2:
        return "explore"

    from orchestrator.prioritizer import _hours_since

    explore_age = _hours_since(history["last_explore"])
    evaluate_age = _hours_since(history["last_evaluate"])
    return "explore" if explore_age >= evaluate_age else "evaluate"


RESEARCH_STRATEGIES = ("full_cycle", "explore_only", "evaluate_only")


def decide_next_phase(
    conn: sqlite3.Connection,
    ws_id: str,
    branch_id: str,
    strategy: str = "full_cycle",
) -> str:
    """Pick the next run type for a single-branch research program.

    Unlike decide_run_type (which handles multi-branch loops), this drives
    a progression through explore → evaluate → cross_check → judgement
    synthesis on ONE branch.
    """
    if strategy == "explore_only":
        return "explore"
    if strategy == "evaluate_only":
        return "evaluate"

    from orchestrator.context import get_branch_health

    health = get_branch_health(conn, branch_id)
    history = get_branch_run_history(conn, ws_id, branch_id)

    if health["total"] < 3:
        return "explore"

    if history["last_explore"] and not history["last_evaluate"]:
        return "evaluate"

    if history["last_explore"] and history["last_evaluate"]:
        if has_siblings(conn, branch_id):
            return "cross_check"

    if not has_judgement(conn, branch_id) and health["total"] > 8:
        return "explore"

    if has_cascade_suggestions(conn, ws_id, branch_id):
        return "evaluate"

    return "explore"
