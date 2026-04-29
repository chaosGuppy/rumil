"""Tests for Settings.all_env_keys() and Settings.cli_forwardable_overrides()."""

from __future__ import annotations

from pydantic_core import PydanticUndefined

from rumil.settings import Settings


def _defaults_for_fields(field_names: frozenset[str]) -> dict[str, object]:
    """Materialise each field's class default into a kwargs dict, skipping fields without a fixed default."""
    out: dict[str, object] = {}
    for name in field_names:
        default = Settings.model_fields[name].default
        if default is PydanticUndefined:
            continue
        out[name] = default
    return out


def _settings_at_defaults(**overrides: object) -> Settings:
    """Build a Settings instance pinned to forwardable-field defaults plus any explicit overrides.

    Pinning blocks values from the local `.env` / shell environment from
    leaking into the test — passing kwargs takes precedence over both.
    """
    base = _defaults_for_fields(Settings._cli_forwardable_fields())
    base.update(overrides)
    return Settings(**base)  # type: ignore[arg-type]


def test_all_env_keys_uppercases_every_field():
    keys = Settings.all_env_keys()
    assert "ANTHROPIC_API_KEY" in keys
    assert "AVAILABLE_MOVES" in keys
    assert "RUMIL_MODEL_OVERRIDE" in keys
    assert "USE_PROD_DB" in keys


def test_all_env_keys_size_matches_model_fields():
    assert len(Settings.all_env_keys()) == len(Settings.model_fields)


def test_all_env_keys_rejects_lowercase():
    """No lowercase keys leak through — defends the API validator's
    'must match ^[A-Z][A-Z0-9_]*$' check from drift."""
    keys = Settings.all_env_keys()
    assert all(k == k.upper() for k in keys)


def test_cli_forwardable_overrides_empty_at_defaults():
    settings = _settings_at_defaults()
    assert settings.cli_forwardable_overrides() == {}


def test_cli_forwardable_overrides_includes_capture_marked_overrides():
    settings = _settings_at_defaults(available_moves="experimental")
    out = settings.cli_forwardable_overrides()
    assert out["AVAILABLE_MOVES"] == "experimental"


def test_cli_forwardable_overrides_includes_extra_set_overrides():
    """rumil_model_override / rumil_smoke_test etc. aren't capture-marked
    but are explicitly forwardable."""
    settings = _settings_at_defaults(rumil_model_override="claude-sonnet-4-6", rumil_smoke_test="1")
    out = settings.cli_forwardable_overrides()
    assert out["RUMIL_MODEL_OVERRIDE"] == "claude-sonnet-4-6"
    assert out["RUMIL_SMOKE_TEST"] == "1"


def test_cli_forwardable_overrides_stringifies_bool_as_lowercase():
    """pydantic-settings parses 'true'/'false' on Job startup."""
    settings = _settings_at_defaults(force_twophase_recurse=True)
    out = settings.cli_forwardable_overrides()
    assert out["FORCE_TWOPHASE_RECURSE"] == "true"


def test_cli_forwardable_overrides_stringifies_int():
    settings = _settings_at_defaults(ingest_num_claims=8)
    out = settings.cli_forwardable_overrides()
    assert out["INGEST_NUM_CLAIMS"] == "8"


def test_cli_forwardable_overrides_stringifies_float():
    settings = _settings_at_defaults(global_prio_budget_fraction=0.5)
    out = settings.cli_forwardable_overrides()
    assert out["GLOBAL_PRIO_BUDGET_FRACTION"] == "0.5"


def test_cli_forwardable_overrides_excludes_credentials_even_when_set():
    """Defends the curated CLI subset: a developer with a local .env
    pointing at prod must not have those values silently pushed into
    the cloud Job."""
    settings = _settings_at_defaults(
        anthropic_api_key="sk-secret",
        supabase_prod_url="https://laptop.supabase.local",
        supabase_prod_key="laptop-secret",
        gcp_project_id="laptop-project",
        frontend_url="http://laptop.local",
    )
    out = settings.cli_forwardable_overrides()
    assert "ANTHROPIC_API_KEY" not in out
    assert "SUPABASE_PROD_URL" not in out
    assert "SUPABASE_PROD_KEY" not in out
    assert "GCP_PROJECT_ID" not in out
    assert "FRONTEND_URL" not in out


def test_cli_forwardable_overrides_omits_field_set_back_to_default():
    """A field re-set to its default value is indistinguishable from
    the unset case — both should be omitted (otherwise we'd shadow
    the cloud's deployed value with a duplicate of the same value,
    inflating the env list)."""
    default = Settings.model_fields["available_moves"].default
    settings = _settings_at_defaults(available_moves=default)
    assert "AVAILABLE_MOVES" not in settings.cli_forwardable_overrides()


def test_cli_forwardable_fields_includes_capture_marked():
    fields = Settings._cli_forwardable_fields()
    assert "available_moves" in fields
    assert "view_variant" in fields
    assert "ingest_num_claims" in fields


def test_cli_forwardable_fields_includes_explicit_extras():
    fields = Settings._cli_forwardable_fields()
    assert "rumil_model_override" in fields
    assert "rumil_smoke_test" in fields
    assert "rumil_test_mode" in fields
    assert "force_twophase_recurse" in fields


def test_cli_forwardable_fields_excludes_credentials_and_infra():
    fields = Settings._cli_forwardable_fields()
    for excluded in (
        "anthropic_api_key",
        "supabase_prod_url",
        "supabase_prod_key",
        "supabase_jwt_secret",
        "gcp_project_id",
        "gcp_cluster_name",
        "frontend_url",
        "rumil_api_url",
        "default_cli_user_id",
        "auth_enabled",
        "langfuse_public_key",
        "langfuse_secret_key",
        "voyage_ai_api_key",
        "jina_api_key",
        "use_prod_db",
        "tracing_enabled",
        "db_max_concurrent_queries",
    ):
        assert excluded not in fields, f"{excluded} should not be CLI-forwardable"
