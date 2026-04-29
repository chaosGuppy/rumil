"""Postgres storage for versus eval data — replaces the JSONL files.

Two tables, both content-aware via generated hash columns:

- ``versus_texts``: any essay-shaped text (human, completion, paraphrase) plus
  the conditions that produced it. ``request`` and ``response`` hold the raw
  provider-shaped JSON; ``request_hash`` is generated.
- ``versus_judgments``: one pairwise verdict. ``judge_inputs`` is the canonical
  condition blob; ``judge_inputs_hash`` is generated. ``project_id`` and
  ``run_id`` are populated only for ws/orch judgments produced inside a rumil
  run.

No DB-level uniqueness on either table. "Skip if exists" semantics live in
the runner — call ``find_texts`` / ``find_judgments`` and decide whether to
insert. This keeps temperature>0 replicates first-class.
"""

from __future__ import annotations

import json
from collections.abc import Iterator, Mapping
from typing import Any, Literal

from rumil.settings import get_settings
from supabase import Client, create_client

TextKind = Literal["human", "completion"]
JudgmentVariant = Literal["blind", "ws", "orch"]
Verdict = Literal["A", "B", "tie"]


def get_client(*, prod: bool = False) -> Client:
    url, key = get_settings().get_supabase_credentials(prod)
    return create_client(url, key)


def insert_text(
    client: Client,
    *,
    essay_id: str,
    kind: TextKind,
    source_id: str,
    text: str,
    prefix_hash: str | None = None,
    model_id: str | None = None,
    request: Mapping[str, Any] | None = None,
    response: Mapping[str, Any] | None = None,
    params: Mapping[str, Any] | None = None,
) -> str:
    row = {
        "essay_id": essay_id,
        "kind": kind,
        "source_id": source_id,
        "text": text,
        "prefix_hash": prefix_hash,
        "model_id": model_id,
        "request": dict(request) if request is not None else None,
        "response": dict(response) if response is not None else None,
        "params": dict(params) if params is not None else {},
    }
    resp = client.table("versus_texts").insert(row).execute()
    return resp.data[0]["id"]


def insert_judgment(
    client: Client,
    *,
    essay_id: str,
    prefix_hash: str,
    source_a: str,
    source_b: str,
    display_first: str,
    text_a_id: str,
    text_b_id: str,
    criterion: str,
    variant: JudgmentVariant,
    judge_model: str,
    judge_inputs: Mapping[str, Any],
    verdict: Verdict,
    reasoning_text: str,
    request: Mapping[str, Any] | None = None,
    response: Mapping[str, Any] | None = None,
    preference_label: str | None = None,
    duration_s: float | None = None,
    project_id: str | None = None,
    run_id: str | None = None,
    rumil_call_id: str | None = None,
    contamination_note: str | None = None,
) -> str:
    if source_a > source_b:
        raise ValueError(
            f"source_a must be <= source_b alphabetically; got {source_a!r} > {source_b!r}"
        )
    row = {
        "essay_id": essay_id,
        "prefix_hash": prefix_hash,
        "source_a": source_a,
        "source_b": source_b,
        "display_first": display_first,
        "text_a_id": text_a_id,
        "text_b_id": text_b_id,
        "criterion": criterion,
        "variant": variant,
        "judge_model": judge_model,
        "judge_inputs": dict(judge_inputs),
        "verdict": verdict,
        "reasoning_text": reasoning_text,
        "request": dict(request) if request is not None else None,
        "response": dict(response) if response is not None else None,
        "preference_label": preference_label,
        "duration_s": duration_s,
        "project_id": project_id,
        "run_id": run_id,
        "rumil_call_id": rumil_call_id,
        "contamination_note": contamination_note,
    }
    resp = client.table("versus_judgments").insert(row).execute()
    return resp.data[0]["id"]


def find_texts(
    client: Client,
    *,
    essay_id: str,
    kind: TextKind | None = None,
    source_id: str | None = None,
    prefix_hash: str | None = None,
    request_hash: str | None = None,
) -> list[dict]:
    """Return matching text rows. Use to check "is there one already?" before insert.

    All filters are AND-ed. Pass `request_hash` to ask "have we seen this exact
    rendered request before?"; omit it to find any row matching the natural key.
    """
    q = client.table("versus_texts").select("*").eq("essay_id", essay_id)
    if kind is not None:
        q = q.eq("kind", kind)
    if source_id is not None:
        q = q.eq("source_id", source_id)
    if prefix_hash is not None:
        q = q.eq("prefix_hash", prefix_hash)
    if request_hash is not None:
        q = q.eq("request_hash", request_hash)
    return q.execute().data


def find_judgments(
    client: Client,
    *,
    essay_id: str,
    prefix_hash: str | None = None,
    source_a: str | None = None,
    source_b: str | None = None,
    criterion: str | None = None,
    judge_inputs_hash: str | None = None,
    project_id: str | None = None,
    run_id: str | None = None,
) -> list[dict]:
    q = client.table("versus_judgments").select("*").eq("essay_id", essay_id)
    if prefix_hash is not None:
        q = q.eq("prefix_hash", prefix_hash)
    if source_a is not None:
        q = q.eq("source_a", source_a)
    if source_b is not None:
        q = q.eq("source_b", source_b)
    if criterion is not None:
        q = q.eq("criterion", criterion)
    if judge_inputs_hash is not None:
        q = q.eq("judge_inputs_hash", judge_inputs_hash)
    if project_id is not None:
        q = q.eq("project_id", project_id)
    if run_id is not None:
        q = q.eq("run_id", run_id)
    return q.execute().data


# Columns to skip when callers don't need the full provider request/response
# blobs. These are 90+% of the row payload — for /versus/results aggregation
# we don't need them and shipping all of them adds tens of MB to the response.
_TEXT_LIGHT_SELECT = (
    "id,essay_id,kind,source_id,prefix_hash,model_id,text,params,request_hash,created_at"
)
_JUDGMENT_LIGHT_SELECT = (
    "id,essay_id,prefix_hash,source_a,source_b,display_first,text_a_id,text_b_id,"
    "criterion,variant,judge_model,judge_inputs,judge_inputs_hash,verdict,winner_source,"
    "preference_label,reasoning_text,duration_s,project_id,run_id,rumil_call_id,"
    "contamination_note,created_at"
)


def iter_texts(
    client: Client,
    *,
    essay_id: str | None = None,
    kind: TextKind | None = None,
    page_size: int = 1000,
    light: bool = False,
) -> Iterator[dict]:
    """Iterate versus_texts rows.

    Pass ``light=True`` to skip the heavy ``request`` / ``response`` JSONB
    columns — useful for aggregation paths that just need essay/source/text.
    """
    select = _TEXT_LIGHT_SELECT if light else "*"
    offset = 0
    while True:
        q = client.table("versus_texts").select(select)
        if essay_id is not None:
            q = q.eq("essay_id", essay_id)
        if kind is not None:
            q = q.eq("kind", kind)
        rows = q.order("created_at").range(offset, offset + page_size - 1).execute().data
        if not rows:
            return
        yield from rows
        if len(rows) < page_size:
            return
        offset += page_size


def iter_judgments(
    client: Client,
    *,
    essay_id: str | None = None,
    project_id: str | None = None,
    run_id: str | None = None,
    page_size: int = 1000,
    light: bool = False,
) -> Iterator[dict]:
    """Iterate versus_judgments rows.

    Pass ``light=True`` to skip the heavy ``request`` / ``response`` JSONB
    columns. The aggregation paths in /versus/results don't read those —
    only the by-key inspector and the trace links do — and skipping them
    cuts response payload by ~95%.
    """
    select = _JUDGMENT_LIGHT_SELECT if light else "*"
    offset = 0
    while True:
        q = client.table("versus_judgments").select(select)
        if essay_id is not None:
            q = q.eq("essay_id", essay_id)
        if project_id is not None:
            q = q.eq("project_id", project_id)
        if run_id is not None:
            q = q.eq("run_id", run_id)
        rows = q.order("created_at").range(offset, offset + page_size - 1).execute().data
        if not rows:
            return
        yield from rows
        if len(rows) < page_size:
            return
        offset += page_size


def compute_canonical_hash(payload: Mapping[str, Any]) -> str:
    """Compute the same hash a generated jsonb column would produce.

    Used in the runner for "have we seen this exact config?" lookups
    without round-tripping through the DB — works for either the
    ``request`` column (request_hash) or ``judge_inputs`` (judge_inputs_hash).
    Mirrors the SQL expression ``encode(digest(<col>::text, 'sha256'), 'hex')``:
    jsonb canonicalizes keys alphabetically, so we serialize with
    ``sort_keys=True`` and Postgres's text-form spacing.
    """
    import hashlib

    canonical = json.dumps(payload, sort_keys=True, separators=(", ", ": "), ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()
