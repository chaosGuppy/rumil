"""Scope: the region of the workspace a process is operating on.

Scope is explicit and structured so that overlap between two processes'
scopes can be detected (a precondition for later features like budget
pooling or concurrent-process arbitration).
"""

from typing import Literal

from pydantic import BaseModel, Field


class QuestionScope(BaseModel):
    """A single question plus its descendant subgraph."""

    kind: Literal["question"] = "question"
    question_id: str
    depth: int | None = Field(
        default=None,
        description="Max traversal depth; None = unbounded (whole subtree)",
    )


class ClaimScope(BaseModel):
    """A single claim, optionally with its dependents."""

    kind: Literal["claim"] = "claim"
    claim_id: str
    include_dependents: bool = True


class SubgraphScope(BaseModel):
    """A named set of roots plus their descendants."""

    kind: Literal["subgraph"] = "subgraph"
    root_ids: list[str]
    depth: int = 2


class ProjectScope(BaseModel):
    """Everything within a project."""

    kind: Literal["project"] = "project"
    project_id: str


Scope = QuestionScope | ClaimScope | SubgraphScope | ProjectScope
