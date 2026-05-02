"""Single-source builder for versus run config + judge_model display.

A versus "config" is the canonical-JSON record of every input one
versus run saw — model id, the workflow's fingerprint (kind, budget,
relevant settings snapshot), the task's fingerprint (kind, dimension,
prompt/tool/pair/closer hashes for judge_pair), the cross-cutting
``model_config`` / ``workspace_id`` / ``workspace_state_hash`` /
``code_fingerprint``. The structured dict + its sha256 land on every
new row alongside the display-string ``judge_model``.

Renamed from ``judge_config.py`` in #424 — the central function is now
:func:`make_versus_config(workflow, task, inputs, ...)`. The old
:func:`make_judge_config(variant, ...)` API is preserved as a
back-compat shim so blind callers (``versus.judge``,
``versus.mainline``) keep working without forced edits in this PR;
deprecation lands in a follow-up.

Historical rows in versus_judgments.judge_inputs may be in either
shape — the older ``{variant, model, dimension, prompts, ...}`` flat
form, or the new ``{model, model_config, workflow, task, ...}`` nested
form. Read-side code (:func:`project_config_to_axes`,
:func:`versus.judge.judge_config_is_current`) accepts both so the UI
panel and staleness detector keep working across the transition.
"""

from __future__ import annotations

import hashlib
import json
import pathlib
from collections.abc import Mapping, Sequence
from typing import Any, Literal

from rumil.model_config import ModelConfig

Variant = Literal["blind", "orch", "reflective"]


def _repo_root() -> pathlib.Path:
    return pathlib.Path(__file__).resolve().parents[3]


def _file_content_sha(path: pathlib.Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()[:8]


def compute_file_fingerprint(paths: Sequence[str]) -> dict[str, str]:
    """Map ``relative_path -> sha256[:8]`` over each file's current content.

    Anchored at the rumil repo root so dict keys are stable across
    machines. Hashes the *on-disk content* (NOT the committed git blob)
    so dirty edits surface immediately. Missing paths are recorded as
    empty strings to make absence visible in config diffs rather than
    silently changing the dict shape.
    """
    root = _repo_root()
    return {rel: (_file_content_sha(p) if (p := root / rel).is_file() else "") for rel in paths}


def _dir_content_sha(rel_dir: str, pattern: str) -> str:
    """Sha256 over all ``rel_dir``-relative ``pattern`` matches' content,
    sorted by name so the result is reproducible. Empty string if the
    directory doesn't exist.

    Folds many files into a single hash so a directory of N files is
    one config key instead of N — keeps the config dict small while
    still detecting any constituent edit.
    """
    root = _repo_root() / rel_dir
    if not root.is_dir():
        return ""
    h = hashlib.sha256()
    # Filter to files: recursive globs (``**/*``) match directories too,
    # and read_bytes() on a directory raises. Sort by relative posix
    # path so the result is reproducible across runs and across patterns
    # that may surface nested files.
    files = sorted(p for p in root.glob(pattern) if p.is_file())
    for p in files:
        h.update(p.relative_to(root).as_posix().encode())
        h.update(b":")
        h.update(p.read_bytes())
        h.update(b"\n")
    return h.hexdigest()[:8]


async def compute_workspace_state_hash(db: Any) -> str:
    """Watermark identifying the baseline workspace state.

    For ws/orch judgments what matters is whether two runs read the
    same baseline. Pages cover most of the surface via the ``pages``
    query's ``active_only=True, include_hidden=False`` filter — a
    page that gets superseded or hidden disappears from the list and
    the count drops, forking the hash.

    Links need more care: in-place mutations like ``change_link_role``
    don't change count or created_at, so the watermark would miss
    them with just `(count, max_created_at)`. We fold a digest of
    each link's mutable fields (role, direction, strength, importance,
    reasoning, section, position) into the hash so a link role
    flipping shows up.

    Cost: same two DB queries as before (pages + links); the link
    digest is a few-pass walk over the already-loaded list.
    """
    pages = await db.get_pages(active_only=True, include_hidden=False)
    links = await db.get_all_links()
    page_max = max((p.created_at.isoformat() for p in pages), default="")
    link_max = max((ln.created_at.isoformat() for ln in links), default="")
    h = hashlib.sha256()
    h.update(f"{len(pages)}|{page_max}|{len(links)}|{link_max}".encode())
    h.update(b"\n---LINK-DETAILS---\n")
    for ln in sorted(links, key=lambda x: x.id):
        h.update(ln.id.encode())
        h.update(b"|")
        h.update(ln.role.value.encode())
        h.update(b"|")
        h.update((ln.direction.value if ln.direction else "").encode())
        h.update(b"|")
        h.update(f"{ln.strength}|{ln.importance}|{ln.position}".encode())
        h.update(b"|")
        h.update((ln.section or "").encode())
        h.update(b"|")
        h.update((ln.reasoning or "").encode())
        h.update(b"\n")
    return h.hexdigest()[:16]


def compute_shared_code_fingerprint() -> dict[str, str]:
    """Cross-cutting harness fingerprint — files every versus run
    touches regardless of which Workflow / Task is composed in.

    One entry per directory in
    :data:`versus.versions.SHARED_CODE_FINGERPRINT_DIRS` (collapsed to
    a single sha over its files) plus one entry per individual file in
    :data:`versus.versions.SHARED_CODE_FINGERPRINT_FILES`. Read once
    at plan time; stable for a single run.

    Pre-#425 this was ``compute_judge_code_fingerprint`` and covered
    every orchestrator + call + per-call prompt under one fat hash.
    Post-#425 those moved to per-Workflow ``code_paths`` and this
    fingerprint shrunk to the harness layer.
    """
    from versus.versions import SHARED_CODE_FINGERPRINT_DIRS, SHARED_CODE_FINGERPRINT_FILES

    out: dict[str, str] = {}
    for rel_dir, pattern in SHARED_CODE_FINGERPRINT_DIRS:
        out[rel_dir] = _dir_content_sha(rel_dir, pattern)
    out.update(compute_file_fingerprint(SHARED_CODE_FINGERPRINT_FILES))
    return out


# Back-compat alias for callers / tests still on the pre-#425 name.
# Removable once nothing imports it.
compute_judge_code_fingerprint = compute_shared_code_fingerprint


def compute_workflow_code_fingerprint(workflow: Any) -> dict[str, str]:
    """Hash each path in ``workflow.code_paths`` (relative to repo root).

    Returns ``{path: sha256[:8]}``. Individual files are hashed
    directly via :func:`compute_file_fingerprint` semantics; directory
    paths (keys ending with ``/``, or any path that resolves to a
    directory on disk) get a recursive ``*`` glob folded into a single
    sha via ``_dir_content_sha`` so a directory is one entry instead
    of N.

    Missing paths are recorded as empty strings — same treatment as
    :func:`compute_file_fingerprint`, so absence shows up in config
    diffs rather than silently dropping.
    """
    root = _repo_root()
    out: dict[str, str] = {}
    for rel in workflow.code_paths:
        target = root / rel
        if target.is_dir():
            # Recursive glob folded into one sha. Pattern ``**/*`` walks
            # nested files; ``_dir_content_sha`` already filters to the
            # given pattern and skips the directory entries themselves
            # (only ``read_bytes()`` succeeds on files, but we sort and
            # iterate paths and the glob matches files only).
            out[rel] = _dir_content_sha(rel, "**/*")
        elif target.is_file():
            out[rel] = _file_content_sha(target)
        else:
            out[rel] = ""
    return out


def compute_config_hash(config: Mapping[str, Any]) -> str:
    """Canonical-JSON sha256 of the structured config, hex-truncated to 16.

    16 hex chars (64 bits) is enough to distinguish configurations
    without cluttering rows; collision probability across the few
    thousand runs versus produces is effectively zero.
    """
    blob = json.dumps(dict(config), sort_keys=True, default=str)
    return hashlib.sha256(blob.encode()).hexdigest()[:16]


def make_versus_config(
    *,
    workflow,
    task,
    inputs,
    model: str,
    model_config: ModelConfig,
    workspace_id: str | None = None,
    workspace_state_hash: str | None = None,
    code_fingerprint: Mapping[str, str] | None = None,
) -> tuple[dict[str, Any], str, str]:
    """Build structured config + hash + display ``judge_model``.

    ``workflow.fingerprint()`` and ``task.fingerprint(inputs)`` carry
    the component-specific knobs. Cross-cutting fields
    (``workspace_id``, ``workspace_state_hash``, ``model_config``)
    are passed through; the code fingerprint is computed here from
    :func:`compute_shared_code_fingerprint` (harness layer) plus
    :func:`compute_workflow_code_fingerprint` (workflow's declared
    ``code_paths``) so callers don't have to assemble it.

    The two are stored as separate top-level fields
    (``shared_code_fingerprint`` / ``workflow_code_fingerprint``) for
    legibility — the projection layer folds both into the existing
    ``judge_code_fingerprint`` axis. ``code_fingerprint`` (legacy
    flat dict) is still accepted for shim callers that want to pin a
    specific value; when supplied it overrides both auto-computed
    fields and lands as a single ``code_fingerprint`` key for
    backward dict-shape compatibility.

    Returns ``(config, config_hash, judge_model)``. Callers write all
    three on each new judgment / completion row; ``config_hash`` is
    the dedup primitive, ``judge_model`` is the human-readable display
    handle.
    """
    config: dict[str, Any] = {
        "model": model,
        "model_config": model_config.to_record_dict(),
        "workflow": dict(workflow.fingerprint()),
        "task": dict(task.fingerprint(inputs)),
    }
    if workspace_id is not None:
        config["workspace_id"] = workspace_id
    if workspace_state_hash:
        config["workspace_state_hash"] = workspace_state_hash
    if code_fingerprint is not None:
        # Caller supplied a pre-built fingerprint (shim path / tests
        # that want a frozen value). Preserve the legacy flat shape so
        # historical hash invariants keep holding.
        config["code_fingerprint"] = dict(code_fingerprint)
    else:
        # Auto-compute. Skip both fields entirely for synthetic
        # workflows that opt out via ``code_paths == ()`` (e.g. the
        # blind shim) — keeps blind configs minimal and matches the
        # pre-#425 shape where blind rows didn't carry a code
        # fingerprint at all.
        if getattr(workflow, "code_paths", ()):
            config["shared_code_fingerprint"] = compute_shared_code_fingerprint()
            config["workflow_code_fingerprint"] = compute_workflow_code_fingerprint(workflow)
    config_hash = compute_config_hash(config)
    judge_model = _derive_judge_model_new(workflow=workflow, task=task, model=model, ch=config_hash)
    return config, config_hash, judge_model


def _derive_judge_model_new(*, workflow, task, model: str, ch: str) -> str:
    """Display ``judge_model`` for a new-shape config.

    Format: ``<task.name>/<workflow.name>:<model>:c<hash8>``. Stable
    for the same config, distinct for different ones. Examples:

    - ``judge_pair/two_phase:claude-opus-4-7:c2937f03b``
    - ``judge_pair/blind:claude-haiku-4-5:cabcd1234``
    """
    return f"{task.name}/{workflow.name}:{model}:c{ch[:8]}"


class _BlindShimWorkflow:
    """Synthetic 'blind' workflow used only by the back-compat shim.

    The blind path doesn't actually run an orchestrator — it's a single
    LLM call with no tools — but we need *something* with a
    ``fingerprint()`` to preserve the shape symmetry. ``name='blind'``
    distinguishes it from real workflows in display strings + axes.
    """

    name = "blind"
    code_paths: Sequence[str] = ()
    produces_artifact = False

    def fingerprint(self) -> Mapping[str, str | int | bool | None]:
        return {"kind": self.name}

    async def setup(self, db: Any, question_id: str) -> None:  # pragma: no cover - never called
        return None

    async def run(self, db: Any, question_id: str, broadcaster: Any) -> None:  # pragma: no cover
        return None


class _ShimJudgePairTaskBlind:
    """Slimmed JudgePair fingerprint for the blind shim path.

    Blind judgments don't go through the closer or the workspace tools,
    so the fingerprint only carries ``dimension`` + ``prompt_hash``
    (with_tools=False). Constructed inline by ``make_judge_config``
    when ``variant='blind'``; not exported.
    """

    name = "judge_pair"

    def __init__(self, *, dimension: str, prompt_hash: str) -> None:
        self.dimension = dimension
        self.prompt_hash = prompt_hash

    def fingerprint(self, _inputs: Any) -> Mapping[str, str | int | bool | None]:
        return {
            "kind": self.name,
            "dimension": self.dimension,
            "prompt_hash": self.prompt_hash,
        }


class _ShimJudgePairTaskOrch:
    """Slimmed JudgePair fingerprint for the orch shim path.

    Lets ``make_judge_config('orch', ...)`` reuse the new
    ``make_versus_config`` plumbing without forcing the caller to hold
    a real ``JudgePairTask`` instance. The hash invariants match
    :class:`versus.tasks.JudgePairTask.fingerprint`.
    """

    name = "judge_pair"

    def __init__(
        self,
        *,
        dimension: str,
        prompt_hash: str,
        tool_prompt_hash: str,
        pair_surface_hash: str,
        closer_hash: str,
    ) -> None:
        self.dimension = dimension
        self.prompt_hash = prompt_hash
        self.tool_prompt_hash = tool_prompt_hash
        self.pair_surface_hash = pair_surface_hash
        self.closer_hash = closer_hash

    def fingerprint(self, _inputs: Any) -> Mapping[str, str | int | bool | None]:
        return {
            "kind": self.name,
            "dimension": self.dimension,
            "prompt_hash": self.prompt_hash,
            "tool_prompt_hash": self.tool_prompt_hash,
            "pair_surface_hash": self.pair_surface_hash,
            "closer_hash": self.closer_hash,
        }


class _ShimJudgePairTaskReflective:
    """Slimmed JudgePair fingerprint for the reflective shim path.

    Reflective uses no closer (the workflow produces the artifact
    directly) and no tools, so the fingerprint carries dimension +
    prompt_hash + pair_surface_hash only. Mirrors the orch shim's
    shape minus the closer_hash and tool_prompt_hash fields.
    """

    name = "judge_pair"

    def __init__(
        self,
        *,
        dimension: str,
        prompt_hash: str,
        pair_surface_hash: str,
    ) -> None:
        self.dimension = dimension
        self.prompt_hash = prompt_hash
        self.pair_surface_hash = pair_surface_hash

    def fingerprint(self, _inputs: Any) -> Mapping[str, str | int | bool | None]:
        return {
            "kind": self.name,
            "dimension": self.dimension,
            "prompt_hash": self.prompt_hash,
            "pair_surface_hash": self.pair_surface_hash,
        }


def make_judge_config(
    variant: Variant,
    *,
    model: str,
    dimension: str,
    model_config: ModelConfig,
    prompt_hash: str,
    tool_prompt_hash: str | None = None,
    pair_surface_hash: str | None = None,
    workspace_id: str | None = None,
    budget: int | None = None,
    closer_hash: str | None = None,
    code_fingerprint: dict[str, str] | None = None,
    workspace_state_hash: str | None = None,
) -> tuple[dict[str, Any], str, str]:
    """Back-compat shim — translates old kwargs into ``make_versus_config``.

    The old API took ``variant`` ∈ {"blind", "orch"} and a flat list of
    fields; the new API takes ``workflow`` + ``task`` objects. This
    shim reconstructs the workflow / task pair internally so blind
    callers (``versus.judge.build_blind_judge_config``,
    ``versus.mainline.current_values_summary``) and the orch caller
    (``versus.rumil_judge.run_orch``) keep working without forced
    edits in this PR.

    The returned dict shape changes vs. pre-#424 — that's deliberate;
    config_hash forks accordingly. Read-side code projects both old
    and new shapes onto the panel axes via :func:`project_config_to_axes`.

    Per-variant required args (asserted):
    - blind: ``model``, ``dimension``, ``model_config``, ``prompt_hash``
    - orch: blind + ``workspace_id``, ``tool_prompt_hash``,
      ``pair_surface_hash``, ``code_fingerprint``,
      ``workspace_state_hash``, ``budget``, ``closer_hash``
    """
    if variant == "blind":
        blind_workflow = _BlindShimWorkflow()
        blind_task = _ShimJudgePairTaskBlind(dimension=dimension, prompt_hash=prompt_hash)
        return make_versus_config(
            workflow=blind_workflow,
            task=blind_task,
            inputs=None,
            model=model,
            model_config=model_config,
        )
    if variant == "orch":
        if (
            workspace_id is None
            or tool_prompt_hash is None
            or pair_surface_hash is None
            or workspace_state_hash is None
            or budget is None
            or closer_hash is None
        ):
            raise ValueError(
                "variant='orch' requires workspace_id, tool_prompt_hash, "
                "pair_surface_hash, workspace_state_hash, budget, closer_hash"
            )
        # Local import to avoid a circular import: versus_workflow imports
        # the orchestrator, which transitively pulls in everything it touches.
        from rumil.versus_workflow import TwoPhaseWorkflow

        orch_workflow = TwoPhaseWorkflow(budget=budget)
        orch_task = _ShimJudgePairTaskOrch(
            dimension=dimension,
            prompt_hash=prompt_hash,
            tool_prompt_hash=tool_prompt_hash,
            pair_surface_hash=pair_surface_hash,
            closer_hash=closer_hash,
        )
        # code_fingerprint defaults to auto-compute (post-#425). Tests
        # / callers that want to pin a frozen value can still pass it
        # explicitly; that lands under the legacy flat
        # ``code_fingerprint`` key for hash compat.
        return make_versus_config(
            workflow=orch_workflow,
            task=orch_task,
            inputs=None,
            model=model,
            model_config=model_config,
            workspace_id=workspace_id,
            workspace_state_hash=workspace_state_hash,
            code_fingerprint=code_fingerprint,
        )
    if variant == "reflective":
        if pair_surface_hash is None:
            raise ValueError("variant='reflective' requires pair_surface_hash")
        # Local import to avoid the same circular-import risk as orch.
        from rumil.orchestrators.reflective_judge import ReflectiveJudgeWorkflow

        # The reflective workflow needs a non-empty dimension_body for
        # construction, but we only have its prompt_hash here. The hash
        # is what fingerprints the workflow; the body content is fixed
        # via that hash and the path the caller gave to the bridge. Use
        # a sentinel body — the fingerprint carries the real content
        # via dimension_body_hash on the orch path, but the reflective
        # workflow recomputes its hash from the body string. To keep
        # config_hash stable across runs that use the same dimension,
        # pass a deterministic sentinel; the actual body is never read
        # by make_versus_config (it only calls fingerprint()).
        reflective_workflow = ReflectiveJudgeWorkflow(
            dimension_body=f"<sentinel:{prompt_hash}>",
        )
        reflective_task = _ShimJudgePairTaskReflective(
            dimension=dimension,
            prompt_hash=prompt_hash,
            pair_surface_hash=pair_surface_hash,
        )
        return make_versus_config(
            workflow=reflective_workflow,
            task=reflective_task,
            inputs=None,
            model=model,
            model_config=model_config,
            workspace_id=workspace_id,
            workspace_state_hash=workspace_state_hash,
            code_fingerprint=code_fingerprint,
        )
    raise ValueError(f"unknown variant: {variant!r}")


def _flat_variant(config: Mapping[str, Any]) -> str | None:
    """Return the legacy 'variant' string for old-shape rows; None for new shape.

    Old-shape rows have a top-level ``variant`` key. New-shape rows
    don't — they have ``workflow`` / ``task`` subdicts whose ``kind``
    fields collectively encode the variant.
    """
    return config.get("variant") if "variant" in config else None


def row_prompt_hash(config: Mapping[str, Any]) -> str | None:
    """Extract the rendered judge prompt hash from either dict shape.

    New-shape rows carry it on ``task.prompt_hash``; legacy rows on
    ``prompts.shell_hash``. Returns ``None`` when the field is missing
    (e.g. partially-built configs); callers should render that case
    gracefully rather than KeyError.
    """
    if "task" in config:
        return (config.get("task") or {}).get("prompt_hash")
    return (config.get("prompts") or {}).get("shell_hash")


def is_rumil_row(config: Mapping[str, Any], judge_model: str) -> bool:
    """True iff the judgment was produced by a rumil workflow (not single-turn blind).

    For new-shape rows this is read from ``workflow.kind`` (anything
    other than ``"blind"`` is a rumil workflow). Legacy rows pre-#424
    encoded variant in the ``judge_model`` display string —
    ``rumil:ws:...`` / ``rumil:orch:...`` for rumil-produced rows,
    ``blind:...`` / ``<provider>/<model>`` for blind — so we fall back
    to the prefix check there.
    """
    if "workflow" in config:
        return (config.get("workflow") or {}).get("kind") != "blind"
    return judge_model.startswith("rumil:")


def _new_variant_path(config: Mapping[str, Any]) -> str:
    """Display-axis 'judge_path' for a new-shape config.

    Mirrors the legacy ``variant`` axis values: ``blind`` for blind
    workflows, ``rumil:<workflow.kind>`` for everything else (so
    ``rumil:two_phase`` reads sensibly even though the legacy axis
    used ``rumil:orch``).
    """
    workflow = config.get("workflow") or {}
    kind = workflow.get("kind")
    if kind == "blind":
        return "blind"
    return f"rumil:{kind}"


def project_config_to_axes(
    config: Mapping[str, Any], *, config_hash: str | None = None
) -> dict[str, str]:
    """Project a structured config onto the same axis names that
    ``mainline.parse_judge_components`` derives from a flat
    ``judge_model`` string.

    Used by ``mainline.summarize_provenance`` so rows that carry a
    ``config`` dict feed the same per-axis counters that legacy
    flat-string rows do — keeping the UI / api shape stable across
    every transition (legacy flat string → ``judge_config`` dict →
    new ``versus_config`` dict).

    Handles both old and new dict shapes:

    - **Old shape** (``judge_inputs`` written before #424): top-level
      ``variant`` key, flat fields.
    - **New shape** (post-#424 ``versus_config``): ``workflow`` /
      ``task`` subdicts with ``kind`` fields, cross-cutting top-level
      keys for the rest.
    """
    if _flat_variant(config) is not None:
        return _project_legacy_config_to_axes(config, config_hash=config_hash)
    return _project_new_config_to_axes(config, config_hash=config_hash)


def _project_legacy_config_to_axes(
    config: Mapping[str, Any], *, config_hash: str | None = None
) -> dict[str, str]:
    """Old-shape projection — kept for historical rows in versus_judgments.

    Read-side only; new rows go through :func:`_project_new_config_to_axes`.
    """
    variant = config["variant"]
    out: dict[str, str] = {
        "judge_path": "blind" if variant == "blind" else f"rumil:{variant}",
        "judge_base_model": config["model"],
        "judge_dimension": config["dimension"],
        "judge_prompt_hash": f"p{config['prompts']['shell_hash']}",
    }
    if (mc := config.get("model_config")) is not None:
        mc_hash = hashlib.sha256(json.dumps(mc, sort_keys=True, default=str).encode()).hexdigest()[
            :8
        ]
        out["judge_model_config_hash"] = f"m{mc_hash}"
    if variant in ("ws", "orch"):
        out["judge_workspace_id"] = config["workspace_id"]
        out["judge_tool_hash"] = f"t{config['tool_descriptions_hash']}"
        out["judge_pair_hash"] = f"q{config['pair_surface_hash']}"
        ws_state = config.get("workspace_state_hash")
        if ws_state:
            out["judge_workspace_state_hash"] = f"w{ws_state[:8]}"
        fp = config.get("code_fingerprint")
        if isinstance(fp, dict) and fp:
            fp_blob = json.dumps(fp, sort_keys=True, default=str)
            fp_hash = hashlib.sha256(fp_blob.encode()).hexdigest()[:8]
            out["judge_code_fingerprint"] = f"f{fp_hash}"
    if variant == "orch":
        out["judge_budget"] = f"b{config['budget']}"
        out["judge_closer_hash"] = f"c{config['closer_hash']}"
    if config_hash:
        out["config_hash"] = config_hash
    return out


def _project_new_config_to_axes(
    config: Mapping[str, Any], *, config_hash: str | None = None
) -> dict[str, str]:
    """Project a new-shape (``workflow``/``task``) config onto axes."""
    workflow = config.get("workflow") or {}
    task = config.get("task") or {}
    out: dict[str, str] = {
        "judge_path": _new_variant_path(config),
        "judge_base_model": config["model"],
        "judge_dimension": task.get("dimension", ""),
    }
    if (ph := task.get("prompt_hash")) is not None:
        out["judge_prompt_hash"] = f"p{ph}"
    if (mc := config.get("model_config")) is not None:
        mc_hash = hashlib.sha256(json.dumps(mc, sort_keys=True, default=str).encode()).hexdigest()[
            :8
        ]
        out["judge_model_config_hash"] = f"m{mc_hash}"
    if (ws := config.get("workspace_id")) is not None:
        out["judge_workspace_id"] = ws
    if (th := task.get("tool_prompt_hash")) is not None:
        out["judge_tool_hash"] = f"t{th}"
    if (qh := task.get("pair_surface_hash")) is not None:
        out["judge_pair_hash"] = f"q{qh}"
    if ws_state := config.get("workspace_state_hash"):
        out["judge_workspace_state_hash"] = f"w{ws_state[:8]}"
    # Fold both shapes into one axis: legacy flat ``code_fingerprint``
    # (shim path / pre-#425) and post-#425 split
    # ``shared_code_fingerprint`` + ``workflow_code_fingerprint`` both
    # render under ``judge_code_fingerprint``. The merge preserves
    # forking — any change to either component shows up in the hash.
    fp_components: dict[str, Any] = {}
    if isinstance(legacy_fp := config.get("code_fingerprint"), dict) and legacy_fp:
        fp_components["legacy"] = legacy_fp
    if isinstance(shared_fp := config.get("shared_code_fingerprint"), dict) and shared_fp:
        fp_components["shared"] = shared_fp
    if isinstance(workflow_fp := config.get("workflow_code_fingerprint"), dict) and workflow_fp:
        fp_components["workflow"] = workflow_fp
    if fp_components:
        fp_blob = json.dumps(fp_components, sort_keys=True, default=str)
        fp_hash = hashlib.sha256(fp_blob.encode()).hexdigest()[:8]
        out["judge_code_fingerprint"] = f"f{fp_hash}"
    if (budget := workflow.get("budget")) is not None:
        out["judge_budget"] = f"b{budget}"
    if (ch := task.get("closer_hash")) is not None:
        out["judge_closer_hash"] = f"c{ch}"
    if config_hash:
        out["config_hash"] = config_hash
    return out
