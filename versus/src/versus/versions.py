"""Central registry of prompt / dedup version constants.

All four versus "prompt version" knobs live here so scattered bumps can't
drift between modules. See versus/AGENT.md for how each interacts with
dedup keys.

- ``COMPLETION_PROMPT_VERSION``: folded into ``prefix_config_hash`` in
  :func:`versus.prepare.prepare`. Bump when ``render_prompt`` changes.
- ``PARAPHRASE_PROMPT_VERSION``: folded into ``sampling_hash`` in
  :func:`versus.paraphrase.sampling_hash`. Bump when
  ``PARAPHRASE_INSTRUCTIONS`` changes.
- ``JUDGE_PROMPT_VERSION``: appended to OpenRouter / ``anthropic:<model>``
  judge_model strings via :func:`versus.judge.compose_judge_model`.
  Bump when ``render_judge_prompt`` or the shared judge prompt changes
  in a way not captured by the prompt hash.
- ``BLIND_JUDGE_VERSION``: appended to ``rumil:ws:*`` / ``rumil:orch:*``
  judge_model strings by the bridge. Bump for semantic changes in the
  bridge not captured by the prompt hash (blind-judge leak fixes,
  tool-list changes, inline user-message edits).

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
JUDGE_PROMPT_VERSION = 2
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
