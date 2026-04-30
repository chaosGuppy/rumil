"""Lightweight per-script call tally — counts, token totals, optional cost, wall time.

Used by run_completions / run_paraphrases / run_rumil_judgments to print
a closing line so the operator sees what happened without scrolling
back through [done] lines. Direct-LLM paths (blind judges, completions,
paraphrases) report tokens; rumil-mediated paths (ws/orch) have a
real $ figure on the result and report cost_usd instead.
"""

from __future__ import annotations

import time
from typing import Any


class RunSummary:
    def __init__(self) -> None:
        self.t_start = time.monotonic()
        self.n_done = 0
        self.n_err = 0
        self.in_tokens = 0
        self.out_tokens = 0
        self.cost_usd = 0.0

    def record_success(self, response: Any = None, *, cost_usd: float = 0.0) -> None:
        self.n_done += 1
        self.cost_usd += cost_usd
        if not isinstance(response, dict):
            return
        usage = response.get("usage") or {}
        if not isinstance(usage, dict):
            return
        self.in_tokens += usage.get("input_tokens") or usage.get("prompt_tokens") or 0
        self.out_tokens += usage.get("output_tokens") or usage.get("completion_tokens") or 0

    def record_error(self) -> None:
        self.n_err += 1

    def print(self, label: str) -> None:
        elapsed = time.monotonic() - self.t_start
        parts = [f"{self.n_done} done, {self.n_err} errors"]
        if self.in_tokens or self.out_tokens:
            parts.append(f"in={self.in_tokens:,} out={self.out_tokens:,} tokens")
        if self.cost_usd > 0:
            parts.append(f"${self.cost_usd:.4f}")
        parts.append(f"{elapsed:.1f}s wall")
        print(f"[summary] {label}: " + " · ".join(parts))
