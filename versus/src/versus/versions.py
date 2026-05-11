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

# Cross-cutting code fingerprint (post-#425): the harness layer every
# versus run touches regardless of which Workflow / Task is composed
# in. Workflow-specific surfaces (orchestrator modules, dispatched
# call modules, per-call prompts) moved out — each Workflow declares
# its own ``code_paths`` and the per-workflow fingerprint covers them.
#
# Directories collapse to a single per-directory sha so the config
# dict stays compact while still forking on any constituent edit.
# Anchored at the rumil repo root by ``versus_config`` helpers. Edits
# to comments / whitespace re-fingerprint; that's a feature (drift is
# visible in config diffs).
SHARED_CODE_FINGERPRINT_DIRS: tuple[tuple[str, str], ...] = (
    # Closer's exploration tools — every workflow's closer uses these
    # regardless of how the upstream research subgraph was produced.
    ("src/rumil/workspace_exploration/", "*.py"),
    # Tracing affects what events the agent loop records and what cost
    # / step accounting flows back into every row.
    ("src/rumil/tracing/", "*.py"),
)
SHARED_CODE_FINGERPRINT_FILES: tuple[str, ...] = (
    # The versus harness / bridge layer — runner composes Workflow +
    # Task; closer drives the SDK agent; bridge owns the per-task
    # surface hashes; workflow protocol defines the contract every
    # workflow implements.
    "src/rumil/versus_runner.py",
    "src/rumil/versus_closer.py",
    "src/rumil/versus_workflow.py",
    "src/rumil/versus_bridge.py",
    "src/rumil/versus_prompts.py",
    # Shared prompt scaffold — included by every call's system prompt.
    # Per-call prompts (find_considerations.md, assess.md, ...) are
    # workflow-specific and live on the workflow's ``code_paths``.
    "src/rumil/prompts/preamble.md",
    # Rumil-side execution glue every workflow's calls flow through.
    "src/rumil/sdk_agent.py",
    "src/rumil/llm.py",
    "src/rumil/context.py",
    # ``wrap_as_mcp_tool`` from run_eval.runner sits in the call path
    # of every workflow's closer via the tool-wrapping step.
    "src/rumil/run_eval/runner.py",
    # Database / settings / page+link models govern what the agent's
    # workspace tools surface and how runtime behaviour is configured.
    "src/rumil/database.py",
    "src/rumil/settings.py",
    "src/rumil/models.py",
)

# Back-compat aliases for the pre-#425 names. Removable once no callers
# import the old names.
JUDGE_CODE_FINGERPRINT_DIRS = SHARED_CODE_FINGERPRINT_DIRS
JUDGE_CODE_FINGERPRINT_FILES = SHARED_CODE_FINGERPRINT_FILES
