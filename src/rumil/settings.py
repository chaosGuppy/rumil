"""Centralised configuration loaded from environment variables and .env files."""

import contextvars
from contextlib import contextmanager
from collections.abc import Iterator
from pathlib import Path

from typing import Any

from collections.abc import Sequence

from pydantic import Field
from pydantic.config import JsonDict
from pydantic_settings import BaseSettings

from rumil.models import FindConsiderationsMode


_CAPTURE: JsonDict = {"capture": True}


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

    find_considerations_call_variant: str = _capture_field(default="embedding")
    assess_call_variant: str = _capture_field(default="embedding")
    ingest_call_variant: str = _capture_field(default="embedding")
    web_research_call_variant: str = _capture_field(default="default")
    prioritizer_variant: str = _capture_field(default="two_phase")

    moves_preset: str = _capture_field(default="default")
    available_calls: str = _capture_field(default="default")

    find_considerations_modes: str = _capture_field(
        default="alternate,abstract,concrete"
    )

    evaluate_content_hops: int = _capture_field(default=0)
    evaluate_abstract_hops: int = _capture_field(default=1)
    evaluate_headline_hops: int = _capture_field(default=2)
    evaluate_max_turns: int = _capture_field(default=200)

    full_page_char_budget: int = _capture_field(default=10_000)
    abstract_page_char_budget: int = _capture_field(default=10_000)
    summary_page_char_budget: int = _capture_field(default=5_000)
    distillation_page_char_budget: int = _capture_field(default=3_000)
    full_page_similarity_floor: float = _capture_field(default=0.3)
    abstract_page_similarity_floor: float = _capture_field(default=0.2)
    summary_page_similarity_floor: float = _capture_field(default=0.1)

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
            else "claude-opus-4-6"
        )

    @property
    def allowed_find_considerations_modes(self) -> Sequence[FindConsiderationsMode]:
        return [
            FindConsiderationsMode(m.strip())
            for m in self.find_considerations_modes.split(",")
            if m.strip()
        ]

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
            raise EnvironmentError(
                "ANTHROPIC_API_KEY environment variable not set. "
                "Set it before running the workspace."
            )
        return self.anthropic_api_key

    @classmethod
    def from_env_files(cls, *env_files: str | Path) -> "Settings":
        """Create a Settings instance loading from the given env files."""
        return cls(_env_file=env_files)  # type: ignore[call-arg]

    def capture_config(self) -> dict:
        """Collect fields marked with capture=True plus derived model."""
        result: dict = {}
        for name, field_info in self.model_fields.items():
            extra = field_info.json_schema_extra
            if isinstance(extra, dict) and extra.get("capture"):
                result[name] = getattr(self, name)
        result["model"] = self.model
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
