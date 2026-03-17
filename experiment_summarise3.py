"""
Experiment: produce short (~30-word) and medium (~200-word) self-contained summaries
of a judgement page using Haiku, Sonnet, and Opus.

Usage:
    uv run python experiment_summarise3.py
"""

import asyncio
import time

import anthropic
from dotenv import load_dotenv

load_dotenv()

PAGE_CONTENT = open(
    "pages/research/judgement-02546c4d-the-most-dangerous-interaction-effects-are-those-that-destro.md",
    encoding="utf-8",
).read()

SYSTEM = (
    "You are a precise information-distillation engine. Your output will be read by LLM instances "
    "that need to quickly understand the key findings of a research judgement. "
    "Prioritise accuracy, epistemic precision, and information density. "
    "Preserve: confidence levels, key qualifications, causal mechanisms, and priority orderings. "
    "Each summary must be fully self-contained — a reader with no prior context should understand "
    "what the judgement is about and what it concludes. "
    "Do not pad or soften. Output only the requested summaries, clearly labelled."
)

PROMPT = """\
Produce two summaries of the research judgement below.

SHORT (~30 words): State the core topic and conclusion in a single self-contained sentence or two. \
Include the highest-priority finding and the main caveat. Must make sense with zero prior context.

MEDIUM (~200 words): Include the core conclusion, the full priority ordering of risks with the key \
reason each is ranked where it is, the most important counter-argument and why it was discounted, \
and the critical empirical uncertainties that would most shift the analysis. Must be self-contained.

Format your response exactly as:
SHORT: <text>

MEDIUM: <text>

Research judgement:
""" + PAGE_CONTENT

MODELS = {
    "haiku": "claude-haiku-4-5-20251001",
    "sonnet": "claude-sonnet-4-6",
    "opus": "claude-opus-4-6",
}


async def run():
    client = anthropic.AsyncAnthropic()

    results = {}
    for model_name, model_id in MODELS.items():
        t0 = time.monotonic()
        response = await client.messages.create(
            model=model_id,
            max_tokens=800,
            system=SYSTEM,
            messages=[{"role": "user", "content": PROMPT}],
        )
        elapsed = time.monotonic() - t0
        results[model_name] = (response.content[0].text.strip(), elapsed)

    print("\n" + "=" * 80)
    print("SOURCE: judgement-02546c4d (1,205 words)")
    print("=" * 80)

    for model_name in MODELS:
        text, elapsed = results[model_name]
        words = len(text.split())
        print(f"\n{'-' * 40}")
        print(f"MODEL: {model_name}  ({elapsed:.1f}s, {words} words total output)")
        print(f"{'-' * 40}")
        print(text)

    print()


if __name__ == "__main__":
    asyncio.run(run())
