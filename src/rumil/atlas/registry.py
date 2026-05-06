"""Build atlas registry views from live MoveDef / DispatchDef / preset / prompt sources.

Pure read-only introspection. Output types are in ``atlas.schemas`` so the
FastAPI router can re-export them as response_models without circular
imports.
"""

from __future__ import annotations

import inspect
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from rumil.atlas.descriptions import (
    CALL_TYPE_DESCRIPTIONS,
    PAGE_LAYER_DESCRIPTIONS,
    PAGE_TYPE_DESCRIPTIONS,
    WORKSPACE_DESCRIPTIONS,
)
from rumil.atlas.prompt_parts import (
    build_prompt_composition,
    get_prompt_sections,
    references_for_prompt_file,
)
from rumil.atlas.schemas import (
    CallTypeSummary,
    DispatchSummary,
    EnumSummary,
    JsonSchemaField,
    MoveSummary,
    PageTypeSummary,
    PromptDoc,
    RegistryRollup,
    WorkflowSummary,
)
from rumil.available_calls import AVAILABLE_CALLS_PRESETS
from rumil.available_moves import PRESETS as AVAILABLE_MOVES_PRESETS
from rumil.calls.dispatches import (
    DISPATCH_DEFS,
    RECURSE_CLAIM_DISPATCH_DEF,
    RECURSE_DISPATCH_DEF,
    DispatchDef,
)
from rumil.models import CallType, MoveType, PageType
from rumil.moves.registry import MOVES
from rumil.prompts import PROMPTS_DIR


def _resolve_simple_type(schema: dict[str, Any]) -> str:
    if "type" in schema:
        t = schema["type"]
        if isinstance(t, list):
            return "|".join(t)
        return t
    if "$ref" in schema:
        return schema["$ref"].split("/")[-1]
    if "anyOf" in schema:
        return "|".join(_resolve_simple_type(opt) for opt in schema["anyOf"])
    return "any"


def _resolve_field_schema(
    field_schema: dict[str, Any],
    defs: Mapping[str, dict[str, Any]] | None = None,
) -> tuple[str, str | None, str | None]:
    """Return (type, items_type, items_ref) for a JSON-schema field.

    Handles direct types, refs, anyOf nullables, and array items.
    """
    if "anyOf" in field_schema:
        non_null = [o for o in field_schema["anyOf"] if o.get("type") != "null"]
        if len(non_null) == 1:
            return _resolve_field_schema(non_null[0], defs)
    t = field_schema.get("type")
    if t == "array":
        items = field_schema.get("items", {}) or {}
        items_type = _resolve_simple_type(items)
        items_ref: str | None = None
        if "$ref" in items:
            items_ref = items["$ref"].split("/")[-1]
        return "array", items_type, items_ref
    if isinstance(t, str):
        return t, None, None
    if "$ref" in field_schema:
        return field_schema["$ref"].split("/")[-1], None, None
    return _resolve_simple_type(field_schema), None, None


def _project_schema_fields(json_schema: dict[str, Any]) -> list[JsonSchemaField]:
    """Project a Pydantic-generated JSON schema into JsonSchemaField rows."""
    props = json_schema.get("properties", {}) or {}
    required = set(json_schema.get("required", []) or [])
    defs = json_schema.get("$defs", {}) or {}
    out: list[JsonSchemaField] = []
    for name, fs in props.items():
        type_str, items_type, items_ref = _resolve_field_schema(fs, defs)
        out.append(
            JsonSchemaField(
                name=name,
                type=type_str,
                description=fs.get("description", "") or "",
                required=name in required,
                default=fs.get("default"),
                enum=fs.get("enum"),
                items_type=items_type,
                items_ref=items_ref,
                minimum=fs.get("minimum"),
                maximum=fs.get("maximum"),
            )
        )
    return out


def _move_used_in_call_types() -> dict[MoveType, set[CallType]]:
    """Across all available-moves presets: which CallTypes admit each MoveType."""
    out: dict[MoveType, set[CallType]] = {mt: set() for mt in MoveType}
    for preset in AVAILABLE_MOVES_PRESETS.values():
        for call_type, moves in preset.items():
            for m in moves:
                out.setdefault(m, set()).add(call_type)
    return out


def _move_used_in_presets() -> dict[MoveType, set[str]]:
    out: dict[MoveType, set[str]] = {mt: set() for mt in MoveType}
    for preset_name, preset in AVAILABLE_MOVES_PRESETS.items():
        for moves in preset.values():
            for m in moves:
                out.setdefault(m, set()).add(preset_name)
    return out


def _move_code_path(move_type: MoveType) -> str:
    """Best-effort: locate the moves/<file>.py that defines the MoveDef."""
    move = MOVES.get(move_type)
    if not move:
        return ""
    try:
        src_file = inspect.getsourcefile(move.execute) or ""
    except TypeError:
        return ""
    if not src_file:
        return ""
    repo_root = Path(__file__).resolve().parents[3]
    try:
        return str(Path(src_file).resolve().relative_to(repo_root))
    except ValueError:
        return src_file


def build_move_summaries() -> list[MoveSummary]:
    used_in = _move_used_in_call_types()
    used_in_presets = _move_used_in_presets()
    out: list[MoveSummary] = []
    for move_type, move in MOVES.items():
        schema = move.schema.model_json_schema()
        out.append(
            MoveSummary(
                move_type=move_type.value,
                name=move.name,
                description=move.description,
                fields=_project_schema_fields(schema),
                used_in_call_types=sorted(ct.value for ct in used_in.get(move_type, set())),
                used_in_presets=sorted(used_in_presets.get(move_type, set())),
                code_path=_move_code_path(move_type),
                raw_schema=schema,
            )
        )
    out.sort(key=lambda m: m.move_type)
    return out


def build_dispatch_summaries() -> list[DispatchSummary]:
    out: list[DispatchSummary] = []
    for call_type, ddef in DISPATCH_DEFS.items():
        schema = ddef.schema.model_json_schema()
        out.append(
            DispatchSummary(
                call_type=call_type.value,
                name=ddef.name,
                description=ddef.description,
                fields=_project_schema_fields(schema),
                is_recurse=False,
                raw_schema=schema,
            )
        )
    for ddef, label in (
        (RECURSE_DISPATCH_DEF, "recurse"),
        (RECURSE_CLAIM_DISPATCH_DEF, "recurse_claim"),
    ):
        schema = ddef.schema.model_json_schema()
        out.append(
            DispatchSummary(
                call_type=label,
                name=ddef.name,
                description=ddef.description,
                fields=_project_schema_fields(schema),
                is_recurse=True,
                raw_schema=schema,
            )
        )
    out.sort(key=lambda d: (not d.is_recurse, d.call_type))
    out = [d for d in out if not d.is_recurse] + [d for d in out if d.is_recurse]
    return out


def _moves_by_preset_for_call(call_type: CallType) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    for preset_name, preset in AVAILABLE_MOVES_PRESETS.items():
        if call_type in preset:
            out[preset_name] = [m.value for m in preset[call_type]]
    return out


def _existing_prompt_files_for_call(call_type: CallType) -> list[str]:
    """Return prompts/*.md files that almost certainly belong to this call type.

    The standard convention is ``{call_type.value}.md``; some calls also
    use call-specific multi-file prompts (e.g. ``two_phase_*``,
    ``claim_investigation_*``). Best-effort: prefix-match.
    """
    files: set[str] = set()
    primary = f"{call_type.value}.md"
    if (PROMPTS_DIR / primary).exists():
        files.add(primary)
    if call_type is CallType.PRIORITIZATION:
        for p in PROMPTS_DIR.glob("*prioritization*.md"):
            files.add(p.name)
    if call_type is CallType.GLOBAL_PRIORITIZATION:
        for p in PROMPTS_DIR.glob("global_prio*.md"):
            files.add(p.name)
    return sorted(files)


_RUNNER_INDEX_CACHE: dict[CallType, type] | None = None


def _runner_index() -> dict[CallType, type]:
    """Index every concrete CallRunner subclass by its declared call_type."""
    global _RUNNER_INDEX_CACHE
    if _RUNNER_INDEX_CACHE is not None:
        return _RUNNER_INDEX_CACHE
    import importlib
    import pkgutil

    import rumil.calls as calls_pkg
    from rumil.calls.stages import CallRunner

    for mod_info in pkgutil.iter_modules(calls_pkg.__path__):
        try:
            importlib.import_module(f"rumil.calls.{mod_info.name}")
        except Exception:
            continue

    index: dict[CallType, type] = {}

    def _walk(cls: type) -> None:
        for sub in cls.__subclasses__():
            ct = getattr(sub, "call_type", None)
            if isinstance(ct, CallType) and ct not in index:
                index[ct] = sub
            _walk(sub)

    _walk(CallRunner)
    _RUNNER_INDEX_CACHE = index
    return index


def _runner_attrs_for_call(
    call_type: CallType,
) -> tuple[str | None, str | None, str | None, str | None]:
    """Return (runner_class, ctx_builder, ws_updater, closer) for a CallType.

    Reads the calls package's CallRunner subclass for this call type, when
    available. Returns Nones when no runner exists (e.g. for PRIORITIZATION,
    which doesn't follow the CallRunner pattern).
    """
    cls = _runner_index().get(call_type)
    if cls is None:
        return None, None, None, None
    return (
        cls.__name__,
        getattr(getattr(cls, "context_builder_cls", None), "__name__", None),
        getattr(getattr(cls, "workspace_updater_cls", None), "__name__", None),
        getattr(getattr(cls, "closing_reviewer_cls", None), "__name__", None),
    )


def build_call_type_summaries() -> list[CallTypeSummary]:
    dispatch_by_call: dict[CallType, DispatchDef] = dict(DISPATCH_DEFS)
    out: list[CallTypeSummary] = []
    for call_type in CallType:
        ddef = dispatch_by_call.get(call_type)
        runner_cls, ctx_b, ws_up, closer = _runner_attrs_for_call(call_type)
        out.append(
            CallTypeSummary(
                call_type=call_type.value,
                description=CALL_TYPE_DESCRIPTIONS.get(call_type, ""),
                has_dispatch=ddef is not None,
                dispatch_name=ddef.name if ddef else None,
                prompt_files=_existing_prompt_files_for_call(call_type),
                moves_by_preset=_moves_by_preset_for_call(call_type),
                runner_class=runner_cls,
                context_builder=ctx_b,
                workspace_updater=ws_up,
                closing_reviewer=closer,
                composition=build_prompt_composition(call_type.value),
            )
        )
    out.sort(key=lambda c: c.call_type)
    return out


def build_page_type_summaries() -> list[PageTypeSummary]:
    return [
        PageTypeSummary(
            page_type=pt.value,
            description=PAGE_TYPE_DESCRIPTIONS.get(pt, ""),
        )
        for pt in PageType
    ]


def build_workspace_enum() -> list[EnumSummary]:
    return [
        EnumSummary(name="Workspace", value=ws.value, description=desc)
        for ws, desc in WORKSPACE_DESCRIPTIONS.items()
    ]


def build_layer_enum() -> list[EnumSummary]:
    return [
        EnumSummary(name="PageLayer", value=ly.value, description=desc)
        for ly, desc in PAGE_LAYER_DESCRIPTIONS.items()
    ]


def list_prompt_files() -> list[str]:
    return sorted(p.name for p in PROMPTS_DIR.glob("*.md"))


def get_prompt_doc(name: str) -> PromptDoc | None:
    if not name.endswith(".md"):
        name = f"{name}.md"
    path = PROMPTS_DIR / name
    if not path.exists():
        return None
    content = path.read_text(encoding="utf-8")
    from rumil.atlas.history import content_hash_for_file

    return PromptDoc(
        name=name,
        path=str(path.relative_to(Path(__file__).resolve().parents[3])),
        content=content,
        char_count=len(content),
        content_hash=content_hash_for_file(path),
        referenced_by=_prompt_referenced_by(name),
        sections=get_prompt_sections(name),
        used_in_compositions=references_for_prompt_file(name),
    )


def _prompt_referenced_by(name: str) -> list[str]:
    """Reverse-lookup: which call types' summaries point at this prompt."""
    base = name.removesuffix(".md")
    out: list[str] = []
    for call_type in CallType:
        if base == call_type.value:
            out.append(call_type.value)
    if base.startswith("two_phase_"):
        out.append("workflow:two_phase")
    if base.startswith("claim_investigation"):
        out.append("workflow:claim_investigation")
    return sorted(set(out))


def build_registry_rollup(workflow_summaries: Sequence[WorkflowSummary]) -> RegistryRollup:
    moves = build_move_summaries()
    dispatches = build_dispatch_summaries()
    call_types = build_call_type_summaries()
    page_types = build_page_type_summaries()
    prompts = list_prompt_files()
    presets = {
        name: [m.value for vs in preset.values() for m in vs]
        for name, preset in AVAILABLE_MOVES_PRESETS.items()
    }
    return RegistryRollup(
        n_moves=len(moves),
        n_dispatches=len(dispatches),
        n_call_types=len(call_types),
        n_page_types=len(page_types),
        n_workflows=len(workflow_summaries),
        n_prompt_files=len(prompts),
        move_summaries=moves,
        dispatch_summaries=dispatches,
        call_type_summaries=call_types,
        page_type_summaries=page_types,
        workflow_summaries=list(workflow_summaries),
        presets=presets,
        available_calls_presets=sorted(AVAILABLE_CALLS_PRESETS.keys()),
    )
