"""HTTP-level tests for the eval/orchestrate/capabilities endpoints.

Covers:
- GET /api/capabilities
- POST /api/questions/{id}/evaluate
- POST /api/calls/{id}/ground
- POST /api/calls/{id}/feedback
- POST /api/questions/{id}/continue (spot-check only — main coverage in test_api.py)

The stage/commit/ab-evals endpoints are already covered in test_api.py so
we don't duplicate them here.

Background tasks are patched out via mocker.patch("rumil.api.app.asyncio.create_task")
so the endpoint's synchronous response is all we exercise — no real LLM
calls, no grounding pipelines fired.
"""

import pytest
from httpx import ASGITransport, AsyncClient

from rumil.api.app import app
from rumil.database import DB
from rumil.evaluate.registry import (
    EVALUATION_TYPES,
    GROUNDING_PIPELINES,
)
from rumil.models import Call, CallStatus, CallType, Page, PageLayer, PageType, Workspace
from rumil.orchestrators.registry import ORCHESTRATORS


@pytest.fixture
def api_client():
    transport = ASGITransport(app=app)
    return AsyncClient(transport=transport, base_url="http://test")


def _fake_question_page(page_id: str = "q1") -> Page:
    return Page(
        id=page_id,
        page_type=PageType.QUESTION,
        layer=PageLayer.SQUIDGY,
        workspace=Workspace.RESEARCH,
        content="test question",
        headline="test question",
    )


def _fake_claim_page(page_id: str = "c1") -> Page:
    return Page(
        id=page_id,
        page_type=PageType.CLAIM,
        layer=PageLayer.SQUIDGY,
        workspace=Workspace.RESEARCH,
        content="test claim",
        headline="test claim",
    )


def _fake_evaluate_call(
    call_id: str = "call1",
    scope_page_id: str = "q1",
    evaluation_text: str = "Some evaluation text",
) -> Call:
    return Call(
        id=call_id,
        call_type=CallType.EVALUATE,
        workspace=Workspace.RESEARCH,
        scope_page_id=scope_page_id,
        status=CallStatus.COMPLETE,
        review_json={"evaluation": evaluation_text} if evaluation_text else {},
    )


def _fake_assess_call(call_id: str = "call1", scope_page_id: str = "q1") -> Call:
    return Call(
        id=call_id,
        call_type=CallType.ASSESS,
        workspace=Workspace.RESEARCH,
        scope_page_id=scope_page_id,
        status=CallStatus.COMPLETE,
    )


# GET /api/capabilities ----------------------------------------------------


async def test_capabilities_happy_path_shape(api_client):
    resp = await api_client.get("/api/capabilities")
    assert resp.status_code == 200
    data = resp.json()
    assert set(data.keys()) >= {
        "orchestrators",
        "eval_types",
        "grounding_pipelines",
        "call_types",
        "available_calls_presets",
        "available_moves_presets",
    }
    assert isinstance(data["orchestrators"], list)
    assert isinstance(data["eval_types"], list)
    assert isinstance(data["grounding_pipelines"], list)
    assert isinstance(data["call_types"], list)
    assert isinstance(data["available_calls_presets"], list)
    assert isinstance(data["available_moves_presets"], list)


async def test_capabilities_orchestrator_entries_well_formed(api_client):
    resp = await api_client.get("/api/capabilities")
    data = resp.json()
    assert len(data["orchestrators"]) > 0
    required_keys = {"variant", "description", "stability", "cost_band", "exposed_in_chat"}
    for entry in data["orchestrators"]:
        assert required_keys <= set(entry.keys())
        assert isinstance(entry["variant"], str) and entry["variant"]
        assert isinstance(entry["description"], str) and entry["description"]
        assert isinstance(entry["exposed_in_chat"], bool)


async def test_capabilities_matches_registries(api_client):
    """The endpoint must surface exactly what's in the Python registries."""
    resp = await api_client.get("/api/capabilities")
    data = resp.json()

    orch_variants = {o["variant"] for o in data["orchestrators"]}
    assert orch_variants == set(ORCHESTRATORS)

    eval_names = {e["name"] for e in data["eval_types"]}
    assert eval_names == set(EVALUATION_TYPES)

    pipeline_names = {p["name"] for p in data["grounding_pipelines"]}
    assert pipeline_names == set(GROUNDING_PIPELINES)


# POST /api/questions/{id}/evaluate ----------------------------------------


async def test_evaluate_missing_question_returns_404(api_client, mocker):
    mocker.patch.object(DB, "get_page", return_value=None)
    create_task_spy = mocker.patch("rumil.api.app.asyncio.create_task")

    resp = await api_client.post(
        "/api/questions/does-not-exist/evaluate",
        json={"eval_type": "default"},
    )
    assert resp.status_code == 404
    create_task_spy.assert_not_called()


async def test_evaluate_non_question_returns_400(api_client, mocker):
    claim = _fake_claim_page()
    mocker.patch.object(DB, "get_page", return_value=claim)
    create_task_spy = mocker.patch("rumil.api.app.asyncio.create_task")

    resp = await api_client.post(
        f"/api/questions/{claim.id}/evaluate",
        json={"eval_type": "default"},
    )
    assert resp.status_code == 400
    create_task_spy.assert_not_called()


async def test_evaluate_unknown_eval_type_returns_400(api_client, mocker):
    question = _fake_question_page()
    mocker.patch.object(DB, "get_page", return_value=question)
    create_task_spy = mocker.patch("rumil.api.app.asyncio.create_task")

    resp = await api_client.post(
        f"/api/questions/{question.id}/evaluate",
        json={"eval_type": "definitely-not-a-real-eval-type"},
    )
    assert resp.status_code == 400
    assert (
        "eval_type" in resp.json()["detail"].lower() or "unknown" in resp.json()["detail"].lower()
    )
    create_task_spy.assert_not_called()


@pytest.mark.parametrize("eval_type", sorted(EVALUATION_TYPES))
async def test_evaluate_happy_path_returns_202(api_client, mocker, eval_type):
    question = _fake_question_page()
    mocker.patch.object(DB, "get_page", return_value=question)
    create_task_spy = mocker.patch("rumil.api.app.asyncio.create_task")

    resp = await api_client.post(
        f"/api/questions/{question.id}/evaluate",
        json={"eval_type": eval_type},
    )
    assert resp.status_code == 202
    data = resp.json()
    assert data["question_id"] == question.id
    assert data["eval_type"] == eval_type
    assert len(data["run_id"]) > 0
    create_task_spy.assert_called_once()


async def test_evaluate_default_eval_type_when_omitted(api_client, mocker):
    """The body's eval_type defaults to 'default' when not provided."""
    question = _fake_question_page()
    mocker.patch.object(DB, "get_page", return_value=question)
    mocker.patch("rumil.api.app.asyncio.create_task")

    resp = await api_client.post(
        f"/api/questions/{question.id}/evaluate",
        json={},
    )
    assert resp.status_code == 202
    assert resp.json()["eval_type"] == "default"


# POST /api/calls/{id}/ground and /feedback --------------------------------


@pytest.mark.parametrize("pipeline", ("ground", "feedback"))
async def test_grounding_missing_call_returns_404(api_client, mocker, pipeline):
    mocker.patch.object(DB, "get_call", return_value=None)
    create_task_spy = mocker.patch("rumil.api.app.asyncio.create_task")

    resp = await api_client.post(
        f"/api/calls/does-not-exist/{pipeline}",
        json={"from_stage": 1},
    )
    assert resp.status_code == 404
    create_task_spy.assert_not_called()


@pytest.mark.parametrize("pipeline", ("ground", "feedback"))
async def test_grounding_non_evaluate_call_returns_400(api_client, mocker, pipeline):
    assess_call = _fake_assess_call()
    mocker.patch.object(DB, "get_call", return_value=assess_call)
    create_task_spy = mocker.patch("rumil.api.app.asyncio.create_task")

    resp = await api_client.post(
        f"/api/calls/{assess_call.id}/{pipeline}",
        json={"from_stage": 1},
    )
    assert resp.status_code == 400
    assert "EVALUATE" in resp.json()["detail"]
    create_task_spy.assert_not_called()


@pytest.mark.parametrize("pipeline", ("ground", "feedback"))
async def test_grounding_empty_evaluation_text_returns_400(api_client, mocker, pipeline):
    eval_call = _fake_evaluate_call(evaluation_text="")
    mocker.patch.object(DB, "get_call", return_value=eval_call)
    create_task_spy = mocker.patch("rumil.api.app.asyncio.create_task")

    resp = await api_client.post(
        f"/api/calls/{eval_call.id}/{pipeline}",
        json={"from_stage": 1},
    )
    assert resp.status_code == 400
    assert "evaluation" in resp.json()["detail"].lower()
    create_task_spy.assert_not_called()


@pytest.mark.parametrize(
    ("path", "pipeline_name"),
    (("ground", "grounding"), ("feedback", "feedback")),
)
async def test_grounding_happy_path_returns_202(api_client, mocker, path, pipeline_name):
    question = _fake_question_page()
    eval_call = _fake_evaluate_call(scope_page_id=question.id)

    mocker.patch.object(DB, "get_call", return_value=eval_call)
    mocker.patch.object(DB, "get_page", return_value=question)
    create_task_spy = mocker.patch("rumil.api.app.asyncio.create_task")

    resp = await api_client.post(
        f"/api/calls/{eval_call.id}/{path}",
        json={"from_stage": 1},
    )
    assert resp.status_code == 202
    data = resp.json()
    assert data["source_call_id"] == eval_call.id
    assert data["pipeline"] == pipeline_name
    assert data["from_stage"] == 1
    assert len(data["run_id"]) > 0
    create_task_spy.assert_called_once()


async def test_grounding_from_stage_defaults_to_1(api_client, mocker):
    question = _fake_question_page()
    eval_call = _fake_evaluate_call(scope_page_id=question.id)
    mocker.patch.object(DB, "get_call", return_value=eval_call)
    mocker.patch.object(DB, "get_page", return_value=question)
    mocker.patch("rumil.api.app.asyncio.create_task")

    resp = await api_client.post(f"/api/calls/{eval_call.id}/ground", json={})
    assert resp.status_code == 202
    assert resp.json()["from_stage"] == 1


async def test_grounding_custom_from_stage_round_trips(api_client, mocker):
    question = _fake_question_page()
    eval_call = _fake_evaluate_call(scope_page_id=question.id)
    eval_call.call_params = {"checkpoints": {"stage_1": {"foo": "bar"}}}
    mocker.patch.object(DB, "get_call", return_value=eval_call)
    mocker.patch.object(DB, "get_page", return_value=question)
    mocker.patch("rumil.api.app.asyncio.create_task")

    resp = await api_client.post(
        f"/api/calls/{eval_call.id}/feedback",
        json={"from_stage": 3},
    )
    assert resp.status_code == 202
    assert resp.json()["from_stage"] == 3


# POST /api/questions/{id}/continue (spot-check) ---------------------------
#
# Extensive coverage already lives in tests/test_api.py — this is a thin
# smoke that catches response-shape drift under the typed ContinueQuestionOut.


async def test_continue_question_response_has_typed_fields(api_client, mocker):
    question = _fake_question_page()
    mocker.patch.object(DB, "get_page", return_value=question)
    mocker.patch("rumil.api.app.asyncio.create_task")

    resp = await api_client.post(
        f"/api/questions/{question.id}/continue",
        json={"budget": 3},
    )
    assert resp.status_code == 202
    data = resp.json()
    assert set(data.keys()) >= {"run_id", "question_id", "budget"}
    assert data["question_id"] == question.id
    assert data["budget"] == 3
