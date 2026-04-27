"""LLM-based validator for normalized essay markdown.

Run after fetch+normalize to catch scraping artifacts (nested-list duplication,
orphan footnote digits, caption leakage, encoding bugs, nav/footer leakage,
etc.) that would otherwise silently feed into completions and judgments.

Cached per essay content hash so repeated imports of unchanged essays don't
re-spend. Verdicts persist next to the essay JSON as ``<id>.verdict.json``.
"""

from __future__ import annotations

import hashlib
import json
import pathlib
import re

from versus import anthropic_client

VALIDATOR_MODEL = "claude-sonnet-4-6"
VALIDATOR_MAX_TOKENS = 4000
VALIDATOR_TEMPERATURE = 0.0

# Bump if you change the prompt or the issue taxonomy in a way that should
# invalidate cached verdicts.
VALIDATOR_VERSION = 1

SYSTEM_PROMPT = (
    "You inspect normalized markdown extracted from a longform web essay "
    "and decide whether it is clean enough to feed into a language-model "
    "evaluation pipeline.\n\n"
    "The pipeline takes the markdown verbatim and asks judges to read it as "
    "if it were the original essay. Anything that would visibly degrade the "
    "reader's experience or change the meaning is an issue. Anything that is "
    "merely a stylistic choice from the author is NOT an issue.\n\n"
    "Known scraping/normalization failure modes to look for:\n"
    "- DUPLICATION: the same sentence or paragraph appearing twice in a row, "
    "or a parent bullet whose text already contains its child bullets' text "
    "while those children are also emitted as siblings.\n"
    "- ORPHAN_FOOTNOTE: a bare digit or '^' at the end of a sentence (e.g. "
    "'public discourse. 2') that is clearly a stripped footnote reference. "
    "Footnote bodies missing entirely is also an orphan_footnote.\n"
    "- PUNCTUATION_SPACING: stray space before a comma/period/colon/semicolon "
    "or inside parentheses (e.g. 'design space .' or '( much - discussed )').\n"
    "- LOST_EMPHASIS: a phrase that was clearly bold or italic in the source "
    "rendered as plain text and now reads awkwardly (e.g. 'User interface :' "
    "where the colon makes it obvious it was a bolded label).\n"
    "- CAPTION_LEAK: a standalone paragraph that is just an image caption or "
    "credit line (e.g. an italic painting title with artist + year).\n"
    "- NAV_FOOTER_LEAK: 'Subscribe', 'Share', 'Read more', author bios, "
    "related-posts, social-media buttons, cookie banners, comment sections, "
    "paywall CTAs, newsletter signup prompts, 'Thanks for reading'.\n"
    "- ENCODING: mojibake (e.g. â€™ for '), unresolved HTML entities (&amp;, "
    "&#39;), zero-width characters, BOM markers.\n"
    "- TRUNCATION: text that abruptly cuts off mid-sentence.\n"
    "- BROKEN_STRUCTURE: malformed lists/tables, headings out of order in a "
    "way that breaks the document hierarchy, code fences that never close.\n"
    "- OTHER: anything else that looks wrong (be specific in description).\n\n"
    "Stylistic choices that are NOT issues:\n"
    "- Smart quotes / em-dashes that render correctly.\n"
    "- The essay starting at H2 (## ...) instead of H1 — the title is "
    "stored separately and intentionally not in the markdown body.\n"
    "- Mixed straight + curly apostrophes if both render correctly.\n"
    "- The author's chosen heading hierarchy, even if a subsection uses a "
    "deeper heading level than its sibling.\n"
    "- Long paragraphs, lists with one item, or other authorial decisions.\n\n"
    "Return strict JSON matching this schema and NOTHING else:\n"
    "{\n"
    '  "clean": <bool>,\n'
    '  "issues": [\n'
    "    {\n"
    '      "kind": <one of: duplication, orphan_footnote, punctuation_spacing, '
    "lost_emphasis, caption_leak, nav_footer_leak, encoding, truncation, "
    "broken_structure, other>,\n"
    '      "snippet": <short verbatim quote from the markdown, ~10-60 chars, '
    "showing the problem>,\n"
    '      "description": <one short sentence explaining what is wrong>\n'
    "    },\n"
    "    ...\n"
    "  ]\n"
    "}\n\n"
    "If there are zero issues, set clean=true and return an empty issues "
    "list. Be precise: each snippet must appear verbatim in the markdown."
)


def content_hash(markdown: str) -> str:
    return hashlib.sha256(markdown.encode("utf-8")).hexdigest()[:16]


def verdict_path(cache_dir: pathlib.Path, essay_id: str) -> pathlib.Path:
    return cache_dir / f"{essay_id}.verdict.json"


def _cached_verdict(path: pathlib.Path, md_hash: str) -> dict | None:
    if not path.is_file():
        return None
    try:
        v = json.loads(path.read_text())
    except json.JSONDecodeError:
        return None
    if v.get("validator_version") != VALIDATOR_VERSION:
        return None
    if v.get("content_hash") != md_hash:
        return None
    return v


def _parse_response(text: str) -> dict:
    cleaned = text.strip()
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        pass
    # Fenced JSON anywhere in the response (Sonnet sometimes prefixes prose).
    for m in re.finditer(r"```(?:json)?\s*\n(\{[\s\S]*?\})\s*\n```", cleaned):
        try:
            return json.loads(m.group(1))
        except json.JSONDecodeError:
            continue
    # Balanced-brace scan: try every candidate ``{...}`` object that contains
    # a ``"clean"`` key, from innermost/latest to outermost/earliest.
    candidates: list[str] = []
    stack: list[int] = []
    for i, c in enumerate(cleaned):
        if c == "{":
            stack.append(i)
        elif c == "}" and stack:
            start = stack.pop()
            candidates.append(cleaned[start : i + 1])
    for cand in reversed(candidates):
        if '"clean"' not in cand:
            continue
        try:
            return json.loads(cand)
        except json.JSONDecodeError:
            continue
    raise ValueError(f"validator response is not JSON: {text[:500]!r}")


def validate(
    essay_id: str,
    markdown: str,
    cache_dir: pathlib.Path,
    *,
    force: bool = False,
) -> dict:
    """Return the verdict dict, calling Sonnet only when the cache misses.

    Verdict shape:
        {"clean": bool, "issues": [...], "content_hash": str,
         "validator_version": int, "model": str}
    """
    md_hash = content_hash(markdown)
    path = verdict_path(cache_dir, essay_id)
    if not force:
        cached = _cached_verdict(path, md_hash)
        if cached is not None:
            return cached

    user_msg = (
        f"Essay id: {essay_id}\n\n"
        "Normalized markdown follows between <markdown> tags. "
        "Inspect for issues per the system prompt rules.\n\n"
        f"<markdown>\n{markdown}\n</markdown>"
    )
    resp = anthropic_client.chat(
        model=VALIDATOR_MODEL,
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": user_msg}],
        temperature=VALIDATOR_TEMPERATURE,
        max_tokens=VALIDATOR_MAX_TOKENS,
    )
    text = anthropic_client.extract_text(resp)
    parsed = _parse_response(text)
    if "clean" not in parsed or "issues" not in parsed:
        raise ValueError(f"validator response missing required fields: {parsed!r}")

    verdict = {
        "essay_id": essay_id,
        "clean": bool(parsed["clean"]),
        "issues": parsed["issues"],
        "content_hash": md_hash,
        "validator_version": VALIDATOR_VERSION,
        "model": VALIDATOR_MODEL,
    }
    path.write_text(json.dumps(verdict, indent=2, ensure_ascii=False))
    return verdict


def format_verdict(v: dict) -> str:
    if v["clean"]:
        return f"  [ok]   {v['essay_id']}: clean"
    lines = [f"  [fail] {v['essay_id']}: {len(v['issues'])} issue(s)"]
    for issue in v["issues"]:
        snippet = issue.get("snippet", "")
        lines.append(
            f"      [{issue.get('kind', '?')}] {snippet!r}: {issue.get('description', '')}"
        )
    return "\n".join(lines)
