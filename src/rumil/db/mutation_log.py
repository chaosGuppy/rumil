"""Cached mutation-event state for staged-run reads.

``MutationState`` is the decoded view of ``mutation_events`` rows visible to
a particular staged run. ``database.py`` loads and caches an instance per
``DB`` handle and consults it inside ``_apply_page_events`` /
``_apply_link_events`` whenever a read path materializes pages or links.

Keeping the dataclass in its own module (rather than inside ``database.py``)
sets us up for the later phase that introduces a structural
``MutationLog.recorded_mutation()`` capability.
"""

from rumil.models import LinkRole


class MutationState:
    """Cached mutation events for a staged run, keyed by target_id.

    The "forward" fields (``superseded_pages`` etc.) replay events *visible*
    to the staged run — its own events plus baseline events up to
    ``snapshot_ts``. The "unapply" fields undo baseline mutations that were
    *written directly to the base tables* after the snapshot: the base
    rows now reflect post-snapshot state that the staged run must not see,
    and we use the mutation event log to revert them on read.
    """

    __slots__ = (
        "credence_overrides",
        "deleted_links",
        "link_role_overrides",
        "page_content_overrides",
        "robustness_overrides",
        "superseded_pages",
        "unapply_credence",
        "unapply_robustness",
        "unapply_role_overrides",
        "unapply_supersessions",
        "unapply_update_content",
    )

    def __init__(self) -> None:
        self.superseded_pages: dict[str, str] = {}
        self.deleted_links: set[str] = set()
        self.link_role_overrides: dict[str, LinkRole] = {}
        self.page_content_overrides: dict[str, str] = {}
        # page_id -> (value, reasoning) for latest set_credence/set_robustness.
        self.credence_overrides: dict[str, tuple[int | None, str | None]] = {}
        self.robustness_overrides: dict[str, tuple[int | None, str | None]] = {}
        # Pages whose baseline row currently shows superseded/updated content
        # but whose supersession/update event landed *after* snapshot_ts.
        # On read, the staged run should see the pre-mutation state:
        # is_superseded=False + original content from the event payload.
        self.unapply_supersessions: set[str] = set()
        self.unapply_update_content: dict[str, str] = {}
        # Same shape as the forward overrides, but carrying the pre-mutation
        # value to restore on read for post-snapshot baseline score events.
        self.unapply_credence: dict[str, tuple[int | None, str | None]] = {}
        self.unapply_robustness: dict[str, tuple[int | None, str | None]] = {}
        # Links whose role was changed on the base table after the snapshot.
        # Maps link_id -> the role value to restore (the event's old_role).
        self.unapply_role_overrides: dict[str, LinkRole] = {}
