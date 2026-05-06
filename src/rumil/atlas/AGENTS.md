# Atlas

## What this is

Atlas is rumil's self-describing surface. It exposes the moves, dispatches,
call types, page types, prompts, and orchestrator workflows the system uses —
read live from the same registries the LLM consumes — as a browseable,
deeply-linked UI for humans.

The atlas is **parallel** to the existing operator UI. It does not replace
`/projects`, `/traces`, `/versus`, etc. It is its own surface, mounted at
`/api/atlas/*` (backend) and `/atlas/*` (frontend).

## Spirit

**Descriptions written for the model are already under selection pressure to
be precise.** When `MoveDef.description` lies, the orchestrator dispatches
wrong; when `Field(description=...)` is vague, the model fills it in noisy.
Human-only docstrings decay silently. Atlas-rendered docs do not — they are
the same strings the LLM is reading at runtime.

This frames every design choice:

1. **Live, never frozen.** Every page in atlas reads its content from the
   live registry on request. There is no separate documentation store, no
   build step, no "regenerate docs" command. If the code says it, atlas
   shows it.

2. **Code-as-data wherever the code allows.** Move payload schemas → JSON
   Schema → rendered field tables with each field's `Field(description=...)`
   as the prominent column. Available-moves presets → rendered as the
   preset's effect on a call type. Prompt files → parsed into sections,
   composed per call type. Workflow profiles → declarative `WorkflowProfile`
   records the imperative orchestrators don't carry, with a description-
   completeness test catching drift.

3. **Cross-link density over hierarchy.** A move is meaningful in the
   context of the call types that admit it; a call type is meaningful in
   the context of the workflow stages that dispatch it; a stage is
   meaningful in the context of the prompt parts that frame it. Atlas
   makes those edges first-class — every count is a link, every chip with a
   meaningful target navigates, every entity surfaces its inverse
   relationships.

4. **From taxonomy to telemetry.** The atlas starts as a catalog of *what
   exists*. It earns its keep by becoming a microscope for *what
   happens* — empirical stats per call type / move, branch-taken counts
   per workflow, run flow trees, live overlays of a single run on the
   workflow diagram. The same registry views that name the moving parts
   should also tell you how often each part actually moves.

5. **Drift is detectable.** What can be checked statically is checked by a
   description-completeness lint (every move/dispatch/call/page type/
   workflow stage has prose; every workflow stage's prompt files exist;
   every dispatch reference resolves). What can be checked dynamically
   is reserved for a future smoke-test that fires each orchestrator and
   asserts observed `*StartedEvent` types ⊆ declared stages.

## Module map

- `descriptions.py` — canonical natural-language descriptions for
  `PageType` / `CallType` / `PageLayer` / `Workspace`. Mirrors what
  `prompts/preamble.md` already says about each, but next to the code.
- `schemas.py` — Pydantic types served by the atlas API.
- `registry.py` — builds the live registry rollups (moves, dispatches,
  call types, page types) by introspecting `MOVES`, `DISPATCH_DEFS`,
  available-moves and available-calls presets, prompt directory, and
  `CallRunner` subclasses.
- `prompt_parts.py` — granular prompt model: sections within a file,
  parts (file + role + condition) composed per call type. Mirrors
  what `build_system_prompt()` actually produces.
- `workflows.py` — declarative `WorkflowProfile`s for orchestrators and
  versus workflows. Stages, branches, loops, prompts, available
  dispatches, recurses_into. The piece that doesn't live in code today
  because the orchestrators are imperative; the description-completeness
  lint keeps it honest.
- `aggregate.py` — cross-run rollups: stage-taken counts, dispatch
  frequencies, sparkline series, per-run flow trees.
- `stats.py` — per-call-type / per-move empirical stats across recent
  runs (sibling of `aggregate.py` but keyed on registry items rather
  than workflows).
- `gaps.py` — detected inconsistencies: call types without runner
  classes, settings declared relevant that don't exist, prompt files
  unreferenced anywhere, etc.
- `search.py` — text search over registry + prompt content.
- `overlay.py` — live-trace overlay for a run on its workflow's stage
  diagram.
- `wisdom.py` — content-shaped surfaces: recent-work feed and
  per-question trajectory (judgements, views, considerations across
  runs, plus credence volatility / converging detection). Where atlas
  starts answering "is the system getting wiser?" rather than "what
  parts exist".

## Conventions for adding to atlas

- New atlas content lives under `src/rumil/atlas/*` and gets its own
  endpoint under `/api/atlas/*` in `src/rumil/api/atlas_router.py`.
- Schemas always go in `atlas/schemas.py` so the FE OpenAPI generator
  picks them up uniformly. No exceptions.
- If a new piece of metadata needs to be tracked (new enum, new field
  on a move payload, etc.), add it to the canonical descriptions
  layer or to `Field(description=...)` first. Atlas should not be a
  place where new docs accumulate independent of the runtime code.
- The description-completeness lint (`tests/test_atlas_descriptions.py`)
  is a pre-merge gate. New CallType/MoveType/PageType etc. without
  prose fails CI.
- Avoid hand-curating cross-references that can be derived. Cross-refs
  in atlas should fall out of the registry, not be maintained by hand.

## Atlas as its own debugging surface

Atlas isn't just *for* humans — it's a useful surface for **agents
investigating rumil's behavior**. The trajectory-ergonomics pattern that's
worked well so far:

1. Spawn an agent (general-purpose or Explore) with a focused brief like
   "use atlas to investigate question X's trajectory; tell me what's
   ergonomically broken or missing." Hand it the relevant atlas URLs.
2. The agent navigates atlas like a human would, hits the same friction
   points (missing fields, silently-ignored filters, scratch pollution,
   cryptic labels), and reports them.
3. Each reported item becomes a concrete fix: a missing aggregation, a
   prose label, a structured-output gap in a prompt.

This pattern produced bugs 1–9 in the chrome-session pass and findings
(a)–(f) in the trajectory ergonomics pass. The findings tend to surface
two kinds of issues at once: **atlas display gaps** (we have the data
but don't show it) and **upstream data gaps** (we don't actually capture
what we need — null `credence`, null consideration `direction`, etc.).
The latter are the more valuable ones; atlas just makes them visible.

When you find such an upstream gap, the fix usually doesn't live in
atlas — it lives in a move payload schema, a prompt file, or a closing
review. Resist the urge to paper over with FE filtering.

## What atlas is not

- Not a replacement for `/traces` — atlas annotates runs in workflow
  context; `/traces` is the canonical per-call deep-dive.
- Not a wiki — atlas content is generated; if you want a free-form note,
  put it in the prompt file or the canonical description, not in atlas.
- Not coupled to versus's UI — versus's provenance axes are atlas-
  shaped (declarative metadata feeding a UI) and may eventually fold
  in as a sibling taxonomy, but for now atlas covers rumil's
  orchestrator/research surface and versus covers its own.

## Auth

The atlas router is admin-gated (`Depends(require_admin)` at the
router level, mirroring `versus_router` and `forks_router`). Atlas
exposes operator-level surfaces — registry internals, run-level
telemetry, prompt content — that aren't intended for non-admin users.
Read-only is not the same as public.
