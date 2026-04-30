"""Round-trip tests for versus.versus_db against local Supabase.

Pins:
- Insert/find/iter for texts, judgments, essays (sync supabase client).
- Generated columns (request_hash, judge_inputs_hash, winner_source,
  response_words, content_hash) populate from the column inputs.
- compute_canonical_hash agrees with Postgres's digest() for the same
  jsonb shapes — drift here breaks the runner-side dedup story.

Each test scopes its rows under an ``__test_*`` essay_id prefix; the
fixture deletes those rows on teardown so the suite is idempotent and
runnable in any order.
"""

from __future__ import annotations

import pytest

from versus import versus_db


@pytest.fixture
def versus_client():
    client = versus_db.get_client()
    yield client
    client.table("versus_judgments").delete().like("essay_id", "__test_%").execute()
    client.table("versus_texts").delete().like("essay_id", "__test_%").execute()
    client.table("versus_essays").delete().like("id", "__test_%").execute()


def _row_by_id(client, table: str, row_id: str) -> dict:
    rows = client.table(table).select("*").eq("id", row_id).execute().data
    assert len(rows) == 1
    return rows[0]


def test_insert_text_human_has_null_request_hash(versus_client):
    text_id = versus_db.insert_text(
        versus_client,
        essay_id="__test_human",
        kind="human",
        source_id="human",
        text="held-out remainder",
        prefix_hash="pfx",
    )
    row = _row_by_id(versus_client, "versus_texts", text_id)
    assert row["request_hash"] is None
    assert row["request"] is None
    assert row["text"] == "held-out remainder"


def test_insert_text_completion_request_hash_matches_python(versus_client):
    request = {
        "model": "google/gemini-3-flash-preview",
        "messages": [{"role": "user", "content": "continue this..."}],
        "temperature": 0.7,
    }
    text_id = versus_db.insert_text(
        versus_client,
        essay_id="__test_completion",
        kind="completion",
        source_id="google/gemini-3-flash-preview",
        text="model continuation here",
        prefix_hash="pfx",
        model_id="google/gemini-3-flash-preview",
        request=request,
    )
    row = _row_by_id(versus_client, "versus_texts", text_id)
    assert row["request_hash"] == versus_db.compute_canonical_hash(request)


def test_insert_text_replicates_at_same_config_coexist(versus_client):
    request = {"model": "m", "messages": [{"role": "user", "content": "go"}]}
    a = versus_db.insert_text(
        versus_client,
        essay_id="__test_replicate",
        kind="completion",
        source_id="m",
        text="sample one",
        prefix_hash="pfx",
        model_id="m",
        request=request,
    )
    b = versus_db.insert_text(
        versus_client,
        essay_id="__test_replicate",
        kind="completion",
        source_id="m",
        text="sample two",
        prefix_hash="pfx",
        model_id="m",
        request=request,
    )
    rows = versus_db.find_texts(
        versus_client,
        essay_id="__test_replicate",
        source_id="m",
    )
    assert {r["id"] for r in rows} == {a, b}
    assert {r["request_hash"] for r in rows} == {versus_db.compute_canonical_hash(request)}


def test_find_texts_by_request_hash_filters_to_matching(versus_client):
    req_a = {"model": "m", "messages": [{"role": "user", "content": "a"}]}
    req_b = {"model": "m", "messages": [{"role": "user", "content": "b"}]}
    versus_db.insert_text(
        versus_client,
        essay_id="__test_findhash",
        kind="completion",
        source_id="m",
        text="x",
        prefix_hash="pfx",
        model_id="m",
        request=req_a,
    )
    versus_db.insert_text(
        versus_client,
        essay_id="__test_findhash",
        kind="completion",
        source_id="m",
        text="y",
        prefix_hash="pfx",
        model_id="m",
        request=req_b,
    )
    matches = versus_db.find_texts(
        versus_client,
        essay_id="__test_findhash",
        request_hash=versus_db.compute_canonical_hash(req_a),
    )
    assert len(matches) == 1
    assert matches[0]["text"] == "x"


def _seed_text_pair(client, essay_id: str) -> tuple[str, str]:
    a = versus_db.insert_text(
        client,
        essay_id=essay_id,
        kind="human",
        source_id="human",
        text="human text",
        prefix_hash="pfx",
    )
    b = versus_db.insert_text(
        client,
        essay_id=essay_id,
        kind="completion",
        source_id="m",
        text="model text",
        prefix_hash="pfx",
        model_id="m",
        request={"model": "m", "messages": [{"role": "user", "content": "go"}]},
    )
    return a, b


def test_insert_judgment_resolves_text_fks(versus_client):
    text_a, text_b = _seed_text_pair(versus_client, "__test_judg_fk")
    jid = versus_db.insert_judgment(
        versus_client,
        essay_id="__test_judg_fk",
        prefix_hash="pfx",
        source_a="human",
        source_b="m",
        display_first="human",
        text_a_id=text_a,
        text_b_id=text_b,
        criterion="general_quality",
        variant="blind",
        judge_model="blind:m:general_quality:c00000000",
        judge_inputs={"model": "m", "variant": "blind", "order": "ab"},
        verdict="A",
        reasoning_text="...",
    )
    row = _row_by_id(versus_client, "versus_judgments", jid)
    assert row["text_a_id"] == text_a
    assert row["text_b_id"] == text_b


def test_insert_judgment_judge_inputs_hash_matches_python(versus_client):
    text_a, text_b = _seed_text_pair(versus_client, "__test_judg_hash")
    judge_inputs = {"model": "m", "variant": "blind", "order": "ab", "extra": {"k": [1, 2]}}
    jid = versus_db.insert_judgment(
        versus_client,
        essay_id="__test_judg_hash",
        prefix_hash="pfx",
        source_a="human",
        source_b="m",
        display_first="human",
        text_a_id=text_a,
        text_b_id=text_b,
        criterion="general_quality",
        variant="blind",
        judge_model="blind:m:general_quality:c00000000",
        judge_inputs=judge_inputs,
        verdict="A",
        reasoning_text="...",
    )
    row = _row_by_id(versus_client, "versus_judgments", jid)
    assert row["judge_inputs_hash"] == versus_db.compute_canonical_hash(judge_inputs)


@pytest.mark.parametrize(
    ("verdict", "display_first", "expected_winner"),
    [
        ("A", "human", "human"),
        ("A", "m", "m"),
        ("B", "human", "m"),
        ("B", "m", "human"),
        ("tie", "human", "tie"),
        ("tie", "m", "tie"),
        (None, "human", None),
    ],
)
def test_winner_source_generated_column(versus_client, verdict, display_first, expected_winner):
    text_a, text_b = _seed_text_pair(versus_client, "__test_winner")
    jid = versus_db.insert_judgment(
        versus_client,
        essay_id="__test_winner",
        prefix_hash="pfx",
        source_a="human",
        source_b="m",
        display_first=display_first,
        text_a_id=text_a,
        text_b_id=text_b,
        criterion="general_quality",
        variant="blind",
        judge_model="blind:m:general_quality:c00000000",
        judge_inputs={"model": "m"},
        verdict=verdict,  # pyright: ignore[reportArgumentType]
        reasoning_text="...",
    )
    row = _row_by_id(versus_client, "versus_judgments", jid)
    assert row["winner_source"] == expected_winner


def test_upsert_essay_inserts_then_idempotent_on_id(versus_client):
    versus_db.upsert_essay(
        versus_client,
        id="__test_essay_idem",
        source_id="redwood",
        url="https://example.com/x",
        title="A title",
        author="Someone",
        pub_date="2026-01-01",
        blocks=[{"type": "p", "text": "first"}],
        markdown="first paragraph",
        schema_version=11,
    )
    versus_db.upsert_essay(
        versus_client,
        id="__test_essay_idem",
        source_id="redwood",
        url="https://example.com/x",
        title="A title (revised)",
        author="Someone",
        pub_date="2026-01-01",
        blocks=[{"type": "p", "text": "first"}, {"type": "p", "text": "second"}],
        markdown="first paragraph\n\nsecond paragraph",
        schema_version=11,
    )
    rows = (
        versus_client.table("versus_essays")
        .select("*")
        .eq("id", "__test_essay_idem")
        .execute()
        .data
    )
    assert len(rows) == 1
    assert rows[0]["title"] == "A title (revised)"
    assert len(rows[0]["blocks"]) == 2


def test_upsert_essay_content_hash_generated_from_markdown(versus_client):
    versus_db.upsert_essay(
        versus_client,
        id="__test_essay_hash",
        source_id="redwood",
        url="x",
        title="t",
        author="a",
        pub_date="2026-01-01",
        blocks=[],
        markdown="exact markdown body",
        schema_version=11,
    )
    row = versus_db.get_essay(versus_client, "__test_essay_hash")
    assert row is not None
    assert isinstance(row["content_hash"], str)
    assert len(row["content_hash"]) == 64

    versus_db.upsert_essay(
        versus_client,
        id="__test_essay_hash",
        source_id="redwood",
        url="x",
        title="t",
        author="a",
        pub_date="2026-01-01",
        blocks=[],
        markdown="exact markdown body",
        schema_version=11,
    )
    row2 = versus_db.get_essay(versus_client, "__test_essay_hash")
    assert row2 is not None
    assert row2["content_hash"] == row["content_hash"]

    versus_db.upsert_essay(
        versus_client,
        id="__test_essay_hash",
        source_id="redwood",
        url="x",
        title="t",
        author="a",
        pub_date="2026-01-01",
        blocks=[],
        markdown="different body",
        schema_version=11,
    )
    row3 = versus_db.get_essay(versus_client, "__test_essay_hash")
    assert row3 is not None
    assert row3["content_hash"] != row["content_hash"]


def test_iter_essays_returns_id_sorted(versus_client):
    for eid in ["__test_iter_c", "__test_iter_a", "__test_iter_b"]:
        versus_db.upsert_essay(
            versus_client,
            id=eid,
            source_id="redwood",
            url="x",
            title="t",
            author="a",
            pub_date="2026-01-01",
            blocks=[],
            markdown="body",
            schema_version=11,
        )
    seen_ids = [
        r["id"] for r in versus_db.iter_essays(versus_client) if r["id"].startswith("__test_iter_")
    ]
    assert seen_ids == ["__test_iter_a", "__test_iter_b", "__test_iter_c"]


def test_upsert_essay_verdict_only_touches_verdict_columns(versus_client):
    versus_db.upsert_essay(
        versus_client,
        id="__test_essay_verdict",
        source_id="redwood",
        url="x",
        title="original-title",
        author="a",
        pub_date="2026-01-01",
        blocks=[{"type": "p", "text": "p"}],
        markdown="body",
        schema_version=11,
    )
    versus_db.upsert_essay_verdict(
        versus_client,
        essay_id="__test_essay_verdict",
        clean=True,
        issues=[],
        model="claude-sonnet-4-6",
        validator_version=1,
        request={"model": "claude-sonnet-4-6", "messages": []},
        response={"id": "msg_x"},
    )
    row = versus_db.get_essay(versus_client, "__test_essay_verdict")
    assert row is not None
    assert row["title"] == "original-title"
    assert row["verdict_clean"] is True
    assert row["verdict_model"] == "claude-sonnet-4-6"
    assert row["verdict_version"] == 1
    assert row["verdict_request"] == {"model": "claude-sonnet-4-6", "messages": []}
    assert row["verdict_response"] == {"id": "msg_x"}
    assert row["verdict_at"] is not None


@pytest.mark.parametrize(
    "payload",
    [
        {"a": 1, "b": 2},
        {"nested": {"x": [1, 2, 3], "y": {"z": True}}},
        {"unicode": "café — résumé", "emoji": "🌶️"},
        {"mixed": [1, "two", 3.14, None, {"k": "v"}]},
        {},
    ],
)
def test_compute_canonical_hash_matches_db_digest(versus_client, payload):
    text_id = versus_db.insert_text(
        versus_client,
        essay_id="__test_canonical",
        kind="completion",
        source_id="m",
        text="x",
        prefix_hash="pfx",
        model_id="m",
        request=payload,
    )
    row = _row_by_id(versus_client, "versus_texts", text_id)
    assert row["request_hash"] == versus_db.compute_canonical_hash(payload)


@pytest.mark.parametrize(
    ("text", "expected_words"),
    [
        ("one two three", 3),
        ("  leading and trailing  ", 3),
        ("collapsed   spaces\ttabs\nnewlines", 4),
        ("", 0),
        ("single", 1),
    ],
)
def test_response_words_generated_column(versus_client, text, expected_words):
    text_id = versus_db.insert_text(
        versus_client,
        essay_id="__test_words",
        kind="human",
        source_id="human",
        text=text,
        prefix_hash="pfx",
    )
    row = _row_by_id(versus_client, "versus_texts", text_id)
    assert row["response_words"] == expected_words
