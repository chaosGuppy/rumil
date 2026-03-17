"""
Experiment: summarise a judgement page at graduated lengths for LLM consumption.

Prompts focus on information density over readability. Each length level adds
a layer of guidance about what additional content is most important to include.

Usage:
    uv run python experiment_summarise2.py
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

LENGTHS = {
    "10-15 words": (
        "Summarise the following research judgement in exactly 10-15 words. "
        "Capture only the single most important conclusion. "
        "Output the summary text only — no preamble, labels, or punctuation beyond what belongs in the sentence."
    ),
    "20-30 words": (
        "Summarise the following research judgement in exactly 20-30 words. "
        "State the core conclusion and the top-priority finding. "
        "Output the summary text only."
    ),
    "50-70 words": (
        "Summarise the following research judgement in exactly 50-70 words. "
        "Convey the core conclusion, the priority ordering of risks, and the key reasoning behind the top priority. "
        "Output the summary text only."
    ),
    "100-120 words": (
        "Summarise the following research judgement in exactly 100-120 words. "
        "Include: the core conclusion, the full priority ordering of risks with brief justification for each rank, "
        "and the author's confidence level and main sources of uncertainty. "
        "Output the summary text only."
    ),
    "170-200 words": (
        "Summarise the following research judgement in exactly 170-200 words. "
        "Include: the core conclusion, the full priority ordering with reasoning, "
        "the key counter-argument and why it was discounted, "
        "and the critical empirical dependencies that would most change the analysis. "
        "Output the summary text only."
    ),
    "250-300 words": (
        "Summarise the following research judgement in exactly 250-300 words. "
        "Include: the core conclusion, the full priority ordering with reasoning for each rank, "
        "how each risk interacts with or feeds into the others, "
        "the key counter-argument and why it was discounted, "
        "the geographic dimension, "
        "and the critical empirical dependencies with the direction their resolution would shift the analysis. "
        "Output the summary text only."
    ),
}

MODELS = {
    "haiku": "claude-haiku-4-5-20251001",
    "sonnet": "claude-sonnet-4-6",
}


async def summarise(client: anthropic.AsyncAnthropic, model_id: str, system: str, content: str) -> tuple[str, float, int]:
    t0 = time.monotonic()
    response = await client.messages.create(
        model=model_id,
        max_tokens=600,
        system=system,
        messages=[{"role": "user", "content": content}],
    )
    elapsed = time.monotonic() - t0
    words = len(response.content[0].text.strip().split())
    return response.content[0].text.strip(), elapsed, words


SYSTEM = (
    "You are a precise information-distillation engine. Your output will be read by LLM instances "
    "that need to quickly understand the key findings of a research judgement. "
    "Prioritise accuracy, epistemic precision, and information density. "
    "Preserve: confidence levels, key qualifications, causal mechanisms, and priority orderings. "
    "Do not pad or soften. Do not add caveats beyond those in the source. "
    "Output only the summary — no preamble, headers, or labels."
)


async def run():
    client = anthropic.AsyncAnthropic()
    results = {}

    for length_label, user_prompt in LENGTHS.items():
        results[length_label] = {}
        for model_name, model_id in MODELS.items():
            text, elapsed, words = await summarise(client, model_id, SYSTEM, user_prompt + "\n\n" + PAGE_CONTENT)
            results[length_label][model_name] = (text, elapsed, words)

    # --- Print results ---
    print("\n" + "=" * 80)
    print("SOURCE PAGE (truncated to first paragraph)")
    print("=" * 80)
    first_para = next(
        line for line in PAGE_CONTENT.split("\n")
        if line.startswith("#")
    )
    print(first_para)
    print()

    for length_label in LENGTHS:
        print("=" * 80)
        print(f"TARGET LENGTH: {length_label}")
        print("=" * 80)
        for model_name in MODELS:
            text, elapsed, words = results[length_label][model_name]
            print(f"\n  [{model_name}] ({words} words, {elapsed:.1f}s)")
            print(f"  {text}")
        print()


if __name__ == "__main__":
    asyncio.run(run())
