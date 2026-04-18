"""Tests for the quality_control eval agent.

The agent is meant to scan a staged run's outputs for glaring errors and
emit one reputation event per finding (negative score). These tests mock
the LLM boundary so they are fast and hermetic.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

import pytest
import pytest_asyncio

from rumil.database import DB
from rumil.models import Page, PageLayer, PageType, Workspace
from rumil.run_eval import runner as run_eval_runner
from rumil.run_eval.agents import EVAL_AGENTS, EvalAgentSpec
from rumil.run_eval.quality_control import (
    MAX_FINDINGS_PER_RUN,
    QualityControlFinding,
    Severity,
    cap_findings,
    format_findings_markdown,
    parse_findings_from_report,
    severity_to_score,
)


def _make_report_with_findings(findings: list[dict]) -> str:
    """Build a markdown report that embeds a findings JSON block."""
    import json

    return (
        "## Summary\n\n"
        "Some QC notes here.\n\n"
        "## Findings\n\n"
        "```json\n" + json.dumps({"findings": findings}) + "\n```\n"
    )


async def _make_db(project_id: str, staged: bool = False) -> DB:
    from datetime import UTC, datetime

    db = await DB.create(run_id=str(uuid.uuid4()), staged=staged)
    db.project_id = project_id
    if staged:
        db.snapshot_ts = datetime.max.replace(tzinfo=UTC)
    return db


@pytest_asyncio.fixture
async def project_id():
    db = await DB.create(run_id=str(uuid.uuid4()))
    project = await db.get_or_create_project(f"test-qc-{uuid.uuid4().hex[:8]}")
    yield project.id
    await db._execute(db.client.table("projects").delete().eq("id", project.id))


@pytest_asyncio.fixture
async def run_db(project_id):
    db = await _make_db(project_id, staged=False)
    await db.create_run(name="test", question_id=None, config={"orchestrator": "two_phase"})
    await db.init_budget(100)
    yield db
    await db.delete_run_data()


def test_quality_control_agent_is_registered():
    """The QC agent shows up in the canonical EVAL_AGENTS list."""
    names = {a.name for a in EVAL_AGENTS}
    assert "quality_control" in names
    spec = next(a for a in EVAL_AGENTS if a.name == "quality_control")
    assert spec.display_name == "Quality Control"
    assert spec.prompt_file == "run-eval-quality-control.md"


def test_severity_scores_are_negative():
    """All severity buckets produce negative scores (quality deficit)."""
    assert severity_to_score(Severity.LOW) == -0.3
    assert severity_to_score(Severity.MODERATE) == -0.6
    assert severity_to_score(Severity.CRITICAL) == -1.0


def test_parse_findings_from_report_happy_path():
    report = _make_report_with_findings(
        [
            {
                "kind": "broken_citation",
                "page_ids": ["c-abcd1234"],
                "severity": "moderate",
                "evidence": "claim X cites source Y but Y does not say X",
                "suggested_fix": "drop the citation",
            },
            {
                "kind": "overconfident_claim",
                "page_ids": ["c-efgh5678"],
                "severity": "critical",
                "evidence": "credence 9 on a single-source claim",
            },
        ]
    )
    findings = parse_findings_from_report(report)
    assert len(findings) == 2
    assert findings[0].kind == "broken_citation"
    assert findings[0].severity == Severity.MODERATE
    assert findings[0].page_ids == ["c-abcd1234"]
    assert findings[1].severity == Severity.CRITICAL
    assert findings[1].suggested_fix == ""


def test_parse_findings_handles_bare_array():
    report = (
        "## Findings\n\n"
        "```json\n"
        '[{"kind":"factual_error","page_ids":["p1"],'
        '"severity":"low","evidence":"off by one"}]'
        "\n```\n"
    )
    findings = parse_findings_from_report(report)
    assert len(findings) == 1
    assert findings[0].kind == "factual_error"


def test_parse_findings_from_empty_list():
    report = _make_report_with_findings([])
    assert parse_findings_from_report(report) == []


def test_parse_findings_from_no_json_block():
    report = "nothing structured here, just prose."
    assert parse_findings_from_report(report) == []


def test_parse_findings_skips_malformed_entries():
    """Items missing required fields are skipped but valid ones still parse."""
    report = _make_report_with_findings(
        [
            {"kind": "incomplete"},
            {
                "kind": "broken_citation",
                "page_ids": ["p1"],
                "severity": "low",
                "evidence": "valid one",
            },
        ]
    )
    findings = parse_findings_from_report(report)
    assert len(findings) == 1
    assert findings[0].kind == "broken_citation"


def test_cap_findings_enforces_limit():
    many = [
        QualityControlFinding(
            kind="orphan_view_item",
            page_ids=[f"p{i}"],
            severity=Severity.LOW,
            evidence=f"finding {i}",
        )
        for i in range(25)
    ]
    capped = cap_findings(many)
    assert len(capped) == MAX_FINDINGS_PER_RUN == 20


def test_cap_findings_keeps_critical_first():
    """Critical findings always survive the cap; low ones get dropped."""
    items: list[QualityControlFinding] = []
    for i in range(18):
        items.append(
            QualityControlFinding(
                kind="orphan_view_item",
                page_ids=[f"low{i}"],
                severity=Severity.LOW,
                evidence="low finding",
            )
        )
    for i in range(5):
        items.append(
            QualityControlFinding(
                kind="broken_citation",
                page_ids=[f"crit{i}"],
                severity=Severity.CRITICAL,
                evidence="critical finding",
            )
        )
    capped = cap_findings(items)
    assert len(capped) == 20
    critical = [f for f in capped if f.severity == Severity.CRITICAL]
    assert len(critical) == 5


def test_format_findings_markdown_empty():
    assert format_findings_markdown([]) == "_No quality-control findings flagged._"


def test_format_findings_markdown_renders_pages_and_fix():
    findings = [
        QualityControlFinding(
            kind="broken_citation",
            page_ids=["p1", "p2"],
            severity=Severity.CRITICAL,
            evidence="cites nothing",
            suggested_fix="fix it",
        )
    ]
    out = format_findings_markdown(findings)
    assert "critical" in out
    assert "p1" in out and "p2" in out
    assert "fix it" in out


async def test_qc_agent_completion_emits_per_finding_reputation_events(run_db, mocker):
    """Running the QC agent parses its report and fires one event per finding."""
    question = Page(
        page_type=PageType.QUESTION,
        layer=PageLayer.SQUIDGY,
        workspace=Workspace.RESEARCH,
        headline="test question",
        content="body",
        extra={"task_shape": {"kind": "open_ended"}},
    )
    await run_db.save_page(question)

    spec = EvalAgentSpec(
        name="quality_control",
        display_name="Quality Control",
        prompt_file="run-eval-quality-control.md",
    )

    report_text = _make_report_with_findings(
        [
            {
                "kind": "broken_citation",
                "page_ids": ["c-abcd1234"],
                "severity": "moderate",
                "evidence": "claim cites unrelated source",
                "suggested_fix": "drop citation",
            },
            {
                "kind": "overconfident_claim",
                "page_ids": ["c-efgh5678"],
                "severity": "critical",
                "evidence": "credence 9, no sources",
            },
        ]
    )

    @dataclass
    class _FakeResult:
        all_assistant_text: list[str]

    mocker.patch(
        "rumil.run_eval.runner.run_sdk_agent",
        return_value=_FakeResult(all_assistant_text=[report_text]),
    )
    mocker.patch(
        "rumil.run_eval.runner.explore_page_impl",
        return_value="graph context",
    )

    await run_eval_runner.evaluate_run_with_agent(
        spec,
        run_id=run_db.run_id,
        question_id=question.id,
        parent_db=run_db,
        broadcaster=None,
    )

    qc_events = await run_db.get_reputation_events(
        source="eval_agent",
        dimension="quality_control",
    )
    # One completion sentinel (score=1.0) + one event per finding (negative).
    assert len(qc_events) == 3

    completion = [e for e in qc_events if e.score > 0]
    assert len(completion) == 1
    assert completion[0].score == 1.0

    finding_events = sorted((e for e in qc_events if e.score < 0), key=lambda e: e.score)
    assert len(finding_events) == 2
    assert finding_events[0].score == -1.0
    assert finding_events[0].extra["severity"] == "critical"
    assert finding_events[0].extra["kind"] == "overconfident_claim"
    assert finding_events[1].score == -0.6
    assert finding_events[1].extra["severity"] == "moderate"
    assert finding_events[1].extra["page_ids"] == ["c-abcd1234"]
    for e in finding_events:
        assert e.extra["subject_run_id"] == run_db.run_id
        assert e.orchestrator == "two_phase"
        assert e.task_shape == {"kind": "open_ended"}


async def test_qc_cap_is_enforced_on_reputation_emission(run_db, mocker):
    """A report with >20 findings results in exactly 20 finding events."""
    question = Page(
        page_type=PageType.QUESTION,
        layer=PageLayer.SQUIDGY,
        workspace=Workspace.RESEARCH,
        headline="q",
        content="c",
    )
    await run_db.save_page(question)

    spec = EvalAgentSpec(
        name="quality_control",
        display_name="Quality Control",
        prompt_file="run-eval-quality-control.md",
    )

    findings_payload = [
        {
            "kind": "orphan_view_item",
            "page_ids": [f"p{i}"],
            "severity": "low",
            "evidence": f"orphan {i}",
        }
        for i in range(30)
    ]
    report_text = _make_report_with_findings(findings_payload)

    @dataclass
    class _FakeResult:
        all_assistant_text: list[str]

    mocker.patch(
        "rumil.run_eval.runner.run_sdk_agent",
        return_value=_FakeResult(all_assistant_text=[report_text]),
    )
    mocker.patch(
        "rumil.run_eval.runner.explore_page_impl",
        return_value="graph context",
    )

    await run_eval_runner.evaluate_run_with_agent(
        spec,
        run_id=run_db.run_id,
        question_id=question.id,
        parent_db=run_db,
        broadcaster=None,
    )

    finding_events = [
        e
        for e in await run_db.get_reputation_events(
            source="eval_agent",
            dimension="quality_control",
        )
        if e.score < 0
    ]
    assert len(finding_events) == MAX_FINDINGS_PER_RUN


async def test_qc_findings_show_up_in_reputation_summary(run_db, mocker):
    """The dashboard summary bucketises the quality_control dimension."""
    question = Page(
        page_type=PageType.QUESTION,
        layer=PageLayer.SQUIDGY,
        workspace=Workspace.RESEARCH,
        headline="q",
        content="c",
    )
    await run_db.save_page(question)

    spec = EvalAgentSpec(
        name="quality_control",
        display_name="Quality Control",
        prompt_file="run-eval-quality-control.md",
    )
    report_text = _make_report_with_findings(
        [
            {
                "kind": "broken_citation",
                "page_ids": ["c-aa"],
                "severity": "moderate",
                "evidence": "x",
            },
            {
                "kind": "factual_error",
                "page_ids": ["c-bb"],
                "severity": "critical",
                "evidence": "y",
            },
        ]
    )

    @dataclass
    class _FakeResult:
        all_assistant_text: list[str]

    mocker.patch(
        "rumil.run_eval.runner.run_sdk_agent",
        return_value=_FakeResult(all_assistant_text=[report_text]),
    )
    mocker.patch(
        "rumil.run_eval.runner.explore_page_impl",
        return_value="graph context",
    )

    await run_eval_runner.evaluate_run_with_agent(
        spec,
        run_id=run_db.run_id,
        question_id=question.id,
        parent_db=run_db,
        broadcaster=None,
    )

    summary = await run_db.get_reputation_summary(run_db.project_id)
    qc_bucket = next(
        (b for b in summary if b["source"] == "eval_agent" and b["dimension"] == "quality_control"),
        None,
    )
    assert qc_bucket is not None
    assert qc_bucket["n_events"] == 3
    assert qc_bucket["min_score"] == -1.0
    assert qc_bucket["max_score"] == 1.0


async def test_qc_idempotency_policy_is_append_only(run_db, mocker):
    """Running QC twice on the same run appends events.

    Policy: the reputation substrate is append-only (see CLAUDE.md and
    marketplace-thread/07-feedback.md) — we do NOT dedupe at write time.
    The dashboard can dedupe by (source_call_id, extra.kind, page_ids)
    when rendering if it wants. This test pins the policy so future
    changes are deliberate.
    """
    question = Page(
        page_type=PageType.QUESTION,
        layer=PageLayer.SQUIDGY,
        workspace=Workspace.RESEARCH,
        headline="q",
        content="c",
    )
    await run_db.save_page(question)

    spec = EvalAgentSpec(
        name="quality_control",
        display_name="Quality Control",
        prompt_file="run-eval-quality-control.md",
    )
    report_text = _make_report_with_findings(
        [
            {
                "kind": "factual_error",
                "page_ids": ["c-aa"],
                "severity": "low",
                "evidence": "z",
            },
        ]
    )

    @dataclass
    class _FakeResult:
        all_assistant_text: list[str]

    mocker.patch(
        "rumil.run_eval.runner.run_sdk_agent",
        return_value=_FakeResult(all_assistant_text=[report_text]),
    )
    mocker.patch(
        "rumil.run_eval.runner.explore_page_impl",
        return_value="graph context",
    )

    await run_eval_runner.evaluate_run_with_agent(
        spec,
        run_id=run_db.run_id,
        question_id=question.id,
        parent_db=run_db,
        broadcaster=None,
    )
    await run_eval_runner.evaluate_run_with_agent(
        spec,
        run_id=run_db.run_id,
        question_id=question.id,
        parent_db=run_db,
        broadcaster=None,
    )

    finding_events = [
        e
        for e in await run_db.get_reputation_events(
            source="eval_agent",
            dimension="quality_control",
        )
        if e.score < 0
    ]
    # Two QC runs, one finding each = 2 finding events, not 1.
    assert len(finding_events) == 2


@pytest.mark.parametrize(
    ("severity_str", "expected_score"),
    [
        ("low", -0.3),
        ("moderate", -0.6),
        ("critical", -1.0),
    ],
)
def test_severity_parsing_maps_to_score(severity_str: str, expected_score: float):
    finding = QualityControlFinding(
        kind="x",
        page_ids=[],
        severity=Severity(severity_str),
        evidence="e",
    )
    assert severity_to_score(finding.severity) == expected_score
