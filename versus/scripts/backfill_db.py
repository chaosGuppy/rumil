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

from versus.versus_db import get_client, insert_judgment, insert_text

DATA_DIR = pathlib.Path(__file__).resolve().parent.parent / "data"
COMPLETIONS = DATA_DIR / "completions.jsonl"
JUDGMENTS = DATA_DIR / "judgments.jsonl"


def _load_jsonl(path: pathlib.Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


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


def import_texts(client) -> dict[tuple[str, str], str]:
    """Insert texts and return ``{(essay_id, source_id): text_id}`` lookup.

    Paraphrase generation is deferred — paraphrase-file rows and
    paraphrase-derived completion rows are skipped. The JSONL archive still
    holds them if we want to re-import once the paraphrase model is settled.
    """
    lookup: dict[tuple[str, str], str] = {}
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
        lookup[(row["essay_id"], row["source_id"])] = text_id
        if i % 100 == 0:
            print(f"  ... {i}/{len(completion_rows)}")
    print(f"  done. Skipped {n_skipped} (errors), {n_skipped_paraphrase} (paraphrase-derived).")
    return lookup


def import_judgments(client, text_lookup: dict[tuple[str, str], str]) -> None:
    rows = _load_jsonl(JUDGMENTS)
    print(f"Importing {len(rows)} judgment rows (skipping paraphrase-touching)...")
    missing_text: dict[tuple[str, str], int] = defaultdict(int)
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
        ta_key = (row["essay_id"], sa)
        tb_key = (row["essay_id"], sb)
        text_a_id = text_lookup.get(ta_key)
        text_b_id = text_lookup.get(tb_key)
        if text_a_id is None or text_b_id is None:
            missing_text[ta_key if text_a_id is None else tb_key] += 1
            n_skipped += 1
            continue

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
                prefix_hash=row["prefix_config_hash"],
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
