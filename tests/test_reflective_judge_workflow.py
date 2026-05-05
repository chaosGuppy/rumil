"""Tests for ``ReflectiveJudgeWorkflow``.

Heavy mocking — the workflow fires three real ``text_call`` LLMs (read,
reflect, verdict) per pair; running for real is wasteful for the
contract-shape tests these focus on. Tests cover:

- Class-attr / protocol contract (``Workflow`` runtime-checkable,
  ``produces_artifact=True``, ``code_paths``, ``name``).
- Constructor validation (dimension_body required and non-empty;
  empty prompt files rejected).
- Fingerprint shape and forking — every constructor knob and every
  prompt's content must change the fingerprint, plus the
  introspection check.
- Setup seeds the fixed budget of 3.

End-to-end ``run()`` is not exercised here; bridge / runner integration
tests cover that path once the bridge is wired.
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from rumil.orchestrators.reflective_judge import (
    _DEFAULT_READ_PROMPT,
    ReflectiveJudgeWorkflow,
)
from rumil.settings import override_settings
from rumil.versus_workflow import Workflow

_SAMPLE_DIMENSION_BODY = (
    Path(__file__).resolve().parents[1] / "src" / "rumil" / "prompts" / "versus-general-quality.md"
).read_text(encoding="utf-8")


@pytest.fixture(autouse=True)
def _stub_rumil_model_override():
    """``_resolve_model`` requires either a per-role constructor kwarg
    or ``rumil_model_override`` set in settings (the production
    ``run_versus`` path sets the latter). Tests construct workflows
    directly without per-role models; stub the override globally so
    construction doesn't depend on settings state.
    """
    with override_settings(rumil_model_override="claude-sonnet-4-6"):
        yield


def test_workflow_class_attrs():
    wf = ReflectiveJudgeWorkflow(dimension_body=_SAMPLE_DIMENSION_BODY)
    assert wf.name == "reflective_judge"
    assert wf.produces_artifact is True
    assert isinstance(wf.code_paths, tuple)
    assert "src/rumil/orchestrators/reflective_judge.py" in wf.code_paths


def test_workflow_satisfies_runtime_protocol():
    wf = ReflectiveJudgeWorkflow(dimension_body="x")
    assert isinstance(wf, Workflow)


def test_constructor_rejects_empty_dimension_body():
    with pytest.raises(ValueError, match="dimension_body"):
        ReflectiveJudgeWorkflow(dimension_body="")


def test_constructor_rejects_whitespace_only_dimension_body():
    with pytest.raises(ValueError, match="dimension_body"):
        ReflectiveJudgeWorkflow(dimension_body="   \n\t  ")


def test_fingerprint_shape():
    wf = ReflectiveJudgeWorkflow(dimension_body=_SAMPLE_DIMENSION_BODY)
    fp = wf.fingerprint()
    assert fp["kind"] == "reflective_judge"
    for key in (
        "dimension_body_hash",
        "read_prompt_hash",
        "reflect_prompt_hash",
        "verdict_prompt_hash",
    ):
        v = fp[key]
        assert isinstance(v, str)
        assert len(v) == 8
        int(v, 16)


def test_fingerprint_changes_on_reader_model():
    a = ReflectiveJudgeWorkflow(dimension_body=_SAMPLE_DIMENSION_BODY).fingerprint()
    b = ReflectiveJudgeWorkflow(
        dimension_body=_SAMPLE_DIMENSION_BODY, reader_model="claude-opus-4-7"
    ).fingerprint()
    assert a != b


def test_fingerprint_changes_on_reflector_model():
    a = ReflectiveJudgeWorkflow(dimension_body=_SAMPLE_DIMENSION_BODY).fingerprint()
    b = ReflectiveJudgeWorkflow(
        dimension_body=_SAMPLE_DIMENSION_BODY, reflector_model="claude-opus-4-7"
    ).fingerprint()
    assert a != b


def test_fingerprint_changes_on_verdict_model():
    a = ReflectiveJudgeWorkflow(dimension_body=_SAMPLE_DIMENSION_BODY).fingerprint()
    b = ReflectiveJudgeWorkflow(
        dimension_body=_SAMPLE_DIMENSION_BODY, verdict_model="claude-opus-4-7"
    ).fingerprint()
    assert a != b


def test_fingerprint_changes_on_dimension_body():
    a = ReflectiveJudgeWorkflow(dimension_body="rubric one").fingerprint()
    b = ReflectiveJudgeWorkflow(dimension_body="rubric two").fingerprint()
    assert a["dimension_body_hash"] != b["dimension_body_hash"]
    assert a != b


def test_prompt_path_with_default_content_fingerprints_same(tmp_path):
    """A path that points to the exact default text should fingerprint
    identically to construction without the path. Confirms the hash
    is over content, not path string.
    """
    p = tmp_path / "read_clone.md"
    p.write_text(_DEFAULT_READ_PROMPT, encoding="utf-8")
    default_fp = ReflectiveJudgeWorkflow(dimension_body=_SAMPLE_DIMENSION_BODY).fingerprint()
    overridden_fp = ReflectiveJudgeWorkflow(
        dimension_body=_SAMPLE_DIMENSION_BODY, read_prompt_path=p
    ).fingerprint()
    assert default_fp["read_prompt_hash"] == overridden_fp["read_prompt_hash"]


def test_prompt_path_with_custom_content_changes_fingerprint(tmp_path):
    p = tmp_path / "read_custom.md"
    p.write_text("YOU ARE A SAMPLE READ PROMPT WITH DISTINCT CONTENT.", encoding="utf-8")
    default_fp = ReflectiveJudgeWorkflow(dimension_body=_SAMPLE_DIMENSION_BODY).fingerprint()
    overridden_fp = ReflectiveJudgeWorkflow(
        dimension_body=_SAMPLE_DIMENSION_BODY, read_prompt_path=p
    ).fingerprint()
    assert default_fp["read_prompt_hash"] != overridden_fp["read_prompt_hash"]


def test_empty_prompt_file_rejected(tmp_path):
    p = tmp_path / "empty.md"
    p.write_text("", encoding="utf-8")
    with pytest.raises(ValueError, match="empty"):
        ReflectiveJudgeWorkflow(dimension_body=_SAMPLE_DIMENSION_BODY, read_prompt_path=p)


def test_whitespace_only_prompt_file_rejected(tmp_path):
    p = tmp_path / "whitespace.md"
    p.write_text("   \n\n\t   ", encoding="utf-8")
    with pytest.raises(ValueError, match="empty"):
        ReflectiveJudgeWorkflow(dimension_body=_SAMPLE_DIMENSION_BODY, reflect_prompt_path=p)


@pytest.mark.asyncio
async def test_setup_seeds_budget_of_three():
    db = MagicMock()
    db.init_budget = AsyncMock()
    wf = ReflectiveJudgeWorkflow(dimension_body=_SAMPLE_DIMENSION_BODY)
    await wf.setup(db, "q-1")
    db.init_budget.assert_awaited_once_with(3)


_OPAQUE_TO_FINGERPRINT = {
    "name",
    "code_paths",
    "produces_artifact",
    "relevant_settings",
    "last_status",
    "dimension_body",
    # Path attrs are pure telemetry; identical content via different
    # paths fingerprints the same via *_prompt_hash.
    "read_prompt_path",
    "reflect_prompt_path",
    "verdict_prompt_path",
}


def _public_attrs(workflow: object) -> set[str]:
    out: set[str] = set()
    for attr in dir(workflow):
        if attr.startswith("_"):
            continue
        value = getattr(workflow, attr)
        if callable(value):
            continue
        out.add(attr)
    return out


def _fingerprint_keys_normalized(fp: Mapping[str, object]) -> set[str]:
    keys = {k.removeprefix("settings.").removesuffix("_hash") for k in fp}
    if "kind" in keys:
        keys.add("name")
    # dimension_body_hash → dimension_body covers the dimension_body attr
    # via the opaque set entry above (we don't surface it raw — it's a
    # large rubric, the hash is what fingerprints).
    return keys


def test_workflow_fingerprint_covers_all_public_fields():
    """Catches drift: a new ``self.foo = ...`` knob in __init__ that's
    not folded into ``fingerprint()`` would silently let runs dedup
    against each other when they shouldn't.
    """
    wf = ReflectiveJudgeWorkflow(dimension_body=_SAMPLE_DIMENSION_BODY)
    fp = wf.fingerprint()
    public_attrs = _public_attrs(wf) - _OPAQUE_TO_FINGERPRINT
    fp_keys = _fingerprint_keys_normalized(fp)
    missing = public_attrs - fp_keys
    assert not missing, f"Workflow fields missing from fingerprint(): {missing}"
