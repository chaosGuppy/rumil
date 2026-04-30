"""Generate model-paraphrased versions of essays.

Each paraphrase keeps all section headers and semantic content but rewrites the
prose in the paraphrasing model's own voice. Paraphrases are cached per
(essay_id, model_id, sampling_hash). Downstream, the completion pipeline
synthesizes a "paraphrase:<model>" source by splitting the paraphrase at the
same paragraph count used for the real completion task.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

import httpx

from versus import anthropic_client, config, jsonl, openrouter
from versus import essay as versus_essay
from versus.judge import route_judge_model
from versus.run_summary import RunSummary
from versus.versions import PARAPHRASE_PROMPT_VERSION

PARAPHRASE_INSTRUCTIONS = (
    "Rewrite the following essay in your own prose style while preserving the content exactly.\n"
    "\n"
    "Requirements:\n"
    "- Keep every section heading unchanged in wording and order.\n"
    "- Keep the same claims, arguments, examples, caveats, distinctions, and level of detail.\n"
    "- Keep the same semantic structure (same points made in the same order).\n"
    "- Do NOT add new content or remove substantive content.\n"
    "- Do NOT include the essay title — start directly with the body.\n"
    "- Use standard Markdown: headings with ## / ### / ####, plain paragraphs, lists with `- `.\n"
    "- The rewrite should read naturally in your voice.\n"
    "\n"
    "---ESSAY BODY---\n"
    "{markdown}\n"
    "---END---\n"
    "\n"
    "Output only the rewritten markdown body, nothing else."
)


HEADING_RE = re.compile(r"^(#{2,4})\s+(.+?)\s*$")


def sampling_hash(model_cfg: config.ModelCfg) -> str:
    payload = {
        "params": model_cfg.model_dump(exclude={"id"}),
        "prompt_version": PARAPHRASE_PROMPT_VERSION,
    }
    blob = json.dumps(payload, sort_keys=True)
    return hashlib.sha256(blob.encode()).hexdigest()[:10]


def paraphrase_key(essay_id: str, model_id: str, samp_hash: str) -> str:
    return f"{essay_id}|{model_id}|{samp_hash}"


def markdown_to_blocks(md: str) -> list[versus_essay.Block]:
    """Rough inverse of blocks_to_markdown. Splits on blank lines.

    Only recognizes ##/###/#### as h1/h2/h3 (other lines are paragraphs).
    Preserves list/blockquote text verbatim inside paragraph blocks.
    """
    out: list[versus_essay.Block] = []
    chunks = re.split(r"\n\s*\n", md.strip())
    for chunk in chunks:
        chunk = chunk.strip("\n").rstrip()
        if not chunk:
            continue
        first_line = chunk.split("\n", 1)[0]
        m = HEADING_RE.match(first_line)
        if m and "\n" not in chunk:
            hashes = m.group(1)
            level_map = {"##": "h1", "###": "h2", "####": "h3"}
            out.append(versus_essay.Block(type=level_map[hashes], text=m.group(2)))
        else:
            out.append(versus_essay.Block(type="p", text=chunk))
    return out


def _call_one_paraphrase(essay, m, sh, k, prompt, client):
    t0 = time.time()
    provider, canonical_model = route_judge_model(m.id)
    if provider == "anthropic":
        # Opus 4.7 deprecates explicit temperature; drop top_p too out of caution
        # (see complete.py for context).
        if canonical_model.startswith("claude-opus-4-7"):
            temp, top_p = None, None
        else:
            temp, top_p = m.temperature, m.top_p
        resp = anthropic_client.chat(
            model=canonical_model,
            messages=[{"role": "user", "content": prompt}],
            temperature=temp,
            max_tokens=m.max_tokens,
            top_p=top_p,
            client=client,
        )
        text = anthropic_client.extract_text(resp)
    else:
        resp = openrouter.chat(
            model=canonical_model,
            messages=[{"role": "user", "content": prompt}],
            temperature=m.temperature,
            max_tokens=m.max_tokens,
            top_p=m.top_p,
            client=client,
        )
        text = openrouter.extract_text(resp)
    blocks = markdown_to_blocks(text)
    return {
        "key": k,
        "essay_id": essay.id,
        "model_id": m.id,
        "sampling_hash": sh,
        "params": m.model_dump(exclude={"id"}),
        "prompt": prompt,
        "response_text": text,
        "response_words": len(text.split()),
        "blocks": [{"type": b.type, "text": b.text} for b in blocks],
        "ts": dt.datetime.utcnow().isoformat() + "Z",
        "duration_s": round(time.time() - t0, 2),
        "raw_response": resp,
    }


def paraphrase_models(cfg: config.Config) -> list[config.ModelCfg]:
    """Models flagged for paraphrasing in cfg.completion.models."""
    return [m for m in cfg.completion.models if m.paraphrase]


def run(cfg: config.Config, essays: list[versus_essay.Essay], *, dry_run: bool = False) -> None:
    models = paraphrase_models(cfg)
    if not cfg.paraphrasing.enabled or not models:
        print("[paraphrase] disabled or no models flagged paraphrase=true; skipping")
        return
    log = cfg.storage.paraphrases_log
    existing = jsonl.keys(log)

    tasks_to_run: list = []
    for essay in essays:
        prompt = PARAPHRASE_INSTRUCTIONS.format(markdown=essay.markdown)
        for m in models:
            sh = sampling_hash(m)
            k = paraphrase_key(essay.id, m.id, sh)
            if k in existing:
                print(f"[skip] paraphrase {k}")
                continue
            tasks_to_run.append((essay, m, sh, k, prompt))
            existing.add(k)

    if not tasks_to_run:
        return
    if dry_run:
        print(f"[plan] {len(tasks_to_run)} paraphrase calls (concurrency={cfg.concurrency})")
        for e, m, _sh, k, _p in tasks_to_run:
            print(f"  * {e.id} | {m.id} | {k}")
        return
    print(f"[run ] {len(tasks_to_run)} paraphrase calls (concurrency={cfg.concurrency})")
    summary = RunSummary()
    client = httpx.Client(timeout=600.0)
    try:
        with ThreadPoolExecutor(max_workers=cfg.concurrency) as pool:
            futures = {
                pool.submit(_call_one_paraphrase, e, m, sh, k, p, client): k
                for (e, m, sh, k, p) in tasks_to_run
            }
            for fut in as_completed(futures):
                k = futures[fut]
                try:
                    row = fut.result()
                except Exception as ex:
                    print(f"[err ] paraphrase {k}: {ex}")
                    summary.record_error()
                    continue
                jsonl.append(log, row)
                summary.record_success(row.get("raw_response"))
                print(f"[done] paraphrase {k}")
    finally:
        client.close()
        summary.print("paraphrases")
