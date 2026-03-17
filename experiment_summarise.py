"""
Experiment: summarise a single page at different lengths using different models/prompts.

Usage:
    uv run python experiment_summarise.py
"""

import asyncio
import time

import anthropic
from dotenv import load_dotenv

load_dotenv()

PAGE_CONTENT = """\
# Pre-drafted AI pause legislation is the single highest-ROI preparedness investment available now

Among the five workstreams needed for triggered-pause readiness, pre-drafted emergency legislation stands out as uniquely high-return because it combines four properties no other preparedness investment shares:

1. **Low cost**: $2-5M for a team of 5-8 people over 12-18 months — trivial relative to the billions flowing into AI development and the tens of millions spent on AI governance research.

2. **Clear owner and deliverable**: AI governance organizations (GovAI, CLTR, or a consortium) can produce model statutes for US, UK, and EU jurisdictions without requiring any government action, political consensus, or research breakthrough. The deliverable is concrete: legislative text with specific compute thresholds, enforcement agencies, penalties, exemption scopes, sunset clauses, and exit criteria.

3. **Determines crisis outcomes**: Historical precedent (PATRIOT Act enacted 45 days post-9/11 using pre-drafted DOJ proposals; Dodd-Frank drawing on pre-existing academic proposals) demonstrates that whoever has draft text ready when a political window opens shapes the resulting legislation. If AI safety advocates lack model statutes when a salient AI incident occurs, industry lobbyists or technically uninformed legislators will fill the vacuum.

4. **Value regardless of activation**: Even if no pause is ever triggered, the process of drafting model legislation forces concrete resolution of ambiguities (What compute threshold? Which agency? How broad are exemptions?) that currently allow "prepare for triggered pause" to remain vague. The drafting process itself advances governance thinking.

The current state — no major AI governance organization has published a complete model statute for an AI training moratorium — represents a striking gap between the strategic consensus ("prepare for triggered pause" dominates) and actual preparedness. If this gap persists through 2026, the strategy is effectively a slogan.
""".strip()

LENGTHS = {
    "headline": "one short sentence (under 15 words)",
    "tweet": "2-3 sentences suitable for a tweet (~60 words)",
    "paragraph": "a single paragraph (~100-120 words)",
    "detailed": "a thorough summary of ~200 words preserving key evidence and nuance",
}

PROMPT_STYLES = {
    "neutral": (
        "You are a precise research summariser. Summarise the following research claim "
        "in {length}. Output only the summary text — no preamble, no labels.\n\n{content}"
    ),
    "journalist": (
        "You are a science journalist writing for a policy audience. Summarise the following "
        "research claim in {length}, using plain language. Output only the summary text.\n\n{content}"
    ),
    "structured": (
        "Summarise the following research claim in {length}. Lead with the core claim, "
        "then the key supporting evidence. Output only the summary text.\n\n{content}"
    ),
}

MODELS = {
    "haiku": "claude-haiku-4-5-20251001",
    "sonnet": "claude-sonnet-4-6",
}


async def summarise(client: anthropic.AsyncAnthropic, model_id: str, prompt: str) -> tuple[str, float]:
    t0 = time.monotonic()
    response = await client.messages.create(
        model=model_id,
        max_tokens=512,
        messages=[{"role": "user", "content": prompt}],
    )
    elapsed = time.monotonic() - t0
    return response.content[0].text.strip(), elapsed


async def run_experiment():
    client = anthropic.AsyncAnthropic()

    results: list[dict] = []

    for model_name, model_id in MODELS.items():
        for style_name, prompt_template in PROMPT_STYLES.items():
            for length_name, length_desc in LENGTHS.items():
                prompt = prompt_template.format(length=length_desc, content=PAGE_CONTENT)
                text, elapsed = await summarise(client, model_id, prompt)
                results.append({
                    "model": model_name,
                    "style": style_name,
                    "length": length_name,
                    "elapsed": elapsed,
                    "word_count": len(text.split()),
                    "text": text,
                })
                print(f"[{model_name}/{style_name}/{length_name}] {elapsed:.1f}s, {len(text.split())} words")

    print("\n" + "=" * 80)
    print("RESULTS")
    print("=" * 80)

    for length_name in LENGTHS:
        print(f"\n### Length: {length_name}\n")
        for model_name in MODELS:
            for style_name in PROMPT_STYLES:
                r = next(x for x in results if x["model"] == model_name and x["style"] == style_name and x["length"] == length_name)
                print(f"  [{model_name} / {style_name}] ({r['word_count']} words, {r['elapsed']:.1f}s)")
                print(f"  {r['text']}")
                print()


if __name__ == "__main__":
    asyncio.run(run_experiment())
