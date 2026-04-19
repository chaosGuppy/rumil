"""Tests for the /api/pages/{page_id}/adversarial-verdicts endpoint.

The endpoint enriches claim pages with any ``adversarial_verdict`` JUDGEMENT
pages pointing at them (inbound DEPENDS_ON links). It powers the inline
verdict badge on view items / claims in parma.

These tests seed verdict pages directly, so no LLM calls — they exercise
the API layer and the batched verdict-collection path end-to-end.
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


async def _seed_claim(tmp_db, headline: str = "Dennard scaling ended around 2006.") -> Page:
    claim = Page(
        page_type=PageType.CLAIM,
        layer=PageLayer.SQUIDGY,
        workspace=Workspace.RESEARCH,
        headline=headline,
        content=headline,
        credence=7,
        robustness=3,
    )
    await tmp_db.save_page(claim)
    return claim


async def _seed_verdict(
    tmp_db,
    target: Page,
    *,
    stronger_side: str = "how_true",
    claim_holds: bool = True,
    confidence: int = 7,
    rationale: str = "how-true scout produced a tighter case.",
    concurrences: list[str] | None = None,
    dissents: list[str] | None = None,
    sunset_after_days: int | None = 180,
    created_at: datetime | None = None,
) -> Page:
    created_at = created_at or datetime.now(UTC)
    verdict_payload = {
        "stronger_side": stronger_side,
        "claim_holds": claim_holds,
        "confidence": confidence,
        "rationale": rationale,
        "concurrences": concurrences or [],
        "dissents": dissents or [],
        "sunset_after_days": sunset_after_days,
        "created_at": created_at.isoformat(),
    }
    verdict = Page(
        page_type=PageType.JUDGEMENT,
        layer=PageLayer.SQUIDGY,
        workspace=Workspace.RESEARCH,
        headline=f"Adversarial verdict: {target.headline[:60]}",
        content="verdict body",
        credence=confidence,
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
async def claim_with_verdict(tmp_db):
    claim = await _seed_claim(tmp_db)
    verdict = await _seed_verdict(
        tmp_db,
        claim,
        dissents=["Dennard scaling arguably ended a couple of years earlier."],
        concurrences=["Power-wall evidence is independently corroborated."],
    )
    return claim, verdict


async def test_endpoint_returns_verdict_summary(api_client, claim_with_verdict):
    claim, verdict = claim_with_verdict
    resp = await api_client.get(f"/api/pages/{claim.id}/adversarial-verdicts")
    assert resp.status_code == 200
    body = resp.json()
    assert len(body) == 1
    entry = body[0]
    assert entry["verdict_page_id"] == verdict.id
    assert entry["target_page_id"] == claim.id
    assert entry["stronger_side"] == "how_true"
    assert entry["claim_holds"] is True
    assert entry["confidence"] == 7
    assert entry["dissents"] == [
        "Dennard scaling arguably ended a couple of years earlier.",
    ]
    assert entry["concurrences"] == [
        "Power-wall evidence is independently corroborated.",
    ]
    assert entry["expired"] is False


async def test_endpoint_returns_empty_when_no_verdict(api_client, tmp_db):
    claim = await _seed_claim(tmp_db, headline="Transistor counts doubled every 2 years.")
    resp = await api_client.get(f"/api/pages/{claim.id}/adversarial-verdicts")
    assert resp.status_code == 200
    assert resp.json() == []


async def test_endpoint_marks_expired_verdicts(api_client, tmp_db):
    claim = await _seed_claim(tmp_db, headline="Power wall forced the multicore turn.")
    stale_created = datetime.now(UTC) - timedelta(days=400)
    await _seed_verdict(
        tmp_db,
        claim,
        sunset_after_days=180,
        created_at=stale_created,
    )
    resp = await api_client.get(f"/api/pages/{claim.id}/adversarial-verdicts")
    assert resp.status_code == 200
    body = resp.json()
    assert len(body) == 1
    assert body[0]["expired"] is True


async def test_batch_endpoint_covers_many_pages(api_client, tmp_db):
    claim_a = await _seed_claim(tmp_db, headline="Claim A")
    claim_b = await _seed_claim(tmp_db, headline="Claim B")
    claim_c = await _seed_claim(tmp_db, headline="Claim C (no verdict)")
    await _seed_verdict(tmp_db, claim_a, stronger_side="how_true")
    await _seed_verdict(tmp_db, claim_b, stronger_side="how_false", claim_holds=False)

    joined = ",".join([claim_a.id, claim_b.id, claim_c.id])
    resp = await api_client.get(f"/api/adversarial-verdicts?page_ids={joined}")
    assert resp.status_code == 200
    body = resp.json()

    assert len(body[claim_a.id]) == 1
    assert body[claim_a.id][0]["stronger_side"] == "how_true"
    assert len(body[claim_b.id]) == 1
    assert body[claim_b.id][0]["claim_holds"] is False
    assert body[claim_c.id] == []


async def test_endpoint_newest_first(api_client, tmp_db):
    claim = await _seed_claim(tmp_db, headline="Reviewed twice")
    older = datetime.now(UTC) - timedelta(days=10)
    newer = datetime.now(UTC)
    await _seed_verdict(tmp_db, claim, rationale="older", created_at=older)
    await _seed_verdict(tmp_db, claim, rationale="newer", created_at=newer)

    resp = await api_client.get(f"/api/pages/{claim.id}/adversarial-verdicts")
    assert resp.status_code == 200
    body = resp.json()
    assert len(body) == 2
    assert body[0]["rationale"] == "newer"
    assert body[1]["rationale"] == "older"
