"""Bounded stores for the rumil database layer.

This package is the target of an ongoing refactor that splits the
``rumil.database.DB`` god-object into per-table stores. Each store owns a
small set of tables and operations; ``DB`` composes them and retains
delegating shims so existing callers (``from rumil.database import DB``)
continue to work unchanged.

Modules:
- ``row_helpers`` — ``_row_to_*`` row-to-model converters and column
  constants. Pure functions; no DB handle.
- ``mutation_log`` — ``MutationState`` dataclass describing mutation events
  visible to a staged run. Cached per ``DB`` instance.
- ``project_store`` — ``ProjectStore``, owning the ``projects`` table plus
  stats RPCs.
"""

from rumil.db.mutation_log import MutationState
from rumil.db.project_store import ProjectStore
from rumil.db.row_helpers import (
    _LINK_COLUMNS,
    _SLIM_PAGE_COLUMNS,
    _row_to_annotation_event,
    _row_to_call,
    _row_to_call_sequence,
    _row_to_link,
    _row_to_page,
    _row_to_suggestion,
    _Rows,
    _rows,
)

__all__ = [
    "_LINK_COLUMNS",
    "_SLIM_PAGE_COLUMNS",
    "MutationState",
    "ProjectStore",
    "_Rows",
    "_row_to_annotation_event",
    "_row_to_call",
    "_row_to_call_sequence",
    "_row_to_link",
    "_row_to_page",
    "_row_to_suggestion",
    "_rows",
]
