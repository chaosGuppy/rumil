"""Backfill versus_texts and versus_judgments from the JSONL files.

Idempotent: if either table already has rows, exits unless ``--force`` is
passed (which deletes everything and re-imports). Run with:

    uv run --with versus --with rumil python versus/scripts/backfill_db.py

Strategy:
  1. Texts come from completions.jsonl (kind=human|completion|paraphrase
     based on source_kind) and paraphrases.jsonl (kind=paraphrase).
  2. For completion/paraphrase rows we synthesize a provider-shaped request
     from prompt + params so request_hash populates. The synthesized request
     is a faithful description of the *condition* but won't byte-equal the
     literal request that was sent — fine, since future runs will render
     fresh requests under the new code path.
  3. Judgments resolve text_a_id / text_b_id by looking up the (essay_id,
     source_id) pair in versus_texts. Source ids in the JSONL judgment row
     come from `source_a` / `source_b` (alphabetically sorted).
"""

from __future__ import annotations

import argparse
import json
import pathlib
import sys
import time
from collections import defaultdict
from typing import Any

from versus.versus_db import (
    get_client,
    insert_judgment,
    insert_text,
    upsert_essay,
    upsert_essay_verdict,
)

DATA_DIR = pathlib.Path(__file__).resolve().parent.parent / "data"
COMPLETIONS = DATA_DIR / "completions.jsonl"
JUDGMENTS = DATA_DIR / "judgments.jsonl"
ESSAYS_DIR = DATA_DIR / "essays"


def _load_jsonl(path: pathlib.Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def import_essays(client) -> None:
    """Import data/essays/<id>.json + <id>.verdict.json into versus_essays.

    Idempotent (upsert by id). Verdict columns get populated when the
    sibling .verdict.json exists; the JSONL/file cache only has clean +
    issues + model + validator_version, not the raw request/response, so
    those stay null on backfilled rows. Future re-validations under the
    new code path will fill them.

    raw_html is intentionally not loaded — backfill stays bytes-light and
    the fetcher will populate raw_html on the next refresh.
    """
    from versus import essay as versus_essay

    if not ESSAYS_DIR.exists():
        print("Skipping essays import: data/essays/ doesn't exist.")
        return
    essay_paths = sorted(
        p for p in ESSAYS_DIR.glob("*.json") if not p.name.endswith(".verdict.json")
    )
    print(f"Importing {len(essay_paths)} essay rows...")
    n_essays = 0
    n_verdicts = 0
    n_skipped = 0
    n_stale_schema = 0
    for p in essay_paths:
        try:
            d = json.loads(p.read_text())
        except json.JSONDecodeError as e:
            print(f"  skipped malformed essay {p.name}: {e}")
            n_skipped += 1
            continue
        if "source_id" not in d or "id" not in d:
            print(f"  skipped pre-multi-source essay {p.name}")
            n_skipped += 1
            continue
        if not versus_essay.is_current_schema(d):
            # Old-schema essays are duplicates of their namespaced replacements
            # (``ai-for-decision-advice`` vs ``forethought__ai-for-...``) and
            # aren't eval-relevant — the router already filters them via
            # is_current_schema.
            n_stale_schema += 1
            continue
        try:
            upsert_essay(
                client,
                id=d["id"],
                source_id=d["source_id"],
                url=d.get("url", ""),
                title=d.get("title", ""),
                author=d.get("author", ""),
                pub_date=d.get("pub_date", ""),
                blocks=d.get("blocks", []),
                markdown=d.get("markdown", ""),
                schema_version=d.get("schema_version", 0),
                image_count=d.get("image_count", 0),
                raw_html=None,
            )
            n_essays += 1
        except Exception as e:
            print(f"  skipped essay upsert {d.get('id')}: {e}")
            n_skipped += 1
            continue

        verdict_path = ESSAYS_DIR / f"{d['id']}.verdict.json"
        if verdict_path.exists():
            try:
                v = json.loads(verdict_path.read_text())
                upsert_essay_verdict(
                    client,
                    essay_id=d["id"],
                    clean=bool(v["clean"]),
                    issues=v.get("issues", []),
                    model=v.get("model", "unknown"),
                    validator_version=v.get("validator_version", 0),
                    request=None,
                    response=None,
                )
                n_verdicts += 1
            except Exception as e:
                print(f"  skipped verdict for {d['id']}: {e}")
    print(
        f"  done. {n_essays} essays, {n_verdicts} verdicts, "
        f"{n_skipped} skipped, {n_stale_schema} stale-schema duplicates skipped."
    )


def _synthesize_completion_request(row: dict) -> dict[str, Any] | None:
    """Build a provider-shaped request dict from a completion JSONL row.

    Returns None for human / paraphrase-derived rows that have no model call.
    The resulting object is what the row's ``request_hash`` is computed over.
    """
    if not row.get("prompt"):
        return None
    params: dict[str, Any] = dict(row.get("params") or {})
    request: dict[str, Any] = {
        "model": row["model_id"],
        "messages": [{"role": "user", "content": row["prompt"]}],
    }
    request.update({k: v for k, v in params.items() if v is not None})
    return request


# Lookup key on (essay, source, prefix). Replicates (temperature>0 sampling)
# legitimately produce multiple text rows under the same key — we keep all of
# them so import_judgments can flag the ambiguity instead of silently picking
# the last-inserted one.
TextKey = tuple[str, str, str | None]


def import_texts(client) -> dict[TextKey, list[str]]:
    """Insert texts and return ``{(essay_id, source_id, prefix_hash): [text_id, ...]}``.

    Paraphrase generation is deferred — paraphrase-file rows and
    paraphrase-derived completion rows are skipped. The JSONL archive still
    holds them if we want to re-import once the paraphrase model is settled.
    """
    lookup: dict[TextKey, list[str]] = defaultdict(list)
    completion_rows = _load_jsonl(COMPLETIONS)

    print(f"Importing {len(completion_rows)} completion-file rows (skipping paraphrase-derived)...")
    n_skipped = 0
    n_skipped_paraphrase = 0
    for i, row in enumerate(completion_rows, 1):
        if row["source_kind"] == "paraphrase":
            n_skipped_paraphrase += 1
            continue
        kind = row["source_kind"]
        request = _synthesize_completion_request(row)
        try:
            text_id = insert_text(
                client,
                essay_id=row["essay_id"],
                kind=kind,
                source_id=row["source_id"],
                text=row["response_text"],
                prefix_hash=row.get("prefix_config_hash"),
                model_id=row.get("model_id") if kind != "human" else None,
                request=request,
                response=row.get("raw_response"),
                params=row.get("params") or {},
            )
        except Exception as e:
            print(f"  skipped completion row {i} ({row.get('key')}): {e}")
            n_skipped += 1
            continue
        lookup[(row["essay_id"], row["source_id"], row.get("prefix_config_hash"))].append(text_id)
        if i % 100 == 0:
            print(f"  ... {i}/{len(completion_rows)}")
    n_dup_keys = sum(1 for v in lookup.values() if len(v) > 1)
    print(
        f"  done. Skipped {n_skipped} (errors), {n_skipped_paraphrase} (paraphrase-derived). "
        f"{n_dup_keys} keys have replicates."
    )
    return lookup


def import_judgments(client, text_lookup: dict[TextKey, list[str]]) -> None:
    rows = _load_jsonl(JUDGMENTS)
    print(f"Importing {len(rows)} judgment rows (skipping paraphrase-touching)...")
    missing_text: dict[TextKey, int] = defaultdict(int)
    ambiguous_text: dict[TextKey, int] = defaultdict(int)
    n_skipped = 0
    n_skipped_paraphrase = 0
    n_inserted = 0
    for i, row in enumerate(rows, 1):
        if "paraphrase:" in row["source_a"] or "paraphrase:" in row["source_b"]:
            n_skipped_paraphrase += 1
            continue
        config = row.get("config") or {}
        variant = config.get("variant", "blind")
        sa, sb = sorted([row["source_a"], row["source_b"]])
        prefix = row.get("prefix_config_hash")
        if prefix is None:
            n_skipped += 1
            continue
        ta_key: TextKey = (row["essay_id"], sa, prefix)
        tb_key: TextKey = (row["essay_id"], sb, prefix)
        ta_ids = text_lookup.get(ta_key) or []
        tb_ids = text_lookup.get(tb_key) or []
        if not ta_ids or not tb_ids:
            missing_text[ta_key if not ta_ids else tb_key] += 1
            n_skipped += 1
            continue
        # JSONL judgment rows don't record which specific replicate they
        # were judged against. Pick the first inserted text deterministically
        # (insert order = JSONL order) and tally the ambiguity for the summary.
        if len(ta_ids) > 1:
            ambiguous_text[ta_key] += 1
        if len(tb_ids) > 1:
            ambiguous_text[tb_key] += 1
        text_a_id = ta_ids[0]
        text_b_id = tb_ids[0]

        # judge_inputs = the existing config dict, plus text id refs.
        judge_inputs = dict(config)
        judge_inputs["text_a_id"] = text_a_id
        judge_inputs["text_b_id"] = text_b_id

        # judge_model: keep historical compound string for ws/orch; underlying
        # model is captured in judge_inputs['model'].
        judge_model = row.get("judge_model") or config.get("model") or "unknown"

        try:
            insert_judgment(
                client,
                essay_id=row["essay_id"],
                prefix_hash=prefix,
                source_a=sa,
                source_b=sb,
                display_first=row.get("display_first") or sa,
                text_a_id=text_a_id,
                text_b_id=text_b_id,
                criterion=row["criterion"],
                variant=variant,
                judge_model=judge_model,
                judge_inputs=judge_inputs,
                verdict=row["verdict"],
                reasoning_text=row.get("reasoning_text") or "",
                request=None,  # original request not preserved on JSONL row
                response=row.get("raw_response"),
                preference_label=row.get("rumil_preference_label"),
                duration_s=row.get("duration_s"),
                project_id=None,  # project lookup not attempted; runs may be pruned
                run_id=row.get("rumil_run_id"),
                rumil_call_id=row.get("rumil_call_id"),
                rumil_question_id=row.get("rumil_question_id"),
                rumil_cost_usd=row.get("rumil_cost_usd"),
                contamination_note=row.get("contamination_note"),
            )
            n_inserted += 1
        except Exception as e:
            print(f"  skipped judgment row {i} ({row.get('key')}): {e}")
            n_skipped += 1
            continue
        if i % 200 == 0:
            print(f"  ... {i}/{len(rows)} (inserted={n_inserted})")
    print(
        f"  done. Inserted {n_inserted}, skipped {n_skipped} (errors), "
        f"{n_skipped_paraphrase} (paraphrase-touching)."
    )
    if missing_text:
        print("  missing texts (first 10):")
        for k, count in list(missing_text.items())[:10]:
            print(f"    {k}: {count} judgments unresolved")
    if ambiguous_text:
        n_amb_judgments = sum(ambiguous_text.values())
        print(
            f"  warning: {n_amb_judgments} judgments resolved against a (essay, source, prefix) "
            f"key with multiple text replicates ({len(ambiguous_text)} keys). "
            f"JSONL doesn't record which replicate was judged — picked first by insert order. "
            f"Going forward judgments carry text_a_id/text_b_id explicitly."
        )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--force", action="store_true", help="Wipe versus_* tables before importing"
    )
    parser.add_argument(
        "--prod", action="store_true", help="Target prod (DO NOT use without intent)"
    )
    args = parser.parse_args()

    client = get_client(prod=args.prod)

    # Essays import is a safe upsert (idempotent on id), so it runs
    # unconditionally — separate from the texts/judgments gate.
    t_essays = time.time()
    import_essays(client)
    print(f"  essays: {time.time() - t_essays:.1f}s")

    existing_texts = client.table("versus_texts").select("id", count="exact").limit(1).execute()
    existing_judgments = (
        client.table("versus_judgments").select("id", count="exact").limit(1).execute()
    )
    n_t = existing_texts.count or 0
    n_j = existing_judgments.count or 0
    if n_t or n_j:
        if not args.force:
            print(
                f"Tables not empty (texts={n_t}, judgments={n_j}). Pass --force to wipe and re-import."
            )
            sys.exit(1)
        print(f"Wiping {n_j} judgments and {n_t} texts...")
        client.table("versus_judgments").delete().neq(
            "id", "00000000-0000-0000-0000-000000000000"
        ).execute()
        client.table("versus_texts").delete().neq(
            "id", "00000000-0000-0000-0000-000000000000"
        ).execute()

    t0 = time.time()
    text_lookup = import_texts(client)
    import_judgments(client, text_lookup)
    print(f"\nTotal: {time.time() - t0:.1f}s")


if __name__ == "__main__":
    main()
