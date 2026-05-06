"""Atlas text search.

Substring + word-boundary scoring over moves, dispatches, call types,
page types, prompt sections, and workflow stages. Cheap to compute on
each request — the registry is small and fits in memory.

Returns typed ``SearchHit``s with ``kind`` so the FE can route to the
right detail page on click.
"""

from __future__ import annotations

import re

from rumil.atlas.prompt_parts import get_prompt_sections
from rumil.atlas.registry import (
    build_call_type_summaries,
    build_dispatch_summaries,
    build_move_summaries,
    build_page_type_summaries,
    list_prompt_files,
)
from rumil.atlas.schemas import SearchHit, SearchResults
from rumil.atlas.workflows import all_profiles


def _score(haystack: str, query: str) -> float:
    if not haystack or not query:
        return 0.0
    h = haystack.lower()
    q = query.lower()
    if q == h:
        return 100.0
    if q in h:
        # word boundary boost
        pattern = re.compile(rf"\b{re.escape(q)}\b")
        if pattern.search(h):
            return 80.0
        return 60.0
    # term-level: every whitespace-split term must occur
    terms = [t for t in q.split() if t]
    if not terms:
        return 0.0
    if all(t in h for t in terms):
        return 40.0
    return 0.0


def _snippet(text: str, query: str, width: int = 160) -> str:
    if not text:
        return ""
    h = text.lower()
    q = query.lower()
    idx = h.find(q)
    if idx == -1:
        terms = [t for t in q.split() if t]
        for t in terms:
            i = h.find(t)
            if i != -1:
                idx = i
                break
    if idx == -1:
        return text[:width].strip()
    start = max(0, idx - width // 3)
    end = min(len(text), idx + width)
    prefix = "…" if start > 0 else ""
    suffix = "…" if end < len(text) else ""
    return f"{prefix}{text[start:end].strip()}{suffix}"


def _best(*candidates: tuple[str, float]) -> float:
    return max((s for _, s in candidates), default=0.0)


def search_atlas(query: str, limit: int = 50) -> SearchResults:
    q = (query or "").strip()
    hits: list[SearchHit] = []
    if not q:
        return SearchResults(query="", hits=[], total=0, by_kind={})

    for m in build_move_summaries():
        title_score = _score(m.name, q) + _score(m.move_type, q) * 0.9
        desc_score = _score(m.description, q) * 0.8
        field_text = "\n".join(f"{f.name}: {f.description}" for f in m.fields)
        field_score = _score(field_text, q) * 0.6
        score = title_score + desc_score + field_score
        if score > 0:
            hits.append(
                SearchHit(
                    kind="move",
                    id=m.move_type,
                    title=f"{m.name}",
                    snippet=_snippet(m.description or field_text, q),
                    score=score,
                    href=f"/atlas/moves/{m.move_type}",
                )
            )

    for d in build_dispatch_summaries():
        title_score = _score(d.name, q) + _score(d.call_type, q) * 0.9
        desc_score = _score(d.description, q) * 0.8
        field_text = "\n".join(f"{f.name}: {f.description}" for f in d.fields)
        field_score = _score(field_text, q) * 0.6
        score = title_score + desc_score + field_score
        if score > 0:
            hits.append(
                SearchHit(
                    kind="dispatch",
                    id=d.call_type,
                    title=d.name,
                    snippet=_snippet(d.description or field_text, q),
                    score=score,
                    href=f"/atlas/dispatches/{d.call_type}",
                )
            )

    for c in build_call_type_summaries():
        score = (
            _score(c.call_type, q) * 1.0
            + _score(c.description, q) * 0.8
            + _score(c.runner_class or "", q) * 0.5
        )
        if score > 0:
            hits.append(
                SearchHit(
                    kind="call",
                    id=c.call_type,
                    title=c.call_type,
                    snippet=_snippet(c.description, q),
                    score=score,
                    href=f"/atlas/calls/{c.call_type}",
                )
            )

    for p in build_page_type_summaries():
        score = _score(p.page_type, q) * 1.0 + _score(p.description, q) * 0.8
        if score > 0:
            hits.append(
                SearchHit(
                    kind="page",
                    id=p.page_type,
                    title=p.page_type,
                    snippet=_snippet(p.description, q),
                    score=score,
                    href=f"/atlas/pages/{p.page_type}",
                )
            )

    for profile in all_profiles():
        score = (
            _score(profile.name, q) * 1.0
            + _score(profile.summary, q) * 0.8
            + _score(profile.kind, q) * 0.4
        )
        if score > 0:
            hits.append(
                SearchHit(
                    kind="workflow",
                    id=profile.name,
                    title=profile.name,
                    snippet=_snippet(profile.summary, q),
                    score=score,
                    href=f"/atlas/workflows/{profile.name}",
                )
            )
        for stage in profile.stages:
            stage_text = f"{stage.label} {stage.description} {stage.note or ''}"
            s = _score(stage_text, q) * 0.7 + _score(stage.id, q) * 0.5
            if s > 0:
                hits.append(
                    SearchHit(
                        kind="stage",
                        id=f"{profile.name}:{stage.id}",
                        title=f"{profile.name} · {stage.label}",
                        snippet=_snippet(stage.description or stage.label, q),
                        score=s,
                        href=f"/atlas/workflows/{profile.name}#stage-{stage.id}",
                    )
                )

    for fname in list_prompt_files():
        sections = get_prompt_sections(fname)
        for section in sections:
            score = _score(section.title, q) * 1.0 + _score(section.body, q) * 0.5
            if score > 0:
                hits.append(
                    SearchHit(
                        kind="prompt_section",
                        id=f"{fname}#{section.anchor}",
                        title=f"{fname} · {section.title}",
                        snippet=_snippet(section.body, q),
                        score=score,
                        href=f"/atlas/prompts/{fname}#{section.anchor}",
                    )
                )

    hits.sort(key=lambda h: h.score, reverse=True)
    by_kind: dict[str, int] = {}
    for h in hits:
        by_kind[h.kind] = by_kind.get(h.kind, 0) + 1
    return SearchResults(
        query=q,
        hits=hits[:limit],
        total=len(hits),
        by_kind=by_kind,
    )
