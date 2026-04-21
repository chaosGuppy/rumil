"""Centralised configuration loaded from environment variables and .env files."""

import contextvars
import subprocess
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from pydantic import Field
from pydantic.config import JsonDict
from pydantic_settings import BaseSettings

_CAPTURE: JsonDict = {"capture": True}


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


class Settings(BaseSettings):
    model_config = {"env_file": ".env", "extra": "ignore", "validate_assignment": True}

    anthropic_api_key: str = ""
    rumil_test_mode: str = ""
    rumil_smoke_test: str = ""
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
    voyage_ai_api_key: str = ""
    jina_api_key: str = ""
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

    max_db_retries: int = _capture_field(default=60)
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

    @property
    def is_test_mode(self) -> bool:
        return bool(self.rumil_test_mode)

    @property
    def is_smoke_test(self) -> bool:
        return bool(self.rumil_smoke_test)

    @property
    def model(self) -> str:
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
                return override
        return self.max_api_retries

    @property
    def is_prod_db(self) -> bool:
        return self.use_prod_db.lower() in ("1", "true")

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

    @classmethod
    def from_env_files(cls, *env_files: str | Path) -> "Settings":
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
