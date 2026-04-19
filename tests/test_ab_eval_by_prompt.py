"""Tests for ab_eval.by_prompt — pair selection + listing by prompt hash.

Does not invoke run_ab_eval (that's the LLM-driven comparison); only
pins the query + pairing logic.
"""

from __future__ import annotations

import pytest
import pytest_asyncio

from rumil.ab_eval.by_prompt import (
    PromptVersionPair,
    list_prompt_versions,
    select_pairs_by_prompt_hash,
)
from rumil.models import Call, CallStatus, CallType, Workspace

HASH_A = "a" * 64
HASH_B = "b" * 64


@pytest_asyncio.fixture
async def prompts_db(tmp_db):
    """tmp_db with a runs row + two prompt_versions rows."""
    await tmp_db.create_run(name="prompt-ab-test", question_id=None, config={})
    for h in (HASH_A, HASH_B):
        await tmp_db._execute(
            tmp_db.client.rpc(
                "upsert_prompt_version",
                {
                    "p_hash": h,
                    "p_name": "scout_analogies",
                    "p_content": f"content-for-{h[:4]}",
                    "p_kind": "composite",
                },
            )
        )
    return tmp_db


async def _make_call_with_prompt(db, question_id: str, prompt_hash: str) -> Call:
    """Create a call stamped with a prompt hash; call is tied to db.run_id for teardown."""
    call = Call(
        call_type=CallType.SCOUT_ANALOGIES,
        workspace=Workspace.RESEARCH,
        scope_page_id=question_id,
        status=CallStatus.COMPLETE,
    )
    call.project_id = db.project_id
    await db.save_call(call)
    await db._execute(
        db.client.table("calls")
        .update(
            {
                "primary_prompt_hash": prompt_hash,
                "primary_prompt_name": "scout_analogies",
            }
        )
        .eq("id", call.id)
    )
    return call


async def test_select_pairs_matches_on_question(prompts_db, question_page):
    await _make_call_with_prompt(prompts_db, question_page.id, HASH_A)
    await _make_call_with_prompt(prompts_db, question_page.id, HASH_B)

    pairs = await select_pairs_by_prompt_hash(
        prompts_db,
        prompt_name="scout_analogies",
        hash_a=HASH_A,
        hash_b=HASH_B,
        call_type="scout_analogies",
    )
    assert len(pairs) == 1
    assert isinstance(pairs[0], PromptVersionPair)
    assert pairs[0].question_id == question_page.id
    # Both calls share tmp_db's run_id in this setup — the pairing logic
    # still identifies them as a match on (question_id, hash). Cross-run
    # pairs are exercised by the underlying query shape; fixture cleanup
    # constraints keep everything on one run here.
    assert pairs[0].run_id_a == prompts_db.run_id
    assert pairs[0].run_id_b == prompts_db.run_id


async def test_select_pairs_skips_unmatched_questions(
    prompts_db, question_page, child_question_page
):
    await _make_call_with_prompt(prompts_db, question_page.id, HASH_A)
    await _make_call_with_prompt(prompts_db, child_question_page.id, HASH_B)

    pairs = await select_pairs_by_prompt_hash(
        prompts_db,
        prompt_name="scout_analogies",
        hash_a=HASH_A,
        hash_b=HASH_B,
    )
    assert pairs == []


async def test_select_pairs_rejects_identical_hashes(prompts_db):
    with pytest.raises(ValueError):
        await select_pairs_by_prompt_hash(
            prompts_db,
            prompt_name="x",
            hash_a=HASH_A,
            hash_b=HASH_A,
        )


async def test_select_pairs_rejects_empty_inputs(prompts_db):
    with pytest.raises(ValueError):
        await select_pairs_by_prompt_hash(
            prompts_db,
            prompt_name="",
            hash_a=HASH_A,
            hash_b=HASH_B,
        )


async def test_list_prompt_versions_filters_by_name(prompts_db):
    versions = await list_prompt_versions(prompts_db, prompt_name="scout_analogies")
    assert len(versions) == 2
    names = {v["name"] for v in versions}
    assert names == {"scout_analogies"}


async def test_list_prompt_versions_empty_on_unknown_name(prompts_db):
    versions = await list_prompt_versions(prompts_db, prompt_name="does-not-exist")
    assert versions == []
