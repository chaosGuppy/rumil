"""Unit-level tests for axon — per-component, no full orchestrator run.

Covers:
- finalize payload validation
- DelegateRequest / DelegateConfig / FinalizeSchemaSpec / SystemPromptSpec validation
- AxonOrchestrator._validate_coupling_rule (static method)
- ArtifactStore.add / render_block / require_keys
- BudgetClock carve_child + record_exchange parent debiting
- build_initial_artifacts
- load_axon_config
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest
import yaml
from pydantic import ValidationError

from rumil.orchestrators.axon import (
    OPERATING_ASSUMPTIONS_KEY,
    ArtifactSeed,
    ArtifactStore,
    AxonConfig,
    AxonOrchestrator,
    BudgetClock,
    BudgetSpec,
    DelegateConfig,
    DelegateRequest,
    FinalizeSchemaSpec,
    OrchInputs,
    SystemPromptSpec,
    build_initial_artifacts,
    load_axon_config,
    validate_finalize_payload,
)


def _basic_finalize_schema() -> dict:
    return {
        "type": "object",
        "properties": {
            "answer": {"type": "string"},
            "score": {"type": "integer"},
        },
        "required": ["answer"],
        "additionalProperties": False,
    }


def test_validate_finalize_payload_missing_required():
    schema = _basic_finalize_schema()
    payload, err = validate_finalize_payload({"score": 1}, schema)
    assert payload is None
    assert err is not None
    assert "answer" in err


def test_validate_finalize_payload_unexpected_with_strict_additional_props():
    schema = _basic_finalize_schema()
    payload, err = validate_finalize_payload({"answer": "ok", "extra": "junk"}, schema)
    assert payload is None
    assert err is not None
    assert "extra" in err


def test_validate_finalize_payload_unexpected_allowed_when_no_strict_flag():
    schema = {
        "type": "object",
        "properties": {"answer": {"type": "string"}},
        "required": ["answer"],
    }
    valid = {"answer": "ok", "extra": "fine"}
    payload, err = validate_finalize_payload(valid, schema)
    assert err is None
    assert payload == valid


def test_validate_finalize_payload_valid_echoes():
    schema = _basic_finalize_schema()
    valid = {"answer": "ok", "score": 3}
    payload, err = validate_finalize_payload(valid, schema)
    assert err is None
    assert payload == valid


def test_validate_finalize_payload_none_input():
    payload, err = validate_finalize_payload(None, _basic_finalize_schema())
    assert payload is None
    assert err is not None


def test_delegate_request_budget_must_be_positive():
    with pytest.raises(ValidationError):
        DelegateRequest(intent="x", inherit_context=True, budget_usd=0)


def test_delegate_request_budget_negative_rejected():
    with pytest.raises(ValidationError):
        DelegateRequest(intent="x", inherit_context=True, budget_usd=-1)


def test_delegate_request_n_must_be_at_least_one():
    with pytest.raises(ValidationError):
        DelegateRequest(intent="x", inherit_context=True, budget_usd=1.0, n=0)


def test_delegate_request_minimum_valid():
    req = DelegateRequest(intent="x", inherit_context=False, budget_usd=0.5)
    assert req.n == 1


def test_delegate_config_write_artifact_requires_artifact_key():
    with pytest.raises(ValidationError, match="artifact_key"):
        DelegateConfig(
            max_rounds=3,
            finalize_schema=FinalizeSchemaSpec(ref="freeform_text"),
            side_effects=["write_artifact"],
            artifact_key=None,
            rationale="r",
        )


def test_delegate_config_artifact_key_requires_write_artifact_side_effect():
    with pytest.raises(ValidationError, match="write_artifact"):
        DelegateConfig(
            max_rounds=3,
            finalize_schema=FinalizeSchemaSpec(ref="freeform_text"),
            side_effects=[],
            artifact_key="dist1",
            rationale="r",
        )


def test_delegate_config_write_artifact_with_key_ok():
    cfg = DelegateConfig(
        max_rounds=3,
        finalize_schema=FinalizeSchemaSpec(ref="freeform_text"),
        side_effects=["write_artifact"],
        artifact_key="dist1",
        rationale="r",
    )
    assert cfg.artifact_key == "dist1"


def test_finalize_schema_spec_requires_exactly_one():
    with pytest.raises(ValidationError, match="exactly one"):
        FinalizeSchemaSpec()
    with pytest.raises(ValidationError, match="exactly one"):
        FinalizeSchemaSpec(ref="a", inline={"type": "object"})


def test_finalize_schema_spec_ref_only():
    spec = FinalizeSchemaSpec(ref="freeform_text")
    assert spec.ref == "freeform_text"
    assert spec.inline is None


def test_finalize_schema_spec_inline_only():
    schema = {"type": "object", "properties": {"x": {"type": "string"}}}
    spec = FinalizeSchemaSpec(inline=schema)
    assert spec.inline == schema
    assert spec.ref is None


def test_system_prompt_spec_requires_exactly_one():
    with pytest.raises(ValidationError, match="exactly one"):
        SystemPromptSpec()
    with pytest.raises(ValidationError, match="exactly one"):
        SystemPromptSpec(ref="a", inline="b")


def test_system_prompt_spec_ref_only():
    spec = SystemPromptSpec(ref="web_research")
    assert spec.ref == "web_research"


def test_system_prompt_spec_inline_only():
    spec = SystemPromptSpec(inline="you are a research assistant")
    assert spec.inline == "you are a research assistant"


def _make_request(*, inherit: bool) -> DelegateRequest:
    return DelegateRequest(
        intent="do x",
        inherit_context=inherit,
        budget_usd=1.0,
    )


def _make_config(
    *,
    system_prompt: SystemPromptSpec | None,
    tools: list[str] | None,
) -> DelegateConfig:
    return DelegateConfig(
        system_prompt=system_prompt,
        tools=tools,
        max_rounds=3,
        finalize_schema=FinalizeSchemaSpec(ref="freeform_text"),
        rationale="r",
    )


def test_coupling_rule_inherit_true_with_system_prompt_set_errors():
    req = _make_request(inherit=True)
    cfg = _make_config(system_prompt=SystemPromptSpec(inline="x"), tools=None)
    err = AxonOrchestrator._validate_coupling_rule(cfg, req)
    assert err is not None
    assert "system_prompt" in err


def test_coupling_rule_inherit_true_with_tools_set_errors():
    req = _make_request(inherit=True)
    cfg = _make_config(system_prompt=None, tools=["load_page"])
    err = AxonOrchestrator._validate_coupling_rule(cfg, req)
    assert err is not None
    assert "tools" in err


def test_coupling_rule_inherit_true_with_both_none_ok():
    req = _make_request(inherit=True)
    cfg = _make_config(system_prompt=None, tools=None)
    err = AxonOrchestrator._validate_coupling_rule(cfg, req)
    assert err is None


def test_coupling_rule_inherit_false_with_system_prompt_none_errors():
    req = _make_request(inherit=False)
    cfg = _make_config(system_prompt=None, tools=["load_page"])
    err = AxonOrchestrator._validate_coupling_rule(cfg, req)
    assert err is not None
    assert "system_prompt" in err


def test_coupling_rule_inherit_false_with_tools_none_errors():
    req = _make_request(inherit=False)
    cfg = _make_config(system_prompt=SystemPromptSpec(inline="sys"), tools=None)
    err = AxonOrchestrator._validate_coupling_rule(cfg, req)
    assert err is not None
    assert "tools" in err


def test_coupling_rule_inherit_false_with_both_set_ok():
    req = _make_request(inherit=False)
    cfg = _make_config(system_prompt=SystemPromptSpec(inline="sys"), tools=["load_page"])
    err = AxonOrchestrator._validate_coupling_rule(cfg, req)
    assert err is None


def test_coupling_rule_inherit_false_with_empty_tools_ok():
    req = _make_request(inherit=False)
    cfg = _make_config(system_prompt=SystemPromptSpec(inline="sys"), tools=[])
    err = AxonOrchestrator._validate_coupling_rule(cfg, req)
    assert err is None


def test_artifact_store_add_collision_raises():
    store = ArtifactStore()
    store.add("k", "v1", produced_by="input")
    with pytest.raises(ValueError, match="already exists"):
        store.add("k", "v2", produced_by="input")


def test_artifact_store_add_and_get():
    store = ArtifactStore()
    store.add("dist1", "the body", produced_by="delegate-abc", round_idx=2)
    art = store.get("dist1")
    assert art is not None
    assert art.text == "the body"
    assert art.produced_by == "delegate-abc"
    assert art.round_idx == 2
    assert "dist1" in store


def test_artifact_store_render_block_format():
    store = ArtifactStore(seed={"pair_text": "PAIR"})
    block = store.render_block(["pair_text"])
    assert "## Artifacts" in block
    assert 'key="pair_text"' in block
    assert 'chars="4"' in block
    assert 'from="input"' in block
    assert "PAIR" in block
    assert "</artifact>" in block


def test_artifact_store_render_block_skips_missing_keys():
    store = ArtifactStore(seed={"a": "A"})
    block = store.render_block(["a", "missing"])
    assert "missing" not in block
    assert "A" in block


def test_artifact_store_require_keys_returns_missing():
    store = ArtifactStore(seed={"a": "A", "b": "B"})
    missing = store.require_keys(["a", "c", "d"])
    assert missing == ["c", "d"]


def test_artifact_store_require_keys_empty_when_all_present():
    store = ArtifactStore(seed={"a": "A"})
    assert store.require_keys(["a"]) == []


def _fake_usage(input_tokens: int, output_tokens: int):
    usage = MagicMock()
    usage.iterations = None
    usage.input_tokens = input_tokens
    usage.output_tokens = output_tokens
    usage.cache_creation_input_tokens = 0
    usage.cache_read_input_tokens = 0
    return usage


def test_budget_clock_carve_child_debits_parent():
    parent = BudgetClock(spec=BudgetSpec(max_cost_usd=10.0))
    child = parent.carve_child(3.0)
    child.record_exchange(_fake_usage(1_000_000, 0), "claude-haiku-4-5")
    assert child.cost_usd_used > 0
    assert parent.cost_usd_used == child.cost_usd_used


def test_budget_clock_carve_child_caps_at_remaining():
    parent = BudgetClock(spec=BudgetSpec(max_cost_usd=2.0))
    child = parent.carve_child(5.0)
    assert child.spec.max_cost_usd == 2.0


def test_budget_clock_carve_child_zero_or_negative_raises():
    parent = BudgetClock(spec=BudgetSpec(max_cost_usd=10.0))
    with pytest.raises(ValueError):
        parent.carve_child(0)
    with pytest.raises(ValueError):
        parent.carve_child(-1)


def test_budget_clock_record_exchange_no_parent():
    clock = BudgetClock(spec=BudgetSpec(max_cost_usd=10.0))
    clock.record_exchange(_fake_usage(1_000_000, 500_000), "claude-haiku-4-5")
    assert clock.cost_usd_used > 0


def test_build_initial_artifacts_operating_assumptions_seeded():
    inputs = OrchInputs(
        question="q",
        budget_usd=1.0,
        operating_assumptions="assume X",
    )
    seed = build_initial_artifacts(inputs)
    entry = seed[OPERATING_ASSUMPTIONS_KEY]
    assert isinstance(entry, ArtifactSeed)
    assert entry.text == "assume X"
    assert entry.render_inline is True


def test_build_initial_artifacts_strips_whitespace_assumptions():
    inputs = OrchInputs(question="q", budget_usd=1.0, operating_assumptions="  assume X  \n")
    seed = build_initial_artifacts(inputs)
    entry = seed[OPERATING_ASSUMPTIONS_KEY]
    assert isinstance(entry, ArtifactSeed)
    assert entry.text == "assume X"


def test_build_initial_artifacts_empty_assumptions_omitted():
    inputs = OrchInputs(question="q", budget_usd=1.0, operating_assumptions="   ")
    seed = build_initial_artifacts(inputs)
    assert OPERATING_ASSUMPTIONS_KEY not in seed


def test_build_initial_artifacts_merges_caller_seed():
    inputs = OrchInputs(
        question="q",
        budget_usd=1.0,
        operating_assumptions="A",
        artifacts={"pair_text": "P"},
    )
    seed = build_initial_artifacts(inputs)
    entry = seed[OPERATING_ASSUMPTIONS_KEY]
    assert isinstance(entry, ArtifactSeed)
    assert entry.text == "A"
    assert seed["pair_text"] == "P"


def test_build_initial_artifacts_collision_on_reserved_key_raises():
    inputs = OrchInputs(
        question="q",
        budget_usd=1.0,
        operating_assumptions="A",
        artifacts={OPERATING_ASSUMPTIONS_KEY: "from caller"},
    )
    with pytest.raises(ValueError, match="reserved"):
        build_initial_artifacts(inputs)


def test_build_initial_artifacts_caller_only_no_assumptions():
    inputs = OrchInputs(
        question="q",
        budget_usd=1.0,
        artifacts={"pair_text": "P"},
    )
    seed = build_initial_artifacts(inputs)
    assert seed == {"pair_text": "P"}


def test_build_initial_artifacts_caller_artifact_seed():
    inputs = OrchInputs(
        question="q",
        budget_usd=1.0,
        artifacts={
            "rubric": ArtifactSeed(
                text="judge per axes A, B, C",
                description="judging rubric",
                render_inline=True,
            )
        },
    )
    seed = build_initial_artifacts(inputs)
    entry = seed["rubric"]
    assert isinstance(entry, ArtifactSeed)
    assert entry.description == "judging rubric"
    assert entry.render_inline is True


def test_load_axon_config_valid_yaml(tmp_path: Path):
    sys_prompt = tmp_path / "sys.md"
    sys_prompt.write_text("you are a research agent")
    config_yaml = tmp_path / "research.yaml"
    config_yaml.write_text(
        yaml.safe_dump(
            {
                "name": "research",
                "main_model": "claude-haiku-4-5",
                "main_system_prompt_path": "sys.md",
                "direct_tools": ["load_page"],
                "finalize_schema_registry": {
                    "freeform_text": {
                        "type": "object",
                        "properties": {"answer": {"type": "string"}},
                        "required": ["answer"],
                        "additionalProperties": False,
                    }
                },
            }
        )
    )
    cfg = load_axon_config(config_yaml)
    assert cfg.name == "research"
    assert cfg.main_model == "claude-haiku-4-5"
    assert Path(cfg.main_system_prompt_path).is_absolute()
    assert Path(cfg.main_system_prompt_path).exists()
    assert cfg.direct_tools == ("load_page",)
    assert "freeform_text" in cfg.finalize_schema_registry


def test_load_axon_config_missing_required_field_raises(tmp_path: Path):
    config_yaml = tmp_path / "bad.yaml"
    config_yaml.write_text(yaml.safe_dump({"name": "x", "main_model": "claude-haiku-4-5"}))
    with pytest.raises(ValueError, match="main_system_prompt_path"):
        load_axon_config(config_yaml)


def test_load_axon_config_top_level_must_be_mapping(tmp_path: Path):
    config_yaml = tmp_path / "bad.yaml"
    config_yaml.write_text("- just\n- a\n- list\n")
    with pytest.raises(ValueError, match="mapping"):
        load_axon_config(config_yaml)


def test_load_axon_config_missing_file_raises(tmp_path: Path):
    with pytest.raises(FileNotFoundError):
        load_axon_config(tmp_path / "nonexistent.yaml")


def test_load_axon_config_relative_paths_resolved(tmp_path: Path):
    sub = tmp_path / "prompts"
    sub.mkdir()
    sys_prompt = sub / "main.md"
    sys_prompt.write_text("system prompt body")
    web_sys = sub / "web.md"
    web_sys.write_text("web research system")
    config_yaml = tmp_path / "research.yaml"
    config_yaml.write_text(
        yaml.safe_dump(
            {
                "name": "research",
                "main_model": "claude-haiku-4-5",
                "main_system_prompt_path": "prompts/main.md",
                "system_prompt_registry": {"web": "prompts/web.md"},
            }
        )
    )
    cfg = load_axon_config(config_yaml)
    assert cfg.system_prompt_registry["web"] == "web research system"
    resolved = Path(cfg.main_system_prompt_path)
    assert resolved.is_absolute()
    assert resolved.read_text() == "system prompt body"


def test_load_axon_config_shipped_research_loads():
    shipped = (
        Path(__file__).resolve().parent.parent
        / "src"
        / "rumil"
        / "orchestrators"
        / "axon"
        / "configs"
        / "research.yaml"
    )
    cfg = load_axon_config(shipped)
    assert cfg.name == "research"
    assert "freeform_text" in cfg.finalize_schema_registry
    assert "research_synthesis" in cfg.finalize_schema_registry


def test_axon_config_dataclass_defaults():
    cfg = AxonConfig(
        name="test",
        main_model="claude-haiku-4-5",
        main_system_prompt_path="x.md",
    )
    assert cfg.max_parallel_delegates_per_turn == 4
    assert cfg.hard_max_rounds == 50
    assert cfg.max_seed_pages == 20
    assert cfg.direct_tools == ()
