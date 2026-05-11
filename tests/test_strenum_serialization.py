"""Pin down str-Enum/StrEnum serialization behavior across boundaries.

Migration safety net: when CallType, Workspace, etc. switched from
``class X(str, Enum)`` to ``class X(StrEnum)``, equality, hashing, and
Pydantic/JSON serialization had to remain identical at every boundary
where these enums cross the wire (DB rows, API responses, dispatch
payloads). The only change ``StrEnum`` introduces is ``__str__``/
``f"{x}"`` (now the value, not ``ClassName.MEMBER``), and we already
audited the codebase to confirm we never relied on the old form.
"""

import json

from rumil.models import (
    CallStage,
    CallStatus,
    CallType,
    ConsiderationDirection,
    LinkRole,
    LinkType,
    MoveType,
    Page,
    PageDetail,
    PageLayer,
    PageType,
    ScoutScope,
    Workspace,
)


def test_workspace_value_round_trip():
    assert Workspace.RESEARCH == "research"
    assert Workspace("research") is Workspace.RESEARCH
    assert json.dumps(Workspace.RESEARCH) == '"research"'


def test_calltype_value_round_trip():
    assert CallType.ASSESS == "assess"
    assert CallType("assess") is CallType.ASSESS
    assert json.dumps(CallType.ASSESS) == '"assess"'


def test_all_str_enums_equal_their_value():
    cases = [
        (PageType.CLAIM, "claim"),
        (PageDetail.HEADLINE, "headline"),
        (PageLayer.SQUIDGY, "squidgy"),
        (Workspace.RESEARCH, "research"),
        (CallType.ASSESS, "assess"),
        (ScoutScope.QUESTION, "question"),
        (CallStatus.COMPLETE, "complete"),
        (LinkType.CONSIDERATION, "consideration"),
        (MoveType.CREATE_CLAIM, "CREATE_CLAIM"),
        (CallStage.BUILD_CONTEXT, "build_context"),
        (LinkRole.DIRECT, "direct"),
        (ConsiderationDirection.SUPPORTS, "supports"),
    ]
    for member, value in cases:
        assert member == value
        assert hash(member) == hash(value)
        assert isinstance(member, str)


def test_pydantic_dump_serializes_to_value():
    page = Page(
        page_type=PageType.CLAIM,
        layer=PageLayer.SQUIDGY,
        workspace=Workspace.RESEARCH,
        headline="x",
        content="y",
    )
    dumped = page.model_dump()
    assert dumped["page_type"] == "claim"
    assert dumped["layer"] == "squidgy"
    assert dumped["workspace"] == "research"
    payload = json.loads(page.model_dump_json())
    assert payload["page_type"] == "claim"
    assert payload["workspace"] == "research"


def test_pydantic_validate_from_value():
    page = Page.model_validate(
        {
            "page_type": "claim",
            "layer": "squidgy",
            "workspace": "research",
            "headline": "x",
            "content": "y",
        }
    )
    assert page.page_type is PageType.CLAIM
    assert page.layer is PageLayer.SQUIDGY
    assert page.workspace is Workspace.RESEARCH


def test_format_uses_underlying_value():
    """StrEnum's __str__/__format__ returns the value (PEP 663). The codebase
    already relies on .value at every formatted call site, but pin it here so
    that the behavior is enforced as a contract."""
    assert f"{Workspace.RESEARCH}" == "research"
    assert f"{CallType.ASSESS}" == "assess"
    assert str(Workspace.RESEARCH) == "research"
    assert str(CallType.ASSESS) == "assess"
