"""Structural and distributional health checks for a rumil question subtree.

Three scan functions, all cheap DB queries with no LLM cost:

- ``graph_health``   — page/link topology problems (barren questions, orphans, etc.)
- ``rating_shape``   — credence/robustness distribution diagnostics
- ``review_signals`` — aggregation of self-reported review_json from calls

Each returns a list of ``Finding`` objects. ``scan_all`` runs all three.
Runnable standalone:

    PYTHONPATH=.claude/lib uv run python -m rumil_skills.scan <question_id>
    PYTHONPATH=.claude/lib uv run python -m rumil_skills.scan <question_id> --checks graph
    PYTHONPATH=.claude/lib uv run python -m rumil_skills.scan <question_id> --checks rating,review
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from collections import Counter
from dataclasses import dataclass, field
from typing import Any

from rumil.database import DB
from rumil.models import (
    ConsiderationDirection,
    LinkType,
    Page,
    PageLink,
    PageType,
)

from ._format import short, truncate
from ._runctx import make_db


@dataclass
class Finding:
    category: str  # "graph_health", "rating_shape", "review_signals"
    severity: int  # 1-5
    code: str  # machine-readable key, e.g. "barren_question"
    description: str  # human-readable one-liner
    page_ids: list[str] = field(default_factory=list)
    suggested_action: str = ""


@dataclass
class SubtreeData:
    """All pages and links reachable from a root question, collected by BFS."""

    root_id: str
    pages: dict[str, Page]  # id -> Page
    questions: list[Page]
    claims: list[Page]
    judgements: list[Page]
    sources: list[Page]
    links_from: dict[str, list[PageLink]]  # page_id -> outgoing links
    links_to: dict[str, list[PageLink]]  # page_id -> incoming links


async def collect_subtree(db: DB, root_id: str) -> SubtreeData:
    """BFS through child questions, collecting all pages and their links."""
    pages: dict[str, Page] = {}
    question_ids: list[str] = []
    frontier = [root_id]
    seen: set[str] = set()

    while frontier:
        next_frontier: list[str] = []
        new_ids = [qid for qid in frontier if qid not in seen]
        if not new_ids:
            break
        seen.update(new_ids)

        q_pages = await db.get_pages_by_ids(new_ids)
        for qid, page in q_pages.items():
            if page.is_active():
                pages[qid] = page
                question_ids.append(qid)

        for qid in new_ids:
            cons_pairs = await db.get_considerations_for_question(qid)
            for claim, _link in cons_pairs:
                pages[claim.id] = claim
            judgements = await db.get_judgements_for_question(qid)
            for j in judgements:
                pages[j.id] = j
            children = await db.get_child_questions(qid)
            for child in children:
                if child.id not in seen:
                    next_frontier.append(child.id)
        frontier = next_frontier

    all_ids = list(pages.keys())
    links_from = await db.get_links_from_many(all_ids)
    links_to = await db.get_links_to_many(all_ids)

    questions = [p for p in pages.values() if p.page_type == PageType.QUESTION]
    claims = [p for p in pages.values() if p.page_type == PageType.CLAIM]
    judgements = [p for p in pages.values() if p.page_type == PageType.JUDGEMENT]
    sources = [p for p in pages.values() if p.page_type == PageType.SOURCE]

    return SubtreeData(
        root_id=root_id,
        pages=pages,
        questions=questions,
        claims=claims,
        judgements=judgements,
        sources=sources,
        links_from=links_from,
        links_to=links_to,
    )


def _consideration_links_to(
    data: SubtreeData, question_id: str,
) -> list[PageLink]:
    return [
        l for l in data.links_to.get(question_id, [])
        if l.link_type == LinkType.CONSIDERATION
    ]


def _judgement_links_to(
    data: SubtreeData, question_id: str,
) -> list[PageLink]:
    return [
        l for l in data.links_to.get(question_id, [])
        if l.link_type == LinkType.ANSWERS
    ]


def _depends_on_inbound(
    data: SubtreeData, page_id: str,
) -> list[PageLink]:
    return [
        l for l in data.links_to.get(page_id, [])
        if l.link_type == LinkType.DEPENDS_ON
    ]


def _headline(data: SubtreeData, page_id: str) -> str:
    page = data.pages.get(page_id)
    return truncate(page.headline, 60) if page else "?"


def graph_health(data: SubtreeData) -> list[Finding]:
    """Structural graph health checks on the subtree."""
    findings: list[Finding] = []

    for q in data.questions:
        cons = _consideration_links_to(data, q.id)
        judgs = _judgement_links_to(data, q.id)
        headline = truncate(q.headline, 60)

        if not cons and q.id != data.root_id:
            findings.append(Finding(
                category="graph_health",
                severity=3,
                code="barren_question",
                description=(
                    f"{short(q)} ({headline}) has 0 considerations"
                ),
                page_ids=[q.id],
                suggested_action="dispatch find_considerations",
            ))

        if len(cons) >= 3 and not judgs:
            findings.append(Finding(
                category="graph_health",
                severity=3,
                code="unjudged_question",
                description=(
                    f"{short(q)} ({headline}) has {len(cons)} considerations "
                    f"but no judgement"
                ),
                page_ids=[q.id],
                suggested_action="dispatch assess",
            ))

    for claim in data.claims:
        outgoing_cons = [
            l for l in data.links_from.get(claim.id, [])
            if l.link_type == LinkType.CONSIDERATION
        ]
        if not outgoing_cons:
            findings.append(Finding(
                category="graph_health",
                severity=2,
                code="orphaned_claim",
                description=(
                    f"{short(claim)} ({truncate(claim.headline, 60)}) "
                    f"not linked as consideration to any question"
                ),
                page_ids=[claim.id],
                suggested_action="inspect",
            ))

    for claim in data.claims:
        deps = _depends_on_inbound(data, claim.id)
        if (
            len(deps) >= 2
            and claim.robustness is not None
            and claim.robustness <= 2
        ):
            dependents = [short(l.from_page_id) for l in deps[:4]]
            findings.append(Finding(
                category="graph_health",
                severity=4,
                code="load_bearing_fragile",
                description=(
                    f"{short(claim)} ({truncate(claim.headline, 50)}) "
                    f"has {len(deps)} dependents but robustness={claim.robustness}. "
                    f"Dependents: {', '.join(dependents)}"
                ),
                page_ids=[claim.id] + [l.from_page_id for l in deps],
                suggested_action="dispatch scout_c_robustify or assess",
            ))

    for q in data.questions:
        children = [
            l for l in data.links_from.get(q.id, [])
            if l.link_type == LinkType.CHILD_QUESTION
        ]
        if not children:
            continue
        all_barren = all(
            not _consideration_links_to(data, l.to_page_id)
            for l in children
        )
        if all_barren and len(children) >= 2:
            findings.append(Finding(
                category="graph_health",
                severity=3,
                code="dead_end_decomposition",
                description=(
                    f"{short(q)} ({truncate(q.headline, 50)}) has "
                    f"{len(children)} sub-questions, all barren"
                ),
                page_ids=[q.id] + [l.to_page_id for l in children],
                suggested_action="dispatch find_considerations on sub-questions",
            ))

    for page in data.pages.values():
        if not page.is_superseded:
            continue
        supersedes_links = [
            l for l in data.links_to.get(page.id, [])
            if l.link_type == LinkType.SUPERSEDES
        ]
        for link in supersedes_links:
            replacement = data.pages.get(link.from_page_id)
            if replacement and replacement.is_superseded:
                findings.append(Finding(
                    category="graph_health",
                    severity=2,
                    code="chained_supersession",
                    description=(
                        f"{short(page)} superseded by {short(link.from_page_id)} "
                        f"which is itself superseded — possible churn"
                    ),
                    page_ids=[page.id, link.from_page_id],
                    suggested_action="inspect",
                ))

    findings.sort(key=lambda f: f.severity, reverse=True)
    return findings


def _spearman_rho(xs: list[int], ys: list[int]) -> float:
    """Spearman rank correlation for two equal-length int sequences."""
    n = len(xs)
    if n < 3:
        return 0.0

    def _rank(vals: list[int]) -> list[float]:
        indexed = sorted(enumerate(vals), key=lambda t: t[1])
        ranks = [0.0] * n
        i = 0
        while i < n:
            j = i
            while j < n and indexed[j][1] == indexed[i][1]:
                j += 1
            avg_rank = (i + j - 1) / 2.0 + 1
            for k in range(i, j):
                ranks[indexed[k][0]] = avg_rank
            i = j
        return ranks

    rx = _rank(xs)
    ry = _rank(ys)
    d_sq = sum((a - b) ** 2 for a, b in zip(rx, ry))
    return 1 - (6 * d_sq) / (n * (n * n - 1))


def rating_shape(data: SubtreeData) -> list[Finding]:
    """Credence/robustness distribution diagnostics."""
    findings: list[Finding] = []
    rated = [p for p in data.claims if p.credence is not None and p.robustness is not None]

    if len(rated) < 3:
        if data.claims and not rated:
            findings.append(Finding(
                category="rating_shape",
                severity=2,
                code="no_ratings",
                description=f"{len(data.claims)} claims, none have ratings",
                suggested_action="inspect",
            ))
        return findings

    creds = [p.credence for p in rated]  # type: ignore[misc]
    robs = [p.robustness for p in rated]  # type: ignore[misc]
    n = len(rated)
    mean_c = sum(creds) / n
    mean_r = sum(robs) / n

    rho = _spearman_rho(creds, robs)
    if abs(rho) > 0.85:
        findings.append(Finding(
            category="rating_shape",
            severity=4,
            code="dimensions_collapsing",
            description=(
                f"Credence and robustness are near-perfectly correlated "
                f"(Spearman rho={rho:.2f}, n={n}). The two dimensions "
                f"may not be providing independent signal."
            ),
            suggested_action="inspect prompt guidance for credence/robustness",
        ))

    high_cred_low_rob = [p for p in rated if p.credence >= 7 and p.robustness <= 2]  # type: ignore[operator]
    low_cred_high_rob = [p for p in rated if p.credence <= 4 and p.robustness >= 4]  # type: ignore[operator]

    if n >= 5 and not high_cred_low_rob and not low_cred_high_rob:
        findings.append(Finding(
            category="rating_shape",
            severity=2,
            code="empty_quadrants",
            description=(
                f"No claims in 'high credence / low robustness' or "
                f"'low credence / high robustness' quadrants (n={n}). "
                f"Ratings may track a single axis."
            ),
            suggested_action="inspect",
        ))

    for q in data.questions:
        cons = _consideration_links_to(data, q.id)
        if len(cons) < 4:
            continue
        directions: Counter[str] = Counter()
        for link in cons:
            d = link.direction.value if link.direction else "neutral"
            directions[d] += 1
        sup = directions.get("supports", 0)
        opp = directions.get("opposes", 0)
        if sup + opp >= 4 and (sup == 0 or opp == 0):
            missing = "opposing" if opp == 0 else "supporting"
            findings.append(Finding(
                category="rating_shape",
                severity=3,
                code="direction_imbalance",
                description=(
                    f"{short(q)} ({truncate(q.headline, 50)}) has "
                    f"{sup} supporting, {opp} opposing considerations — "
                    f"no {missing} views"
                ),
                page_ids=[q.id],
                suggested_action="dispatch find_considerations (look for counterarguments)",
            ))

    type_counts = Counter(p.page_type.value for p in data.pages.values())
    call_type_counts: dict[str, int] = {}
    summary_parts = [f"{n} rated claims, mean C{mean_c:.1f}/R{mean_r:.1f}"]
    PLURALS = {"summary": "summaries"}
    for ptype in ["claim", "question", "judgement", "source", "concept", "wiki", "summary", "view", "view_item", "view_meta"]:
        c = type_counts.get(ptype, 0)
        if c:
            summary_parts.append(f"{c} {PLURALS.get(ptype, ptype + 's')}")

    findings.append(Finding(
        category="rating_shape",
        severity=0,
        code="summary",
        description=", ".join(summary_parts),
    ))

    findings.sort(key=lambda f: f.severity, reverse=True)
    return findings


async def review_signals(db: DB, data: SubtreeData) -> list[Finding]:
    """Aggregate self-reported review_json signals from calls on this question."""
    findings: list[Finding] = []

    rows = await db._execute(
        db.client.table("calls")
        .select("id,call_type,review_json,scope_page_id")
        .eq("scope_page_id", data.root_id)
        .eq("status", "complete")
        .order("created_at", desc=True)
        .limit(30)
    )
    calls = list(getattr(rows, "data", None) or [])

    if not calls:
        return findings

    type_counts: Counter[str] = Counter()
    inadequate_context: list[dict[str, Any]] = []
    low_confidence: list[dict[str, Any]] = []
    all_missing: list[str] = []
    all_tensions: list[str] = []

    for c in calls:
        type_counts[c.get("call_type", "?")] += 1
        review = c.get("review_json") or {}
        if not isinstance(review, dict):
            continue

        if review.get("context_was_adequate") is False:
            inadequate_context.append(c)
            missing = review.get("what_was_missing", "")
            if missing:
                all_missing.append(f"{short(c['id'])} ({c.get('call_type', '?')}): {truncate(missing, 100)}")

        tensions = review.get("tensions_noticed", "")
        if tensions and tensions.strip():
            all_tensions.append(f"{short(c['id'])} ({c.get('call_type', '?')}): {truncate(tensions, 100)}")

        conf = review.get("confidence_in_output")
        if conf is not None and conf < 2:
            low_confidence.append(c)

    type_summary = ", ".join(f"{v} {k}" for k, v in type_counts.most_common())
    findings.append(Finding(
        category="review_signals",
        severity=0,
        code="call_type_mix",
        description=f"Call mix on this question: {type_summary}",
    ))

    if inadequate_context:
        frac = len(inadequate_context) / len(calls)
        findings.append(Finding(
            category="review_signals",
            severity=4 if frac > 0.5 else 3,
            code="inadequate_context",
            description=(
                f"{len(inadequate_context)}/{len(calls)} calls reported "
                f"inadequate context"
            ),
            page_ids=[c["id"] for c in inadequate_context],
            suggested_action="inspect context_builder or prompt",
        ))

    if all_missing:
        findings.append(Finding(
            category="review_signals",
            severity=3,
            code="what_was_missing",
            description="Calls reported missing context:\n" + "\n".join(
                f"  · {m}" for m in all_missing[:5]
            ),
            suggested_action="inspect context builder",
        ))

    if all_tensions:
        findings.append(Finding(
            category="review_signals",
            severity=2,
            code="unresolved_tensions",
            description="Calls flagged unresolved tensions:\n" + "\n".join(
                f"  · {t}" for t in all_tensions[:5]
            ),
            suggested_action="inspect — may warrant new considerations",
        ))

    if low_confidence:
        findings.append(Finding(
            category="review_signals",
            severity=3,
            code="low_confidence",
            description=(
                f"{len(low_confidence)} call(s) reported confidence < 2"
            ),
            page_ids=[c["id"] for c in low_confidence],
            suggested_action="inspect traces",
        ))

    findings.sort(key=lambda f: f.severity, reverse=True)
    return findings


async def scan_all(db: DB, question_id: str) -> tuple[SubtreeData, list[Finding]]:
    """Run all scan functions and return combined findings."""
    data = await collect_subtree(db, question_id)
    findings: list[Finding] = []
    findings.extend(graph_health(data))
    findings.extend(rating_shape(data))
    findings.extend(await review_signals(db, data))
    findings.sort(key=lambda f: f.severity, reverse=True)
    return data, findings


def format_findings(findings: list[Finding]) -> str:
    """Format findings for printing."""
    if not findings:
        return "(no findings)"
    lines: list[str] = []
    for f in findings:
        if f.severity == 0:
            lines.append(f"  {f.description}")
        else:
            ids = ", ".join(short(pid) for pid in f.page_ids[:4])
            id_part = f"  {ids}" if ids else ""
            lines.append(f"  [{f.severity}] {f.code}{id_part}")
            lines.append(f"      {f.description}")
            if f.suggested_action:
                lines.append(f"      -> {f.suggested_action}")
    return "\n".join(lines)


def format_compact(findings: list[Finding]) -> str:
    """One-line summary for show_question: counts + key stats."""
    actionable = [f for f in findings if f.severity > 0]
    summaries = [f for f in findings if f.code == "summary"]
    mix = [f for f in findings if f.code == "call_type_mix"]

    parts: list[str] = []
    if summaries:
        parts.append(summaries[0].description)
    if mix:
        parts.append(mix[0].description)
    if actionable:
        by_sev = Counter(f.severity for f in actionable)
        sev_parts = []
        for s in sorted(by_sev, reverse=True):
            sev_parts.append(f"{by_sev[s]}x s{s}")
        parts.append(f"findings: {', '.join(sev_parts)}")

    return " · ".join(parts) if parts else "(no data)"


async def main() -> None:
    parser = argparse.ArgumentParser(
        description="Structural and distributional health checks for a rumil question",
    )
    parser.add_argument("question_id", help="Full or short question ID")
    parser.add_argument("--workspace", default=None)
    parser.add_argument(
        "--checks",
        default="all",
        help="Comma-separated list of checks to run: graph, rating, review, all (default: all)",
    )
    args = parser.parse_args()

    db, ws = await make_db(workspace=args.workspace)
    try:
        full_id = await db.resolve_page_id(args.question_id)
        if not full_id:
            print(f"no question matching {args.question_id!r} in workspace {ws!r}")
            sys.exit(1)
        question = await db.get_page(full_id)
        if question is None:
            print(f"page {short(full_id)} vanished mid-lookup")
            sys.exit(1)

        print(f"workspace: {ws}")
        print(f"question:  {short(full_id)}  {truncate(question.headline, 80)}")
        print()

        checks = args.checks.split(",")
        data = await collect_subtree(db, full_id)
        all_findings: list[Finding] = []

        if "all" in checks or "graph" in checks:
            gh = graph_health(data)
            print(f"=== graph health ({len([f for f in gh if f.severity > 0])} findings) ===")
            print(format_findings(gh) if gh else "  (clean)")
            print()
            all_findings.extend(gh)

        if "all" in checks or "rating" in checks:
            rs = rating_shape(data)
            print(f"=== rating shape ({len([f for f in rs if f.severity > 0])} findings) ===")
            print(format_findings(rs) if rs else "  (clean)")
            print()
            all_findings.extend(rs)

        if "all" in checks or "review" in checks:
            rv = await review_signals(db, data)
            print(f"=== review signals ({len([f for f in rv if f.severity > 0])} findings) ===")
            print(format_findings(rv) if rv else "  (clean)")
            print()
            all_findings.extend(rv)

        actionable = [f for f in all_findings if f.severity > 0]
        print(f"total: {len(actionable)} actionable finding(s)")
    finally:
        await db.close()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        sys.exit(130)
