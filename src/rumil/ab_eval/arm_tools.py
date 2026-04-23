"""Arm-aware wrappers around the workspace exploration tools.

The AB eval uses a single agent per dimension that produces a direct
comparison between Run A and Run B. That agent needs to query either
workspace on demand — these wrappers add a required ``arm`` field (``"A"``
or ``"B"``) to each underlying tool's input schema and dispatch to the
corresponding inner tool.

The inner tools are the unchanged single-arm ones from
``rumil.workspace_exploration``; each is built against its arm-specific
staged DB and keeps its own ``highlight_run_id`` for "[ADDED BY THIS RUN]"
annotations. All three wrappers share the single ``CallTrace`` for the
comparison call — the inner tools record trace events against it as usual.
"""

from copy import deepcopy

from rumil.database import DB
from rumil.llm import Tool
from rumil.tracing.tracer import CallTrace
from rumil.workspace_exploration import (
    make_explore_subgraph_tool,
    make_load_page_tool,
    make_search_tool,
)


def _add_arm_field(inner_schema: dict) -> dict:
    """Return a copy of *inner_schema* with a required ``arm`` field prepended."""
    schema = deepcopy(inner_schema)
    props = schema.setdefault("properties", {})
    new_props = {
        "arm": {
            "type": "string",
            "enum": ["A", "B"],
            "description": ("Which arm's workspace to query: 'A' for Run A, 'B' for Run B."),
        }
    }
    new_props.update(props)
    schema["properties"] = new_props
    required = list(schema.get("required", []))
    if "arm" not in required:
        required = ["arm", *required]
    schema["required"] = required
    return schema


def _make_arm_dispatch_tool(
    tool_a: Tool,
    tool_b: Tool,
    *,
    name: str,
    description: str,
) -> Tool:
    """Build a Tool that dispatches to *tool_a* or *tool_b* based on ``arm``."""
    assert tool_a.name == tool_b.name, "inner tools must share a name"

    merged_schema = _add_arm_field(tool_a.input_schema)

    async def fn(args: dict) -> str:
        arm = args.get("arm")
        if arm not in ("A", "B"):
            return (
                f"Error: the 'arm' field is required and must be exactly 'A' or 'B'. Got: {arm!r}."
            )
        inner_args = {k: v for k, v in args.items() if k != "arm"}
        target = tool_a if arm == "A" else tool_b
        return await target.fn(inner_args)

    return Tool(
        name=name,
        description=description,
        input_schema=merged_schema,
        fn=fn,
    )


def make_arm_explore_subgraph_tool(
    db_a: DB,
    db_b: DB,
    trace: CallTrace,
    *,
    run_id_a: str,
    run_id_b: str,
) -> Tool:
    """Arm-aware `explore_subgraph`. Required `arm` selects which workspace."""
    inner_a = make_explore_subgraph_tool(
        db_a,
        trace,
        questions_only=False,
        highlight_run_id=run_id_a,
    )
    inner_b = make_explore_subgraph_tool(
        db_b,
        trace,
        questions_only=False,
        highlight_run_id=run_id_b,
    )
    description = (
        "Render a subtree of the research graph for one arm's workspace. "
        "The 'arm' field selects which run's workspace to inspect ('A' or 'B'). "
        "Same short IDs in the two arms refer to different pages — always "
        "re-resolve IDs via the arm you intend to inspect.\n\n" + inner_a.description
    )
    return _make_arm_dispatch_tool(
        inner_a,
        inner_b,
        name="explore_subgraph",
        description=description,
    )


def make_arm_load_page_tool(
    db_a: DB,
    db_b: DB,
    trace: CallTrace,
    *,
    run_id_a: str,
    run_id_b: str,
) -> Tool:
    """Arm-aware `load_page`. Required `arm` selects which workspace."""
    inner_a = make_load_page_tool(db_a, trace, highlight_run_id=run_id_a)
    inner_b = make_load_page_tool(db_b, trace, highlight_run_id=run_id_b)
    description = (
        "Load a page from one arm's workspace. The 'arm' field selects "
        "which run's workspace to read ('A' or 'B'). A short ID (first 8 "
        "chars) only resolves within the selected arm — the same short ID "
        "on the other arm may refer to an entirely different page.\n\n" + inner_a.description
    )
    return _make_arm_dispatch_tool(
        inner_a,
        inner_b,
        name="load_page",
        description=description,
    )


def make_arm_search_tool(
    db_a: DB,
    db_b: DB,
    trace: CallTrace,
) -> Tool:
    """Arm-aware `search_workspace`. Required `arm` selects which workspace."""
    inner_a = make_search_tool(db_a, trace)
    inner_b = make_search_tool(db_b, trace)
    description = (
        "Semantic search within one arm's workspace. The 'arm' field "
        "selects which run's workspace to search ('A' or 'B'). Run the "
        "same query against both arms to compare coverage on a topic.\n\n" + inner_a.description
    )
    return _make_arm_dispatch_tool(
        inner_a,
        inner_b,
        name="search_workspace",
        description=description,
    )
