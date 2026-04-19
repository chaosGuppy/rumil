"""Tests for GET /api/pages/{page_id}/iterations.

The endpoint walks the draft chain of a refine-artifact run — given an
accepted artifact page, it returns one entry per draft (v1 -> vN)
including the adversarial_review verdict for each. These tests seed
pages directly (no LLM) to exercise: the happy path, the two 400 cases
(not an artifact, no refinement metadata), and verdict resolution.
"""

from datetime import UTC, datetime, timedelta

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from rumil.api.app import app
from rumil.models import (
    LinkType,
    Page,
    PageLayer,
    PageLink,
    PageType,
    Workspace,
)


@pytest.fixture
def api_client():
    transport = ASGITransport(app=app)
    return AsyncClient(transport=transport, base_url="http://test")


async def _save_draft(
    tmp_db,
    *,
    headline: str,
    content: str,
    created_at: datetime,
    superseded_by: str | None = None,
) -> Page:
    page = Page(
        page_type=PageType.ARTIFACT,
        layer=PageLayer.SQUIDGY,
        workspace=Workspace.RESEARCH,
        headline=headline,
        content=content,
        created_at=created_at,
        is_superseded=superseded_by is not None,
        superseded_by=superseded_by,
    )
    await tmp_db.save_page(page)
    return page


async def _save_verdict(
    tmp_db,
    *,
    target: Page,
    claim_holds: bool,
    claim_confidence: int,
    dissents: list[str],
    concurrences: list[str],
    stronger_side: str = "how_true",
) -> Page:
    verdict_payload = {
        "stronger_side": stronger_side,
        "claim_holds": claim_holds,
        "claim_confidence": claim_confidence,
        "rationale": "synthesizer rationale",
        "concurrences": concurrences,
        "dissents": dissents,
        "sunset_after_days": None,
        "created_at": datetime.now(UTC).isoformat(),
    }
    verdict = Page(
        page_type=PageType.JUDGEMENT,
        layer=PageLayer.SQUIDGY,
        workspace=Workspace.RESEARCH,
        headline=f"Verdict for {target.headline[:40]}",
        content="verdict body",
        credence=claim_confidence,
        robustness=3,
        extra={"adversarial_verdict": verdict_payload, "target_page_id": target.id},
    )
    await tmp_db.save_page(verdict)
    await tmp_db.save_link(
        PageLink(
            from_page_id=verdict.id,
            to_page_id=target.id,
            link_type=LinkType.DEPENDS_ON,
            reasoning="test verdict",
        )
    )
    return verdict


@pytest_asyncio.fixture
async def three_iteration_artifact(tmp_db):
    """Three-pass refine-artifact chain: v1 + v2 superseded by v3 (final)."""
    now = datetime.now(UTC)
    # Build the final first so we have its id when saving the earlier drafts
    # with superseded_by set.
    final_draft = await _save_draft(
        tmp_db,
        headline="Final draft",
        content="# v3\n\nFinal content with key insight.",
        created_at=now,
    )
    draft_v1 = await _save_draft(
        tmp_db,
        headline="Draft v1",
        content="# v1\n\nFirst attempt.",
        created_at=now - timedelta(minutes=10),
        superseded_by=final_draft.id,
    )
    draft_v2 = await _save_draft(
        tmp_db,
        headline="Draft v2",
        content="# v2\n\nSecond attempt, addressing early dissent.",
        created_at=now - timedelta(minutes=5),
        superseded_by=final_draft.id,
    )

    await _save_verdict(
        tmp_db,
        target=draft_v1,
        claim_holds=False,
        claim_confidence=4,
        dissents=["Argument A is weak.", "Evidence B is thin."],
        concurrences=[],
    )
    await _save_verdict(
        tmp_db,
        target=draft_v2,
        claim_holds=True,
        claim_confidence=5,
        dissents=["Argument A still weak."],
        concurrences=["Point C is solid."],
    )
    final_verdict_payload = {
        "stronger_side": "how_true",
        "claim_holds": True,
        "claim_confidence": 6,
        "rationale": "final rationale",
        "concurrences": ["Point C is solid.", "Point D holds."],
        "dissents": [],
        "sunset_after_days": None,
        "created_at": now.isoformat(),
    }
    await _save_verdict(
        tmp_db,
        target=final_draft,
        claim_holds=True,
        claim_confidence=6,
        dissents=[],
        concurrences=["Point C is solid.", "Point D holds."],
    )

    # Stamp refinement metadata on the final draft, as the orchestrator
    # would after acceptance.
    final_draft.extra = dict(final_draft.extra or {})
    final_draft.extra["refinement"] = {
        "iterations": 3,
        "outcome": "accepted",
        "final_verdict": final_verdict_payload,
        "dissents_addressed": [
            "Argument A is weak.",
            "Evidence B is thin.",
            "Argument A still weak.",
        ],
        "remaining_dissents": [],
        "immutable": True,
    }
    await tmp_db.save_page(final_draft)

    return final_draft, [draft_v1, draft_v2, final_draft]


async def test_iterations_happy_path(api_client, three_iteration_artifact):
    final, drafts = three_iteration_artifact
    resp = await api_client.get(f"/api/pages/{final.id}/iterations")
    assert resp.status_code == 200
    body = resp.json()

    assert body["page_id"] == final.id
    assert len(body["iterations"]) == 3

    iterations = body["iterations"]
    assert [it["iteration"] for it in iterations] == [1, 2, 3]
    assert [it["draft_page_id"] for it in iterations] == [d.id for d in drafts]
    assert [it["draft_short_id"] for it in iterations] == [d.id[:8] for d in drafts]
    assert iterations[0]["content"].startswith("# v1")
    assert iterations[2]["content"].startswith("# v3")


async def test_iterations_populates_verdicts(api_client, three_iteration_artifact):
    final, _ = three_iteration_artifact
    resp = await api_client.get(f"/api/pages/{final.id}/iterations")
    assert resp.status_code == 200
    iterations = resp.json()["iterations"]

    v1 = iterations[0]["verdict"]
    assert v1 is not None
    assert v1["claim_holds"] is False
    assert v1["claim_confidence"] == 4
    assert v1["dissents"] == ["Argument A is weak.", "Evidence B is thin."]
    assert v1["concurrences"] == []
    assert v1["stronger_side"] == "how_true"

    v3 = iterations[2]["verdict"]
    assert v3 is not None
    assert v3["claim_holds"] is True
    assert v3["claim_confidence"] == 6
    assert v3["concurrences"] == ["Point C is solid.", "Point D holds."]


async def test_iterations_rejects_non_artifact(api_client, tmp_db):
    claim = Page(
        page_type=PageType.CLAIM,
        layer=PageLayer.SQUIDGY,
        workspace=Workspace.RESEARCH,
        headline="a claim",
        content="claim body",
    )
    await tmp_db.save_page(claim)
    resp = await api_client.get(f"/api/pages/{claim.id}/iterations")
    assert resp.status_code == 400
    assert "not an artifact" in resp.json()["detail"]


async def test_iterations_rejects_artifact_without_refinement(api_client, tmp_db):
    # An artifact that never went through refine-artifact — e.g. a one-shot
    # draft that was never accepted or superseded. No refinement block = 400.
    artifact = Page(
        page_type=PageType.ARTIFACT,
        layer=PageLayer.SQUIDGY,
        workspace=Workspace.RESEARCH,
        headline="lone draft",
        content="body",
    )
    await tmp_db.save_page(artifact)
    resp = await api_client.get(f"/api/pages/{artifact.id}/iterations")
    assert resp.status_code == 400
    assert "refinement" in resp.json()["detail"]


async def test_iterations_404_for_missing_page(api_client):
    import uuid

    resp = await api_client.get(f"/api/pages/{uuid.uuid4()}/iterations")
    assert resp.status_code == 404


async def test_iterations_leaves_verdict_null_when_missing(api_client, tmp_db):
    # Single-iteration artifact — no prior drafts, no verdicts saved.
    now = datetime.now(UTC)
    artifact = Page(
        page_type=PageType.ARTIFACT,
        layer=PageLayer.SQUIDGY,
        workspace=Workspace.RESEARCH,
        headline="standalone",
        content="body",
        created_at=now,
        extra={
            "refinement": {
                "iterations": 1,
                "outcome": "accepted",
                "final_verdict": {},
                "dissents_addressed": [],
                "remaining_dissents": [],
                "immutable": True,
            },
        },
    )
    await tmp_db.save_page(artifact)
    resp = await api_client.get(f"/api/pages/{artifact.id}/iterations")
    assert resp.status_code == 200
    body = resp.json()
    assert len(body["iterations"]) == 1
    assert body["iterations"][0]["verdict"] is None
