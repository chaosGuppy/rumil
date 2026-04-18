"""Task-shape auto-tagging for research questions (v1 taxonomy).

See ``marketplace-thread/27-task-shape-taxonomy.md`` for the full design. The
tagger reads a question's headline + abstract and returns a structured
``TaskShape`` covering two dimensions: ``deliverable_shape`` (6 values) and
``source_posture`` (3 values). ``workspace_coverage`` is deferred to a later
phase and is not produced here.

The shape is metadata only — no routing or behavior change is keyed off it in
v1. It is stored as JSONB on ``pages.task_shape`` for tagged questions.
"""

import logging
from datetime import UTC, datetime
from enum import Enum
from pathlib import Path
from typing import TYPE_CHECKING

from pydantic import BaseModel, Field

from rumil.llm import structured_call

if TYPE_CHECKING:
    from rumil.database import DB

log = logging.getLogger(__name__)

_PROMPTS_DIR = Path(__file__).resolve().parents[2] / "prompts"

TAG_VERSION = 1


class DeliverableShape(str, Enum):
    PREDICTION = "prediction"
    EXTRACTION = "extraction"
    AUDIT = "audit"
    EXPLORATION = "exploration"
    DEFINITION = "definition"
    DECISION_SUPPORT = "decision_support"


class SourcePosture(str, Enum):
    SYNTHETIC = "synthetic"
    SOURCE_BOUND = "source_bound"
    MIXED = "mixed"


class TaskShape(BaseModel):
    """Structured task-shape tag for a research question (v1 taxonomy)."""

    deliverable_shape: DeliverableShape = Field(
        description=(
            "What does a finished answer look like? One of prediction, "
            "extraction, audit, exploration, definition, decision_support."
        )
    )
    source_posture: SourcePosture = Field(
        description=(
            "What grounding does answering require? One of synthetic, source_bound, mixed."
        )
    )
    required_source_id: str | None = Field(
        default=None,
        description=(
            "If the question names a specific existing page (8-char short id "
            "or full uuid) that must be read, return its id. Else null."
        ),
    )

    def to_payload(self, tagged_by: str = "llm_v1") -> dict:
        """Return the JSONB payload stored in ``pages.task_shape``."""
        return {
            "deliverable_shape": self.deliverable_shape.value,
            "source_posture": self.source_posture.value,
            "required_source_id": self.required_source_id,
            "tagged_at": datetime.now(UTC).isoformat(),
            "tagged_by": tagged_by,
            "tag_version": TAG_VERSION,
        }


def _tagger_system_prompt() -> str:
    return (_PROMPTS_DIR / "task_shape_tagger.md").read_text()


def _tagger_user_message(headline: str, abstract: str) -> str:
    parts = [f"## Question headline\n{headline}"]
    if abstract.strip():
        parts.append(f"## Abstract / context\n{abstract}")
    parts.append("Tag this question.")
    return "\n\n".join(parts)


async def tag_question(
    headline: str,
    abstract: str = "",
    *,
    model: str | None = None,
) -> TaskShape:
    """Call Haiku to produce a ``TaskShape`` for a question.

    Pass the question's ``headline`` and ``abstract`` (or content). The call is
    a single structured ``structured_call`` invocation — no agent loop, no
    tool use. Defaults to the Haiku model since tagging is cheap and stable.
    """
    result = await structured_call(
        system_prompt=_tagger_system_prompt(),
        user_message=_tagger_user_message(headline, abstract),
        response_model=TaskShape,
        model=model or "claude-haiku-4-5-20251001",
    )
    if result.parsed is None:
        raise RuntimeError("task_shape tagger returned no parsed output")
    return result.parsed


def parse_task_shape_override(raw: str) -> dict:
    """Parse a CLI ``--task-shape`` flag value into a JSONB payload dict.

    Accepts ``dimension1=value1,dimension2=value2,...``. Unknown dimensions are
    rejected. Values must match the closed enums above. Returns the payload
    with ``tagged_by="user"``.
    """
    if not raw.strip():
        raise ValueError("--task-shape value is empty")

    valid_dims: dict[str, type[Enum]] = {
        "deliverable_shape": DeliverableShape,
        "source_posture": SourcePosture,
    }

    parsed: dict[str, str] = {}
    for chunk in raw.split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        if "=" not in chunk:
            raise ValueError(f"--task-shape entry {chunk!r} is not key=value")
        key, _, value = chunk.partition("=")
        key = key.strip()
        value = value.strip()
        if key not in valid_dims:
            raise ValueError(
                f"unknown task-shape dimension {key!r}; valid: {', '.join(sorted(valid_dims))}"
            )
        enum_cls = valid_dims[key]
        try:
            parsed[key] = enum_cls(value).value
        except ValueError as exc:
            choices = ", ".join(e.value for e in enum_cls)
            raise ValueError(f"invalid value {value!r} for {key}; valid: {choices}") from exc

    if "deliverable_shape" not in parsed or "source_posture" not in parsed:
        raise ValueError("--task-shape must set both deliverable_shape and source_posture")

    return {
        **parsed,
        "required_source_id": None,
        "tagged_at": datetime.now(UTC).isoformat(),
        "tagged_by": "user",
        "tag_version": TAG_VERSION,
    }


async def auto_tag_and_save(
    page_id: str,
    headline: str,
    abstract: str,
    db: "DB",
) -> dict | None:
    """Run the auto-tagger on a question and persist the payload.

    Swallows tagger exceptions so question creation never fails because of
    tagging. Returns the payload written (or ``None`` on failure).
    """
    try:
        shape = await tag_question(headline, abstract)
    except Exception:
        log.warning("task_shape auto-tagger failed for page %s", page_id[:8], exc_info=True)
        return None
    payload = shape.to_payload()
    try:
        await db.update_page_task_shape(page_id, payload)
    except Exception:
        log.warning("Persisting task_shape failed for page %s", page_id[:8], exc_info=True)
        return None
    log.info(
        "task_shape tagged: page=%s shape=%s posture=%s",
        page_id[:8],
        shape.deliverable_shape.value,
        shape.source_posture.value,
    )
    return payload
