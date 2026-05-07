"""Centralised configuration loaded from environment variables and .env files."""

import contextvars
import subprocess
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from pydantic import Field, field_validator
from pydantic.config import JsonDict
from pydantic_settings import BaseSettings, PydanticBaseSettingsSource

_CAPTURE: JsonDict = {"capture": True}

RUMIL_MODEL_ALIASES: dict[str, str] = {
    "opus": "claude-opus-4-7",
    "sonnet": "claude-sonnet-4-6",
    "haiku": "claude-haiku-4-5-20251001",
}

_MODEL_OVERRIDE_PREFIXES = ("claude-opus-", "claude-sonnet-", "claude-haiku-")


def resolve_model_alias(name: str) -> str:
    """Map a short model alias (opus/sonnet/haiku) to its full Anthropic id.

    Returns ``name`` unchanged if it isn't a known alias — entry points that
    accept either an alias or a full id can call this unconditionally.
    """
    return RUMIL_MODEL_ALIASES.get(name, name)


def _get_git_commit() -> str:
    """Return the short git commit hash, or '' if unavailable."""
    try:
        return subprocess.check_output(
            ["git", "rev-parse", "--short", "HEAD"],
            stderr=subprocess.DEVNULL,
            text=True,
        ).strip()
    except Exception:
        return ""


def _capture_field(**kwargs: Any) -> Any:
    """Mark a field for inclusion in capture_config()."""
    return Field(json_schema_extra=_CAPTURE, **kwargs)


# Settings field names the CLI auto-forwards into the cloud Job's env when
# their local value differs from the class default. Curated rather than
# blanket: a developer's local `.env` setting (say) SUPABASE_PROD_URL or
# ANTHROPIC_API_KEY would otherwise redirect the cloud Job at their laptop's
# resources or burn local quota. The cloud Job already inherits those from
# the rumil-api Deployment. The set covers experiment knobs the user actually
# wants to vary per run: every `_capture_field`-marked field plus a small
# handful of mode/override booleans/strings.
_CLI_FORWARDABLE_EXTRAS: frozenset[str] = frozenset(
    {
        "rumil_model_override",
        "rumil_smoke_test",
        "rumil_test_mode",
        "force_twophase_recurse",
    }
)


class Settings(BaseSettings):
    # ".env" is the shared (often symlinked) project env; ".env.overrides"
    # layers on top — typically written by the workmux post_create hook for
    # per-worktree overrides (see /Users/chaos-guppy/differential/.workmux.yaml),
    # but devs without a worktree setup can use it too. Later files in the
    # tuple override earlier ones, so .env.overrides wins.
    #
    # Source priority is overridden below: .env files beat process
    # environment variables, inverting pydantic-settings v2's default.
    # The default would let a stale ``ANTHROPIC_API_KEY`` exported in a
    # developer's shell silently redirect API calls / billing away from
    # the project key in .env. Intent on this branch (and per the
    # comment above) is that the project's .env IS the source of truth;
    # shell vars are only consulted as a last resort fallback.
    model_config = {
        "env_file": (".env", ".env.overrides"),
        "extra": "ignore",
        "validate_assignment": True,
    }

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        # Order: explicit init kwargs win > dotenv (.env, .env.overrides)
        # > shell env > file_secret_settings. Inverts pydantic-settings'
        # default which puts env_settings above dotenv_settings.
        return (init_settings, dotenv_settings, env_settings, file_secret_settings)

    anthropic_api_key: str = ""
    rumil_test_mode: str = ""
    rumil_smoke_test: str = ""
    # Optional full model id ("claude-sonnet-4-6", etc.) that overrides
    # the default derived by the `model` property. Set via
    # RUMIL_MODEL_OVERRIDE env var. Intended for entry points (e.g.
    # versus judging) that want to pick a model per invocation. Validated
    # against known Anthropic model id prefixes so typos fail fast at
    # construction instead of leaking into a downstream API error.
    rumil_model_override: str = ""

    @field_validator("rumil_model_override")
    @classmethod
    def _validate_model_override(cls, v: str) -> str:
        if v and not v.startswith(_MODEL_OVERRIDE_PREFIXES):
            raise ValueError(
                f"rumil_model_override={v!r} must start with one of "
                f"{_MODEL_OVERRIDE_PREFIXES} (or be empty/unset)"
            )
        return v

    force_twophase_recurse: bool = False
    use_prod_db: str = ""
    tracing_enabled: bool = True

    supabase_local_url: str = "http://127.0.0.1:54321"
    supabase_local_key: str = (
        "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9."
        "eyJpc3MiOiJzdXBhYmFzZS1kZW1vIiwicm9sZSI6InNlcnZpY2Vfcm9sZSIsImV4cCI6MTk4MzgxMjk5Nn0."
        "EGIM96RAZx35lJzdJsyH-qQwv8Hdp7fsn3W0YpN81IU"
    )
    supabase_prod_url: str = ""
    supabase_prod_key: str = ""
    supabase_jwt_secret: str = "super-secret-jwt-token-with-at-least-32-characters-long"
    auth_enabled: bool = True
    # Shared CLI service-account user in *prod* Supabase Auth
    # (cli-service@rumil.local). Created by
    # scripts/create_cli_service_account.py. Override via the
    # DEFAULT_CLI_USER_ID env var to attribute remote jobs to a specific user.
    # Only applied when targeting prod — see `effective_cli_user_id` below; the
    # local Supabase has its own auth.users and would FK-fail this UUID.
    default_cli_user_id: str = "c4179ddb-bf61-4ba3-acfa-6b5408c19874"
    rumil_api_url: str = "https://api.rumil.ink"
    # GKE cluster identity, used to build a Cloud Logging URL for the
    # orchestrator-run pod. Only the API container needs these set; the laptop
    # CLI just prints whatever URL the API returns.
    gcp_project_id: str = ""
    gcp_cluster_name: str = "differential"
    # Vertex AI region for google-genai calls. "global" routes to the
    # multi-region endpoint; gemini-3-flash-preview is only served there
    # at the moment.
    gcp_location: str = "global"
    voyage_ai_api_key: str = ""
    jina_api_key: str = ""
    langfuse_public_key: str = ""
    langfuse_secret_key: str = ""
    langfuse_base_url: str = "https://us.cloud.langfuse.com"
    frontend_url: str = "http://127.0.0.1:3000"
    db_max_concurrent_queries: int = 20

    assess_call_variant: str = _capture_field(default="default")
    prioritizer_variant: str = _capture_field(default="two_phase")
    view_variant: str = _capture_field(default="sectioned")

    available_moves: str = _capture_field(default="default")
    available_calls: str = _capture_field(default="default")

    budget_pacing_enabled: bool = _capture_field(default=True)

    evaluate_content_hops: int = _capture_field(default=0)
    evaluate_abstract_hops: int = _capture_field(default=1)
    evaluate_headline_hops: int = _capture_field(default=2)
    sdk_agent_max_turns: int = _capture_field(default=200)
    sdk_agent_max_subagents: int = _capture_field(default=5)
    grounding_update_budget: int = _capture_field(default=10)
    feedback_update_budget: int = _capture_field(default=10)
    feedback_investigation_budget: int = _capture_field(default=30)
    ingest_num_claims: int = _capture_field(default=4)

    sonnet_model_configured: str = _capture_field(default="claude-sonnet-4-6")
    enable_global_prio: bool = _capture_field(default=False)
    global_prio_budget_fraction: float = _capture_field(default=0.2)
    global_prio_trigger_threshold: int = _capture_field(default=10)
    global_prio_explore_rounds: int = _capture_field(default=3)
    global_prio_subgraph_max_pages: int = _capture_field(default=80)

    explore_subgraph_default_max_pages: int = _capture_field(default=30)

    scope_subquestion_linker_max_rounds: int = _capture_field(default=6)
    scope_subquestion_linker_seed_limit: int = _capture_field(default=10)
    scope_subquestion_linker_subgraph_max_pages: int = _capture_field(default=40)
    linker_cache_invalidation_threshold: int = _capture_field(default=100)
    subquestion_linker_enabled: bool = _capture_field(default=True)
    enable_red_team: bool = _capture_field(default=False)

    max_db_retries: int = _capture_field(default=10)
    max_db_statement_timeout_retries: int = _capture_field(default=3)
    max_api_retries: int = _capture_field(default=60)
    max_api_retries_429: int | None = _capture_field(default=None)
    max_api_retries_500: int | None = _capture_field(default=None)
    max_api_retries_529: int | None = _capture_field(default=None)

    view_importance_5_cap: int = _capture_field(default=5)
    view_importance_4_cap: int = _capture_field(default=10)
    view_importance_3_cap: int = _capture_field(default=25)
    view_importance_2_cap: int = _capture_field(default=50)

    full_page_char_budget: int = _capture_field(default=10_000)
    abstract_page_char_budget: int = _capture_field(default=10_000)
    summary_page_char_budget: int = _capture_field(default=5_000)
    distillation_page_char_budget: int = _capture_field(default=3_000)

    big_assess_full_page_char_budget: int | None = _capture_field(default=None)
    big_assess_abstract_page_char_budget: int | None = _capture_field(default=None)
    big_assess_summary_page_char_budget: int | None = _capture_field(default=None)
    full_page_similarity_floor: float = _capture_field(default=0.3)
    abstract_page_similarity_floor: float = _capture_field(default=0.2)
    summary_page_similarity_floor: float = _capture_field(default=0.1)
    big_assess_full_page_similarity_floor: float | None = _capture_field(default=None)
    big_assess_abstract_page_similarity_floor: float | None = _capture_field(default=None)
    document_floor_delta: float = _capture_field(default=0.25)

    impact_filter_scoring_model: str = _capture_field(default="claude-sonnet-4-6")
    impact_filter_pare_model: str = _capture_field(default="claude-opus-4-7")
    impact_filter_token_budget: int = _capture_field(default=200_000)
    impact_filter_floor_percentile: int = _capture_field(default=25)
    impact_filter_pare_threshold_tokens: int = _capture_field(default=50_000)
    impact_filter_pare_target_tokens: int = _capture_field(default=50_000)
    impact_filter_max_distance: int = _capture_field(default=4)
    impact_filter_concurrency: int = _capture_field(default=10)

    @property
    def is_test_mode(self) -> bool:
        return bool(self.rumil_test_mode)

    @property
    def is_smoke_test(self) -> bool:
        return bool(self.rumil_smoke_test)

    @property
    def model(self) -> str:
        if self.rumil_model_override:
            return self.rumil_model_override
        return (
            "claude-haiku-4-5-20251001"
            if self.is_test_mode or self.is_smoke_test
            else "claude-opus-4-7"
        )

    @property
    def sonnet_model(self) -> str:
        if self.is_test_mode or self.is_smoke_test:
            return self.model
        return self.sonnet_model_configured

    def get_max_retries(self, status: int | None = None) -> int:
        """Return the max retry count, optionally specialized by HTTP status."""
        if status is not None:
            override = getattr(self, f"max_api_retries_{status}", None)
            if override is not None:
                return min(override, 3) if self.is_test_mode else override
        return min(self.max_api_retries, 3) if self.is_test_mode else self.max_api_retries

    @property
    def is_prod_db(self) -> bool:
        return self.use_prod_db.lower() in ("1", "true")

    @property
    def effective_cli_user_id(self) -> str:
        """The user_id to stamp on projects created by `main.py`.

        The committed default for `default_cli_user_id` is the prod-Supabase
        service-account UUID and would FK-fail the local Supabase's
        `auth.users`. Only apply it when the run is actually targeting prod;
        local runs default to no owner.
        """
        return self.default_cli_user_id if self.is_prod_db else ""

    def get_supabase_credentials(self, prod: bool = False) -> tuple[str, str]:
        if prod:
            if not self.supabase_prod_url or not self.supabase_prod_key:
                raise KeyError(
                    "SUPABASE_PROD_URL and SUPABASE_PROD_KEY must be set for production."
                )
            return self.supabase_prod_url, self.supabase_prod_key
        return self.supabase_local_url, self.supabase_local_key

    def require_anthropic_key(self) -> str:
        if not self.anthropic_api_key:
            raise OSError(
                "ANTHROPIC_API_KEY environment variable not set. "
                "Set it before running the workspace."
            )
        return self.anthropic_api_key

    @property
    def langfuse_enabled(self) -> bool:
        return bool(self.langfuse_public_key and self.langfuse_secret_key)

    @classmethod
    def from_env_files(cls, *env_files: str | Path) -> Settings:
        """Create a Settings instance loading from the given env files."""
        return cls(_env_file=env_files)  # type: ignore[call-arg]

    def capture_config(self) -> dict:
        """Collect fields marked with capture=True plus derived model and git commit."""
        result: dict = {}
        for name, field_info in self.model_fields.items():
            extra = field_info.json_schema_extra
            if isinstance(extra, dict) and extra.get("capture"):
                result[name] = getattr(self, name)
        result["model"] = self.model
        result["git_commit"] = _get_git_commit()
        return result

    @classmethod
    def all_env_keys(cls) -> frozenset[str]:
        """Uppercased view of every Settings field name.

        Used by the cloud-job launcher to validate caller-supplied
        `extra_env` keys: anything not in this set is a typo or an
        unrelated env var, and we 422 it at submission time rather than
        let it silently fall on the floor (Settings is configured
        extra="ignore", so unknown env vars don't surface anywhere).
        """
        return frozenset(name.upper() for name in cls.model_fields)

    @classmethod
    def _cli_forwardable_fields(cls) -> frozenset[str]:
        """Snake_case Settings field names the CLI may auto-forward.

        Every `_capture_field`-marked field (the existing per-run tuning
        surface) plus a small set of mode/override knobs. Credentials,
        DB URLs, GCP identifiers, etc. are intentionally excluded — the
        cloud Job inherits those from the rumil-api Deployment and a
        local override would point it at the wrong resources.
        """
        capture_marked = {
            name
            for name, field_info in cls.model_fields.items()
            if isinstance(field_info.json_schema_extra, dict)
            and field_info.json_schema_extra.get("capture")
        }
        return frozenset(capture_marked | _CLI_FORWARDABLE_EXTRAS)

    def cli_forwardable_overrides(self) -> dict[str, str]:
        """Forwardable Settings fields whose value differs from the default.

        Returned as a dict of `{ENV_VAR_NAME: stringified_value}` ready
        to drop into the cloud-job request's `extra_env`. Bools become
        "true"/"false" so they round-trip through pydantic-settings'
        env parsing; `None`-valued fields are omitted.
        """
        out: dict[str, str] = {}
        for name in self._cli_forwardable_fields():
            field_info = type(self).model_fields[name]
            current = getattr(self, name)
            if current is None or current == field_info.default:
                continue
            out[name.upper()] = (
                "true" if current is True else "false" if current is False else str(current)
            )
        return out


_settings_var: contextvars.ContextVar[Settings | None] = contextvars.ContextVar(
    "rumil_settings", default=None
)


def get_settings() -> Settings:
    """Return the current task-local settings (created on first call)."""
    current = _settings_var.get()
    if current is None:
        current = Settings()
        _settings_var.set(current)
    return current


@contextmanager
def override_settings(**overrides: object) -> Iterator[Settings]:
    """Replace the settings for the current context with a copy that has the given overrides.

    Usage (in tests)::

        with override_settings(rumil_test_mode="1"):
            assert get_settings().is_test_mode
    """
    previous = _settings_var.get()
    new = Settings(**overrides)  # type: ignore[arg-type]
    _settings_var.set(new)
    try:
        yield new
    finally:
        _settings_var.set(previous)
