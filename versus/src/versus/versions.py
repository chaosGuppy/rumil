"""Central registry of prompt / dedup version constants.

All four versus "prompt version" knobs live here so scattered bumps can't
drift between modules. See versus/AGENT.md for how each interacts with
dedup keys.

- ``COMPLETION_PROMPT_VERSION``: folded into ``prefix_config_hash`` in
  :func:`versus.prepare.prepare`. Bump when ``render_prompt`` changes.
- ``PARAPHRASE_PROMPT_VERSION``: folded into ``sampling_hash`` in
  :func:`versus.paraphrase.sampling_hash`. Bump when
  ``PARAPHRASE_INSTRUCTIONS`` changes.
- ``BLIND_JUDGE_VERSION``: manual override / escape hatch stamped
  onto every judge_model (blind / ws / orch). With ``code_fingerprint``
  in place for ws/orch, the auto-hashes catch most semantic changes.
  Bump this only when you *want* to invalidate prior rows even when
  no measurable input changed (e.g. you've reinterpreted what an
  existing prompt means and want a hard fork without editing files).

Numeric values match their former per-module locations.
``COMPLETION_PROMPT_VERSION = 5`` adds a
``<continuation>...</continuation>`` scratch-space convention: models
can plan/outline before the tagged block, and only the content inside
the last tag is used for judging. Models that skip the tag fall back
to full-text behaviour.
"""

from __future__ import annotations

COMPLETION_PROMPT_VERSION = 5
PARAPHRASE_PROMPT_VERSION = 3

# Curated set folded into ``code_fingerprint`` on every ws/orch
# judgment config. Covers the bridge, sdk-agent, and (per directory)
# every orchestrator + call + prompt that the agent's run can touch.
# Directories collapse to a single per-directory sha so the config
# dict stays compact while still forking on any constituent edit.
# Anchored at the rumil repo root by judge_config helpers. Edits to
# comments / whitespace re-fingerprint; that's a feature (drift is
# visible in config diffs).
JUDGE_CODE_FINGERPRINT_DIRS: tuple[tuple[str, str], ...] = (
    ("src/rumil/orchestrators/", "*.py"),
    ("src/rumil/calls/", "*.py"),
    ("src/rumil/prompts/", "*.md"),
    ("src/rumil/workspace_exploration/", "*.py"),
)
JUDGE_CODE_FINGERPRINT_FILES: tuple[str, ...] = (
    "src/rumil/versus_bridge.py",
    "src/rumil/versus_prompts.py",
    "src/rumil/sdk_agent.py",
    "src/rumil/llm.py",
    "src/rumil/context.py",
)

# v4 (2026-04-23): extract_preference parses the LAST 7-point label in the
# output instead of the first, so models that think-out-loud and revise
# their rating don't get locked to an early mention. Unhashed surface
# change — affects verdicts but not prompt/tool hashes.
# v5 (2026-04-24): orch closer is now an SDK agent with the three
# workspace-exploration tools and max_turns=5 (was a single text_call
# with no tools despite the shell prompt promising them). The closer
# now also sees considerations/judgements at CONTENT detail and the
# View + view_items inline — materially changes what signal the
# verdict is conditioned on, so rumil:orch rows fork cleanly from
# pre-bump rows.
# v6 (2026-04-24): both ws and orch now extract the 7-point label
# from last_assistant_text (final turn only) instead of
# all_assistant_text (joined across all turns). The prompt shell
# pins the verdict to the final turn ("End your response with ...");
# earlier turns may mention labels mid-thought and those shouldn't
# count.
BLIND_JUDGE_VERSION = 6
