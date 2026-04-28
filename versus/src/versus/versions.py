"""Central registry of prompt / dedup version constants.

- ``COMPLETION_PROMPT_VERSION``: folded into ``prefix_config_hash`` in
  :func:`versus.prepare.prepare`. Bump when ``render_prompt`` changes.
- ``PARAPHRASE_PROMPT_VERSION``: folded into ``sampling_hash`` in
  :func:`versus.paraphrase.sampling_hash`. Bump when
  ``PARAPHRASE_INSTRUCTIONS`` changes.

(``BLIND_JUDGE_VERSION`` was retired — judge-side prompt content is
covered by the auto-computed prompt hash and the code fingerprint
hashes ws/orch rely on, so the manual willpower knob no longer earns
its weight. If you ever need a "hard fork without editing files,"
add a CLI flag rather than reintroducing a global.)
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
