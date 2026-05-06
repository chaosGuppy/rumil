"""Cross-workflow recurse graph.

Static node-edge graph derived from each ``WorkflowProfile``'s
``recurses_into`` list (and per-stage ``recurses_into``). Used for the
workflow index page's "how do these compose" affordance and as a
reference layout for other graph-shaped surfaces (run flow trees,
call hierarchies) that share the same primitives.
"""

from __future__ import annotations

from rumil.atlas.schemas import (
    WorkflowGraph,
    WorkflowGraphEdge,
    WorkflowGraphNode,
)
from rumil.atlas.workflows import all_profiles


def build_workflow_graph() -> WorkflowGraph:
    profiles = all_profiles()
    nodes: list[WorkflowGraphNode] = [
        WorkflowGraphNode(id=p.name, label=p.name, kind=p.kind) for p in profiles
    ]
    seen: set[tuple[str, str, str | None]] = set()
    edges: list[WorkflowGraphEdge] = []
    for p in profiles:
        for target in p.recurses_into:
            key = (p.name, target, None)
            if key in seen:
                continue
            seen.add(key)
            edges.append(WorkflowGraphEdge(from_id=p.name, to_id=target, via_stage=None))
        for stage in p.stages:
            for target in stage.recurses_into:
                key = (p.name, target, stage.id)
                if key in seen:
                    continue
                seen.add(key)
                edges.append(WorkflowGraphEdge(from_id=p.name, to_id=target, via_stage=stage.id))
    return WorkflowGraph(nodes=nodes, edges=edges)
