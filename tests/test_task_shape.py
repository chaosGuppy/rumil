"""Tests for task-shape tagging (v1 taxonomy)."""

import pytest

from rumil.models import Page, PageLayer, PageType, Workspace
from rumil.task_shape import (
    DeliverableShape,
    SourcePosture,
    TaskShape,
    auto_tag_and_save,
    parse_task_shape_override,
    tag_question,
)


async def test_auto_tag_and_save_persists_payload(tmp_db, mocker):
    page = Page(
        page_type=PageType.QUESTION,
        layer=PageLayer.SQUIDGY,
        workspace=Workspace.RESEARCH,
        content="How informative is METR's time horizon work? What are its limitations?",
        headline="How informative is METR's time horizon work?",
    )
    await tmp_db.save_page(page)

    fixed_shape = TaskShape(
        deliverable_shape=DeliverableShape.AUDIT,
        source_posture=SourcePosture.SOURCE_BOUND,
        required_source_id=None,
    )
    mocker.patch("rumil.task_shape.tag_question", return_value=fixed_shape)

    payload = await auto_tag_and_save(page.id, page.headline, page.content, tmp_db)

    assert payload is not None
    assert payload["deliverable_shape"] == "audit"
    assert payload["source_posture"] == "source_bound"
    assert payload["tagged_by"] == "llm_v1"
    assert payload["tag_version"] == 1

    stored = await tmp_db.get_page(page.id)
    assert stored is not None
    assert stored.task_shape is not None
    assert stored.task_shape["deliverable_shape"] == "audit"
    assert stored.task_shape["source_posture"] == "source_bound"


async def test_auto_tag_and_save_swallows_tagger_failure(tmp_db, mocker):
    page = Page(
        page_type=PageType.QUESTION,
        layer=PageLayer.SQUIDGY,
        workspace=Workspace.RESEARCH,
        content="Any question",
        headline="Any question",
    )
    await tmp_db.save_page(page)

    mocker.patch("rumil.task_shape.tag_question", side_effect=RuntimeError("api down"))
    payload = await auto_tag_and_save(page.id, page.headline, page.content, tmp_db)

    assert payload is None
    stored = await tmp_db.get_page(page.id)
    assert stored is not None
    assert stored.task_shape is None


def test_parse_task_shape_override_basic():
    payload = parse_task_shape_override("deliverable_shape=audit,source_posture=source_bound")
    assert payload["deliverable_shape"] == "audit"
    assert payload["source_posture"] == "source_bound"
    assert payload["tagged_by"] == "user"
    assert payload["tag_version"] == 1
    assert payload["required_source_id"] is None


def test_parse_task_shape_override_whitespace():
    payload = parse_task_shape_override(" deliverable_shape = prediction , source_posture = mixed ")
    assert payload["deliverable_shape"] == "prediction"
    assert payload["source_posture"] == "mixed"


def test_parse_task_shape_override_rejects_missing_dim():
    with pytest.raises(ValueError, match="must set both"):
        parse_task_shape_override("deliverable_shape=audit")


def test_parse_task_shape_override_rejects_unknown_dim():
    with pytest.raises(ValueError, match="unknown task-shape dimension"):
        parse_task_shape_override("deliverable_shape=audit,source_posture=mixed,fake=x")


def test_parse_task_shape_override_rejects_invalid_value():
    with pytest.raises(ValueError, match="invalid value"):
        parse_task_shape_override("deliverable_shape=not_a_shape,source_posture=mixed")


def test_parse_task_shape_override_rejects_malformed_entry():
    with pytest.raises(ValueError, match="not key=value"):
        parse_task_shape_override("deliverable_shape")


async def test_workspace_coverage_counts_by_dimension(tmp_db):
    shapes = [
        {"deliverable_shape": "audit", "source_posture": "source_bound"},
        {"deliverable_shape": "audit", "source_posture": "mixed"},
        {"deliverable_shape": "prediction", "source_posture": "source_bound"},
        {"deliverable_shape": "exploration", "source_posture": "mixed"},
        {"deliverable_shape": "extraction", "source_posture": "source_bound"},
    ]
    for i, shape in enumerate(shapes):
        page = Page(
            page_type=PageType.QUESTION,
            layer=PageLayer.SQUIDGY,
            workspace=Workspace.RESEARCH,
            content=f"Question {i}",
            headline=f"Question {i}",
            task_shape=shape,
        )
        await tmp_db.save_page(page)

    untagged = Page(
        page_type=PageType.QUESTION,
        layer=PageLayer.SQUIDGY,
        workspace=Workspace.RESEARCH,
        content="Untagged question",
        headline="Untagged question",
    )
    await tmp_db.save_page(untagged)

    non_question = Page(
        page_type=PageType.CLAIM,
        layer=PageLayer.SQUIDGY,
        workspace=Workspace.RESEARCH,
        content="A claim",
        headline="A claim",
    )
    await tmp_db.save_page(non_question)

    coverage = await tmp_db.workspace_coverage()

    assert coverage["deliverable_shape"] == {
        "audit": 2,
        "prediction": 1,
        "exploration": 1,
        "extraction": 1,
    }
    assert coverage["source_posture"] == {
        "source_bound": 3,
        "mixed": 2,
    }


@pytest.mark.llm
@pytest.mark.parametrize(
    ("headline", "expected_shape"),
    [
        (
            "How informative is METR's time horizon work? What are its limitations?",
            "audit",
        ),
        (
            "What are the main claims in this essay about scaling laws?",
            "extraction",
        ),
        (
            "Will frontier AI automate 50% of software engineering by 2030?",
            "prediction",
        ),
        (
            "What drives deforestation in the Amazon?",
            "exploration",
        ),
        (
            "What does 'alignment' mean in the context of frontier model safety?",
            "definition",
        ),
    ],
)
async def test_tag_question_live(headline, expected_shape):
    shape = await tag_question(headline)
    assert isinstance(shape, TaskShape)
    assert shape.deliverable_shape.value in {
        "prediction",
        "extraction",
        "audit",
        "exploration",
        "definition",
        "decision_support",
    }
    assert shape.source_posture.value in {"synthetic", "source_bound", "mixed"}
    payload = shape.to_payload()
    assert payload["deliverable_shape"] == shape.deliverable_shape.value
    assert payload["source_posture"] == shape.source_posture.value
