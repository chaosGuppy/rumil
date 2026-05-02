"""Single-source builder for versus judge config + judge_model.

A judge "config" is the canonical-JSON record of every input the judge
saw — model id, sampling params, rendered prompt hash, tool prompt
hash, pair-surface hash, closer settings, and (for orch) a content
fingerprint of code paths whose behavior affects the run. The structured
dict + its sha256 are stored on every new row alongside the existing
``judge_model`` string.

This module is the single compose site for all three judge paths
(blind / ws / orch); ``versus.judge.build_blind_judge_config`` and the
``_compose`` lambdas in ``versus.rumil_judge.run_ws`` / ``run_orch`` all
delegate here.

The visible ``judge_model`` shape is unchanged from the pre-config era
so existing dedup keys keep matching; ``config_hash`` is additive
metadata that gives the UI / analysis layer a robust handle on
"are these rows from the same effective configuration."
"""

from __future__ import annotations

import hashlib
import json
import pathlib
from collections.abc import Sequence
from typing import Any, Literal

from rumil.model_config import ModelConfig

Variant = Literal["blind", "ws", "orch"]


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
    for p in sorted(root.glob(pattern)):
        h.update(p.name.encode())
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


def compute_judge_code_fingerprint() -> dict[str, str]:
    """Bridge + orchestrator + calls + prompts content fingerprint,
    used identically by ws and orch judgments.

    One entry per fingerprint directory (collapsed to a single sha over
    its files) plus one entry per individual file in
    :data:`versus.versions.JUDGE_CODE_FINGERPRINT_FILES`. Read once at
    plan time; stable for a single ``run_ws`` / ``run_orch`` invocation.
    """
    from versus.versions import JUDGE_CODE_FINGERPRINT_DIRS, JUDGE_CODE_FINGERPRINT_FILES

    out: dict[str, str] = {}
    for rel_dir, pattern in JUDGE_CODE_FINGERPRINT_DIRS:
        out[rel_dir] = _dir_content_sha(rel_dir, pattern)
    out.update(compute_file_fingerprint(JUDGE_CODE_FINGERPRINT_FILES))
    return out


def compute_config_hash(config: dict[str, Any]) -> str:
    """Canonical-JSON sha256 of the structured config, hex-truncated to 16.

    16 hex chars (64 bits) is enough to distinguish configurations
    without cluttering rows; collision probability across the few
    thousand judgments versus produces is effectively zero.
    """
    blob = json.dumps(config, sort_keys=True, default=str)
    return hashlib.sha256(blob.encode()).hexdigest()[:16]


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
    """Build the structured config + its hash + the display judge_model.

    Returns ``(config, config_hash, judge_model)``. Callers write all
    three onto each new judgment row; ``config_hash`` is the dedup
    primitive, ``judge_model`` is a short human-readable display
    string.

    ``model_config`` is the full rumil ``ModelConfig`` versus applied
    on the wire — sampling, thinking, effort, max_thinking_tokens,
    service_tier. Stored canonically as a nested ``model_config`` dict
    on the row; the dedup hash forks naturally on any field change.
    Direct paths (blind) build it from the versus model registry;
    bridge paths (ws/orch) do too — single source of truth.

    Per-variant required args (asserted):
    - blind: ``model_config``, ``prompt_hash``
    - ws: blind + ``workspace_id``, ``tool_prompt_hash``, ``pair_surface_hash``,
      ``code_fingerprint``, ``workspace_state_hash``
    - orch: ws + ``budget``, ``closer_hash``
    """
    config: dict[str, Any] = {
        "variant": variant,
        "model": model,
        "dimension": dimension,
        "model_config": model_config.to_record_dict(),
        "prompts": {"shell_hash": prompt_hash},
    }
    if variant in ("ws", "orch"):
        if (
            workspace_id is None
            or tool_prompt_hash is None
            or pair_surface_hash is None
            or code_fingerprint is None
            or workspace_state_hash is None
        ):
            raise ValueError(
                f"variant={variant!r} requires workspace_id, tool_prompt_hash, "
                "pair_surface_hash, code_fingerprint, workspace_state_hash"
            )
        config["workspace_id"] = workspace_id
        config["tool_descriptions_hash"] = tool_prompt_hash
        config["pair_surface_hash"] = pair_surface_hash
        config["code_fingerprint"] = code_fingerprint
        config["workspace_state_hash"] = workspace_state_hash
    if variant == "orch":
        if budget is None or closer_hash is None:
            raise ValueError("variant='orch' requires budget, closer_hash")
        config["budget"] = budget
        config["closer_hash"] = closer_hash
    config_hash = compute_config_hash(config)
    judge_model = _derive_judge_model(config)
    return config, config_hash, judge_model


def _derive_judge_model(config: dict[str, Any]) -> str:
    """Render the display ``judge_model`` for a structured config.

    Now that dedup is keyed on ``config_hash``, ``judge_model`` is a
    purely human-readable handle: ``<path>:<model>:<dimension>:c<hash8>``.
    Stable for the same config, distinct for different ones, and short
    enough to grep without losing sleep.
    """
    variant = config["variant"]
    path = "blind" if variant == "blind" else f"rumil:{variant}"
    short = compute_config_hash(config)[:8]
    return f"{path}:{config['model']}:{config['dimension']}:c{short}"


def project_config_to_axes(
    config: dict[str, Any], *, config_hash: str | None = None
) -> dict[str, str]:
    """Project a structured config onto the same axis names that
    ``mainline.parse_judge_components`` derives from a flat
    ``judge_model`` string.

    Used by ``mainline.summarize_provenance`` so rows that carry a
    ``config`` dict feed the same per-axis counters that legacy
    flat-string rows do — keeping the UI / api shape stable across the
    transition.
    """
    variant = config["variant"]
    out: dict[str, str] = {
        "judge_path": "blind" if variant == "blind" else f"rumil:{variant}",
        "judge_base_model": config["model"],
        "judge_dimension": config["dimension"],
        "judge_prompt_hash": f"p{config['prompts']['shell_hash']}",
    }
    # The full ModelConfig hash collapses sampling/thinking/effort/etc
    # into one axis. Editing any field of the registry forks this hash
    # naturally, so cross-config rollups stay distinct.
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
        # Composite axis — see mainline._AXIS_DESCRIPTIONS["config_hash"]
        # for what makes it useful.
        out["config_hash"] = config_hash
    return out
