"""Shared helpers for Claude Code skills that drive the rumil workspace.

All skill scripts under .claude/skills/rumil-* import from this package via
`uv run` (rumil is installed as editable from pyproject.toml, so imports
from rumil.* and .claude.lib.rumil_skills.* both work).

Two-lane provenance model:
  - rumil-mediated: a real rumil call (FIND_CONSIDERATIONS / ASSESS / scout /
    ...) triggered from Claude Code. The call is identical to one kicked off
    by main.py, just tagged with origin=claude-code in runs.config and
    calls.call_params.
  - cc-mediated: Claude Code is the brain. Moves are applied directly onto
    a CLAUDE_CODE_DIRECT envelope Call without running a rumil-internal LLM
    call. The call_type itself marks provenance.
"""
