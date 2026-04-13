"""Run type configurations for the orchestrator.

A run type is a dict combining prompt files, tool set, context layers, and
runner parameters. Adding a new run type = adding a dict entry + a prompt file.
"""

from collections.abc import Sequence
from pathlib import Path
from typing import Any

from orchestrator import tools

PROMPTS_DIR = Path(__file__).parent / "prompts"


def _load_prompt(name: str) -> str:
    return (PROMPTS_DIR / name).read_text(encoding="utf-8")


RUN_TYPES: dict[str, dict[str, Any]] = {
    "explore": {
        "description": "Generative: expand and strengthen a branch by adding missing content",
        "prompts": ["preamble.md", "explore.md"],
        "tool_set": "explore",
        "context_layers": ["root", "ancestors", "branch", "health", "worldview", "history", "pending"],
        "runner": {
            "max_rounds": 8,
            "temperature": 0.5,
        },
    },
    "evaluate": {
        "description": "Evaluative: assess importance levels, scores, and structure — no new nodes",
        "prompts": ["preamble.md", "evaluate.md"],
        "tool_set": "evaluate",
        "context_layers": ["root", "branch", "worldview", "health", "siblings", "history"],
        "runner": {
            "max_rounds": 6,
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
        **config["runner"],
    }


def list_run_types() -> list[dict[str, str]]:
    """List available run types with descriptions."""
    return [
        {"name": name, "description": cfg["description"]}
        for name, cfg in RUN_TYPES.items()
    ]
