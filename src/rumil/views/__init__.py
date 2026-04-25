"""Pluggable View abstraction.

A `View` is the ever-evolving best summary of a question. Concrete variants
(SectionedView, JudgementView) implement the same lifecycle (exists/refresh)
and rendering surfaces (prioritization, parent-scoring, child-investigation).
The active variant is chosen via `settings.view_variant` — see `registry.py`.
"""

from rumil.views.base import View
from rumil.views.registry import VIEW_VARIANTS, get_active_view

__all__ = ["VIEW_VARIANTS", "View", "get_active_view"]
