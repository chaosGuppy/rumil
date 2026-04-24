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
``COMPLETION_PROMPT_VERSION = 3`` reflects the source-neutral prompt
wording + namespaced essay ids introduced when forethought/redwood/
carlsmith sources were unified.
"""

from __future__ import annotations

COMPLETION_PROMPT_VERSION = 3
PARAPHRASE_PROMPT_VERSION = 3
JUDGE_PROMPT_VERSION = 2
BLIND_JUDGE_VERSION = 3
