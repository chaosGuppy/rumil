"""Centralised configuration loaded from environment variables and .env files."""

from contextlib import contextmanager
from collections.abc import Iterator

from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    model_config = {"env_file": ".env", "extra": "ignore", "validate_assignment": True}

    anthropic_api_key: str = ""
    differential_test_mode: str = ""
    differential_prod_db: str = ""
    differential_smoke_test: str = ""
    tracing_enabled: bool = True

    supabase_url: str = "http://127.0.0.1:54321"
    supabase_key: str = (
        "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9."
        "eyJpc3MiOiJzdXBhYmFzZS1kZW1vIiwicm9sZSI6InNlcnZpY2Vfcm9sZSIsImV4cCI6MTk4MzgxMjk5Nn0."
        "EGIM96RAZx35lJzdJsyH-qQwv8Hdp7fsn3W0YpN81IU"
    )
    supabase_prod_url: str = ""
    supabase_prod_key: str = ""
    frontend_url: str = "http://127.0.0.1:3000"

    @property
    def is_test_mode(self) -> bool:
        return bool(self.differential_test_mode)

    @property
    def is_smoke_test(self) -> bool:
        return bool(self.differential_smoke_test)

    @property
    def model(self) -> str:
        return (
            "claude-haiku-4-5-20251001"
            if self.is_test_mode or self.is_smoke_test
            else "claude-opus-4-6"
        )

    @property
    def is_prod_db(self) -> bool:
        return self.differential_prod_db.lower() in ("1", "true")

    def get_supabase_credentials(self, prod: bool = False) -> tuple[str, str]:
        if prod:
            if not self.supabase_prod_url or not self.supabase_prod_key:
                raise KeyError(
                    "SUPABASE_PROD_URL and SUPABASE_PROD_KEY must be set for production."
                )
            return self.supabase_prod_url, self.supabase_prod_key
        return self.supabase_url, self.supabase_key

    def require_anthropic_key(self) -> str:
        if not self.anthropic_api_key:
            raise EnvironmentError(
                "ANTHROPIC_API_KEY environment variable not set. "
                "Set it before running the workspace."
            )
        return self.anthropic_api_key


_current: Settings | None = None


def get_settings() -> Settings:
    """Return the cached settings singleton (created on first call)."""
    global _current
    if _current is None:
        _current = Settings()
    return _current


@contextmanager
def override_settings(**overrides: object) -> Iterator[Settings]:
    """Replace the cached settings with a copy that has the given overrides.

    Usage (in tests)::

        with override_settings(differential_test_mode="1"):
            assert get_settings().is_test_mode
    """
    global _current
    previous = _current
    _current = Settings(**overrides)  # type: ignore[arg-type]
    try:
        yield _current
    finally:
        _current = previous
