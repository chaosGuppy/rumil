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

from versus import config, jsonl, openrouter
from versus import essay as versus_essay

# Bump when ``PARAPHRASE_INSTRUCTIONS`` changes in a way that should
# invalidate existing paraphrase rows. Folded into ``sampling_hash``
# below so each paraphrase key forks on edit. Paraphrases are keyed on
# (essay_id, model_id, sampling_hash), so a bump here produces fresh
# keys without clobbering.
PARAPHRASE_PROMPT_VERSION = 3


PARAPHRASE_INSTRUCTIONS = """\
Rewrite the following essay in your own prose style while preserving the content exactly.

Requirements:
- Keep every section heading unchanged in wording and order.
- Keep the same claims, arguments, examples, caveats, distinctions, and level of detail.
- Keep the same semantic structure (same points made in the same order).
- Do NOT add new content or remove substantive content.
- Do NOT include the essay title — start directly with the body.
- Use standard Markdown: headings with ## / ### / ####, plain paragraphs, lists with `- `.
- The rewrite should read naturally in your voice.

---ESSAY BODY---
{markdown}
---END---

Output only the rewritten markdown body, nothing else."""


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
    resp = openrouter.chat(
        model=m.id,
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


def run(cfg: config.Config, essays: list[versus_essay.Essay]) -> None:
    if not cfg.paraphrasing.enabled or not cfg.paraphrasing.models:
        print("[paraphrase] disabled or no models configured; skipping")
        return
    log = cfg.storage.paraphrases_log
    existing = jsonl.keys(log)

    tasks_to_run: list = []
    for essay in essays:
        prompt = PARAPHRASE_INSTRUCTIONS.format(markdown=essay.markdown)
        for m in cfg.paraphrasing.models:
            sh = sampling_hash(m)
            k = paraphrase_key(essay.id, m.id, sh)
            if k in existing:
                print(f"[skip] paraphrase {k}")
                continue
            tasks_to_run.append((essay, m, sh, k, prompt))
            existing.add(k)

    if not tasks_to_run:
        return
    print(f"[run ] {len(tasks_to_run)} paraphrase calls (concurrency={cfg.concurrency})")
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
                    continue
                jsonl.append(log, row)
                print(f"[done] paraphrase {k}")
    finally:
        client.close()
