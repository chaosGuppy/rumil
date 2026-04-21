"""Registry + lookup for the active `View` variant.

Mirrors the `ASSESS_CALL_CLASSES` pattern in `calls/call_registry.py`.
The active variant is selected by `settings.view_variant` and read lazily
so tests/overrides take effect immediately.

Concrete `View` classes are imported lazily inside `get_active_view()`
so that `rumil.views` can be imported from the orchestrators package
without pulling in the concrete implementations' import chains (which
themselves depend on pieces of `rumil.orchestrators`).
"""

from rumil.settings import get_settings
from rumil.views.base import View

VIEW_VARIANTS: tuple[str, ...] = ("sectioned", "judgement")


def get_active_view() -> View:
    """Return an instance of the View variant selected by settings."""
    variant = get_settings().view_variant
    if variant == "sectioned":
        from rumil.views.sectioned import SectionedView

        return SectionedView()
    if variant == "judgement":
        from rumil.views.judgement import JudgementView

        return JudgementView()
    raise ValueError(f"Unknown view_variant: {variant!r}. Valid values: {list(VIEW_VARIANTS)}")
