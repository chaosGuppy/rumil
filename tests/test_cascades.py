import pytest

from rumil.cascades import check_cascades, _significant_changes
from rumil.models import (
    LinkType,
    Page,
    PageLayer,
    PageLink,
    PageType,
    SuggestionType,
    Workspace,
)


def _make_claim(headline: str, credence: int = 5, robustness: int = 3) -> Page:
    return Page(
        page_type=PageType.CLAIM,
        layer=PageLayer.SQUIDGY,
        workspace=Workspace.RESEARCH,
        content=f"Content for {headline}",
        headline=headline,
        credence=credence,
        robustness=robustness,
    )


async def _setup_dependency(tmp_db, upstream: Page, dependent: Page) -> None:
    await tmp_db.save_page(upstream)
    await tmp_db.save_page(dependent)
    await tmp_db.save_link(
        PageLink(
            from_page_id=dependent.id,
            to_page_id=upstream.id,
            link_type=LinkType.DEPENDS_ON,
            strength=4.0,
            reasoning="builds on upstream",
        )
    )


def test_significant_changes_filters_to_cascade_fields():
    result = _significant_changes(
        {
            "credence": (3, 6),
            "headline": ("old", "new"),
            "robustness": (1, 4),
        }
    )
    assert set(result.keys()) == {"credence", "robustness"}


def test_significant_changes_ignores_small_delta():
    result = _significant_changes({"credence": (5, 6)})
    assert result == {}


def test_significant_changes_ignores_non_int_values():
    result = _significant_changes({"credence": (5.0, 8.0)})
    assert result == {}


def test_significant_changes_exact_threshold():
    result = _significant_changes({"credence": (5, 7)})
    assert result == {"credence": (5, 7)}


@pytest.mark.asyncio
async def test_cascade_on_credence_change(tmp_db):
    upstream = _make_claim("Upstream claim", credence=5)
    dependent = _make_claim("Dependent claim", credence=6)
    await _setup_dependency(tmp_db, upstream, dependent)

    suggestions = await check_cascades(
        tmp_db,
        upstream.id,
        {"credence": (5, 7)},
    )

    assert len(suggestions) == 1
    assert suggestions[0].target_page_id == dependent.id
    assert suggestions[0].source_page_id == upstream.id
    assert suggestions[0].suggestion_type == SuggestionType.CASCADE_REVIEW


@pytest.mark.asyncio
async def test_no_cascade_for_small_credence_change(tmp_db):
    upstream = _make_claim("Upstream claim", credence=5)
    dependent = _make_claim("Dependent claim", credence=6)
    await _setup_dependency(tmp_db, upstream, dependent)

    suggestions = await check_cascades(
        tmp_db,
        upstream.id,
        {"credence": (5, 6)},
    )

    assert len(suggestions) == 0


@pytest.mark.asyncio
async def test_cascade_on_robustness_change(tmp_db):
    upstream = _make_claim("Upstream claim")
    dependent = _make_claim("Dependent claim")
    await _setup_dependency(tmp_db, upstream, dependent)

    suggestions = await check_cascades(
        tmp_db,
        upstream.id,
        {"robustness": (2, 5)},
    )

    assert len(suggestions) == 1
    assert suggestions[0].target_page_id == dependent.id


@pytest.mark.asyncio
async def test_cascade_on_importance_change(tmp_db):
    upstream = _make_claim("Upstream claim")
    dependent = _make_claim("Dependent claim")
    await _setup_dependency(tmp_db, upstream, dependent)

    suggestions = await check_cascades(
        tmp_db,
        upstream.id,
        {"importance": (1, 4)},
    )

    assert len(suggestions) == 1
    assert suggestions[0].target_page_id == dependent.id


@pytest.mark.asyncio
async def test_no_cascade_when_no_dependents(tmp_db):
    upstream = _make_claim("Lonely claim")
    await tmp_db.save_page(upstream)

    suggestions = await check_cascades(
        tmp_db,
        upstream.id,
        {"credence": (3, 8)},
    )

    assert len(suggestions) == 0


@pytest.mark.asyncio
async def test_multiple_dependents(tmp_db):
    upstream = _make_claim("Upstream claim")
    dep_a = _make_claim("Dependent A")
    dep_b = _make_claim("Dependent B")
    await tmp_db.save_page(upstream)
    await tmp_db.save_page(dep_a)
    await tmp_db.save_page(dep_b)

    for dep in (dep_a, dep_b):
        await tmp_db.save_link(
            PageLink(
                from_page_id=dep.id,
                to_page_id=upstream.id,
                link_type=LinkType.DEPENDS_ON,
                strength=4.0,
                reasoning="depends on upstream",
            )
        )

    suggestions = await check_cascades(
        tmp_db,
        upstream.id,
        {"credence": (2, 7)},
    )

    assert len(suggestions) == 2
    target_ids = {s.target_page_id for s in suggestions}
    assert target_ids == {dep_a.id, dep_b.id}


@pytest.mark.asyncio
async def test_suggestion_payload_structure(tmp_db):
    upstream = _make_claim("Upstream claim", credence=5, robustness=3)
    dependent = _make_claim("Dependent claim")
    await _setup_dependency(tmp_db, upstream, dependent)

    suggestions = await check_cascades(
        tmp_db,
        upstream.id,
        {"credence": (5, 8)},
    )

    assert len(suggestions) == 1
    payload = suggestions[0].payload
    assert payload["changed_page_id"] == upstream.id
    assert payload["changed_headline"] == "Upstream claim"
    assert payload["dependent_page_id"] == dependent.id
    assert payload["dependent_headline"] == "Dependent claim"
    assert payload["changes"] == {"credence": {"old": 5, "new": 8}}
    assert "reasoning" in payload
    assert upstream.id[:8] in payload["reasoning"]
    assert dependent.id[:8] in payload["reasoning"]
