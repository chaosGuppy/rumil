"""Atlas: self-describing registry over moves, calls, page types, and workflows.

The atlas reads live from the same sources that the LLM reads —
``MoveDef.description``, ``DispatchDef.description``, payload schemas with
``Field(description=...)``, prompt markdown files, available-moves /
available-calls presets — so the documentation it surfaces cannot drift
from runtime behaviour. Used by the parallel ``/atlas/*`` API routes.

Modules:

- ``descriptions``: canonical natural-language descriptions for ``PageType``
  and ``CallType`` enums (mirrors what ``preamble.md`` already says, but
  next to the code).
- ``registry``: builders that turn the live registries into structured
  schemas the API can serve.
- ``workflows``: workflow-profile composition (orchestrators + versus
  workflows) — stages, prompts, available dispatches, recursion.
- ``aggregate``: cross-run rollups — branch-taken counts, page-load /
  dispatch / cost distributions per workflow.
"""
