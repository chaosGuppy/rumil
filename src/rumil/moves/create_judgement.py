"""CREATE_JUDGEMENT move: create a considered position on a question."""

import logging
from typing import Any


from pydantic import Field

from rumil.database import DB
from rumil.models import (
    Call,
    LinkType,
    MoveType,
    PageLayer,
    PageLink,
    PageType,
)
from rumil.moves.base import (
    CreatePagePayload,
    MoveDef,
    MoveResult,
    create_page,
    supersede_old_judgements,
)

log = logging.getLogger(__name__)


class CreateJudgementPayload(CreatePagePayload):
    key_dependencies: str | None = Field(
        None, description="What this judgement most depends on"
    )
    sensitivity_analysis: str | None = Field(
        None, description="What would shift this judgement, and in which direction"
    )

    def page_extra_fields(self) -> dict[str, Any]:
        extra = super().page_extra_fields()
        if self.key_dependencies is not None:
            extra["key_dependencies"] = self.key_dependencies
        if self.sensitivity_analysis is not None:
            extra["sensitivity_analysis"] = self.sensitivity_analysis
        return extra

    @classmethod
    def model_json_schema(
        cls,
        by_alias: bool = True,
        ref_template: str = "#/$defs/{model}",
        schema_generator: Any = None,
        mode: str = "validation",
        **kwargs: Any,
    ) -> dict[str, Any]:
        kw: dict[str, Any] = {
            "by_alias": by_alias,
            "ref_template": ref_template,
            "mode": mode,
            **kwargs,
        }
        if schema_generator is not None:
            kw["schema_generator"] = schema_generator
        schema = super().model_json_schema(**kw)
        schema.get("properties", {}).pop("supersedes", None)
        return schema


class CreateJudgementForQuestionPayload(CreateJudgementPayload):
    question_id: str = Field(
        description="The question this judgement answers. The judgement will be "
        "linked to this question and will supersede any prior judgement on it."
    )


async def _link_and_supersede(new_judgement_id: str, question_id: str, db: DB) -> None:
    """Link a new judgement to its question and supersede prior judgements."""
    await db.save_link(
        PageLink(
            from_page_id=new_judgement_id,
            to_page_id=question_id,
            link_type=LinkType.RELATED,
        )
    )
    await supersede_old_judgements(new_judgement_id, question_id, db)
    log.info(
        "Judgement %s linked to question %s",
        new_judgement_id[:8],
        question_id[:8],
    )


async def execute(payload: CreateJudgementPayload, call: Call, db: DB) -> MoveResult:
    result = await create_page(payload, call, db, PageType.JUDGEMENT, PageLayer.SQUIDGY)
    if not result.created_page_id or not call.scope_page_id:
        return result

    await _link_and_supersede(result.created_page_id, call.scope_page_id, db)
    return result


async def execute_for_question(
    payload: CreateJudgementForQuestionPayload, call: Call, db: DB
) -> MoveResult:
    resolved = await db.resolve_page_id(payload.question_id)
    if not resolved:
        return MoveResult(message=f"Question {payload.question_id} not found")

    result = await create_page(payload, call, db, PageType.JUDGEMENT, PageLayer.SQUIDGY)
    if not result.created_page_id:
        return result

    await _link_and_supersede(result.created_page_id, resolved, db)
    return result


MOVE = MoveDef(
    move_type=MoveType.CREATE_JUDGEMENT,
    name="create_judgement",
    description=(
        "Create a judgement — a considered position synthesising the "
        "considerations bearing on a question. Must engage with "
        "considerations on multiple sides. Include key_dependencies and "
        "sensitivity_analysis fields. The judgement is automatically linked "
        "to the scope question and supersedes any prior judgement on it."
    ),
    schema=CreateJudgementPayload,
    execute=execute,
)

QUESTION_MOVE = MoveDef(
    move_type=MoveType.CREATE_JUDGEMENT,
    name="create_judgement_for_question",
    description=(
        "Create a judgement linked to an explicit question (by ID). "
        "The judgement supersedes any prior judgement on that question. "
        "Use this when the target question is not the scope question of "
        "the current call. Include key_dependencies and "
        "sensitivity_analysis fields."
    ),
    schema=CreateJudgementForQuestionPayload,
    execute=execute_for_question,
)
