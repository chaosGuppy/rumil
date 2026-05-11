"""ArtifactStore — append-only k,v map of named text blobs threaded through a run.

Inputs (caller-seeded via :class:`OrchInputs.artifacts`) and spawn outputs
(via :class:`SubroutineResult.produces`) are unified into one store. The
mainline agent doesn't need to forward content between subroutines —
it references entries by key in the spawn tool's ``include_artifacts``
field, and the orchestrator splices the entries' text into the spawn's
user prompt.

Three usage axes:

- **Caller-seeded (input):** :class:`OrchInputs.artifacts` becomes
  ``Artifact(produced_by="input", spawn_id=None, round_idx=None)`` entries.
- **Spawn-produced:** every entry in :class:`SubroutineResult.produces`
  is folded in under a namespaced key after the spawn returns. A single
  produces entry with empty key ``""`` becomes ``<sub_name>/<spawn_id>``;
  multiple entries become ``<sub_name>/<spawn_id>/<sub_key>``.
- **Static consumes:** subroutines list keys they always need on
  :class:`SubroutineBase.consumes` — splicing happens automatically at
  spawn time.

Append-only: keys are unique and the store rejects collisions loudly.
This avoids "which steelman did mainline mean?" ambiguity — every spawn
output gets a unique spawn-id-suffixed key, and the registry surfaces
the provenance so mainline can pick the right one by key.

Memory: each artifact is just a string. Bounded in practice by the
token budget; no GC needed within a run.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass

_INPUT_PRODUCER = "input"


@dataclass(frozen=True)
class Artifact:
    """One named text blob in the store, with provenance metadata.

    ``produced_by`` is ``"input"`` for caller-seeded entries or the
    subroutine name for spawn-produced ones. ``spawn_id`` and
    ``round_idx`` are populated for spawn-produced entries; both are
    ``None`` for input-seeded entries.
    """

    key: str
    text: str
    produced_by: str
    spawn_id: str | None = None
    round_idx: int | None = None


class ArtifactStore:
    """Append-only k,v store of typed text artifacts threaded through a run.

    Add via :meth:`add` (raises on key collision). Read via :meth:`get`,
    :meth:`list_keys`. Render a subset of entries as a markdown block via
    :meth:`render_block` for splicing into a spawn's user prompt.
    """

    def __init__(self, seed: Mapping[str, str] | None = None) -> None:
        """Build the store, optionally seeding caller-supplied entries.

        Seed entries are recorded with ``produced_by="input"``. Seeding
        through ``__init__`` is equivalent to calling :meth:`add` for
        each pair with ``produced_by="input"``; it exists so callers
        don't have to spell out the producer for every seed entry.
        """
        self._items: dict[str, Artifact] = {}
        if seed:
            for k, v in seed.items():
                self.add(k, v, produced_by=_INPUT_PRODUCER)

    def add(
        self,
        key: str,
        text: str,
        *,
        produced_by: str,
        spawn_id: str | None = None,
        round_idx: int | None = None,
    ) -> Artifact:
        """Insert an artifact under ``key``. Raises on collision.

        Collision-loud-by-design: silent overwrite would let a later
        spawn's "output_v2" silently shadow an earlier producer's value
        and any subroutine that consumed the earlier value would see
        different text on retry. The orchestrator constructs unique keys
        (``<name>/<spawn_id>[/<sub_key>]``) so collisions only happen on
        bugs (or caller-seeded keys colliding with auto-generated ones).
        """
        if key in self._items:
            raise ValueError(
                f"ArtifactStore: key {key!r} already exists "
                f"(produced_by={self._items[key].produced_by}); "
                "artifacts are append-only by design"
            )
        artifact = Artifact(
            key=key,
            text=text,
            produced_by=produced_by,
            spawn_id=spawn_id,
            round_idx=round_idx,
        )
        self._items[key] = artifact
        return artifact

    def get(self, key: str) -> Artifact | None:
        return self._items.get(key)

    def __contains__(self, key: object) -> bool:
        return isinstance(key, str) and key in self._items

    def list_keys(self) -> list[str]:
        """All keys, in insertion order."""
        return list(self._items)

    def render_block(self, keys: Sequence[str]) -> str:
        """Render the listed keys as an XML-fenced block for splicing.

        XML fences (rather than markdown headers) so artifact bodies
        containing arbitrary markdown — including their own H2/H3
        headers — can't collide with the boundary marker. Anthropic
        models parse this shape natively. Format:

        ```
        ## Artifacts

        <artifact key="pair_text" chars="23142" from="input">
        <text>
        </artifact>

        <artifact key="rubric" chars="4180" from="input">
        <text2>
        </artifact>
        ```

        Missing keys are skipped silently — callers should validate
        first via :meth:`require_keys` if they want a loud error.
        """
        parts: list[str] = ["## Artifacts", ""]
        for k in keys:
            art = self._items.get(k)
            if art is None:
                continue
            parts.append(self._render_one(art))
            parts.append("")
        return "\n".join(parts).rstrip() + "\n"

    def render_seed_block(self) -> str:
        """Render input-seeded artifacts with content for mainline's first turn.

        Mainline's initial user message uses this to surface the seed
        keys *and their content* in one go — wrapped in the same XML
        fences subroutines see, so demarcation is consistent end to
        end. Spawn-produced artifacts get announced lazily (one-line
        ``announce`` strings in tool_result messages) since their
        content is already in the spawn's ``text_summary``.
        """
        seed_keys = [k for k, a in self._items.items() if a.produced_by == _INPUT_PRODUCER]
        if not seed_keys:
            return ""
        return self.render_block(seed_keys)

    def _render_one(self, art: Artifact) -> str:
        provenance = "input" if art.produced_by == _INPUT_PRODUCER else f"spawn:{art.produced_by}"
        return (
            f'<artifact key="{art.key}" chars="{len(art.text)}" '
            f'from="{provenance}">\n{art.text}\n</artifact>'
        )

    def announce(self, key: str) -> str:
        """One-line announcement for the spawn tool_result message.

        Used by the orchestrator to inform mainline that a new artifact
        is available for ``include_artifacts`` reference. Format:

        ``Produced artifact `<key>` (12,345 chars, from spawn:<name> round 3).``
        """
        art = self._items.get(key)
        if art is None:
            return f"[announce-error] no artifact at key {key!r}"
        chars = f"{len(art.text):,}"
        provenance = "input" if art.produced_by == _INPUT_PRODUCER else f"spawn:{art.produced_by}"
        round_part = "" if art.round_idx is None else f" round {art.round_idx}"
        return f"Produced artifact `{key}` ({chars} chars, from {provenance}{round_part})."

    def announce_seed(self) -> list[str]:
        """One-line announcement per seed entry — emitted in the initial user message.

        Returns one string per ``produced_by="input"`` artifact. Empty
        list when no seed entries exist.
        """
        return [
            f"Artifact `{k}` available ({len(a.text):,} chars, from input)."
            for k, a in self._items.items()
            if a.produced_by == _INPUT_PRODUCER
        ]

    def require_keys(self, keys: Iterable[str]) -> list[str]:
        """Return the subset of ``keys`` that aren't in the store.

        Empty list ⇒ all present. The orchestrator uses this to surface
        an informative ``is_error`` tool_result when mainline references
        an unknown artifact key.
        """
        return [k for k in keys if k not in self._items]
