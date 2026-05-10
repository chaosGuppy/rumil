"""ArtifactStore — append-only k,v map of named text blobs threaded through a run.

Two sources fold into one store:

- **Caller-seeded (input):** :class:`OrchInputs.artifacts` becomes
  ``Artifact(produced_by="input", ...)`` entries. Operating assumptions
  arrive here as the reserved key ``"operating_assumptions"``. Callers
  can supply each entry as a plain string (full text only) or as an
  :class:`ArtifactSeed` carrying a description and a ``render_inline``
  flag — inline=True splices the body into the spine's first user
  message; inline=False (default) just announces the key + description
  and lets the model load the body via the ``read_artifact`` tool.
- **Delegate-produced:** when :class:`DelegateConfig.artifact_key` is
  set, the inner loop's finalize payload is folded in under that key
  after the delegate returns. For ``n>1`` delegates, each sample lands
  at a distinct key derived from the base key.

Append-only by design: keys are unique and the store rejects collisions
loudly. Mainline references entries by key in the artifact-aware tools
(``read_artifact``) and configure can splice them into inner-loop
framing via ``DelegateConfig.artifact_keys``.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass

_INPUT_PRODUCER = "input"


@dataclass(frozen=True)
class ArtifactSeed:
    """Caller-supplied artifact at run start with rendering metadata.

    ``text`` is the body. ``description`` is a short label (one line)
    surfaced in the spine's announcement of available artifacts —
    helps the model decide whether to ``read_artifact`` the body.
    ``render_inline``: if True, the spine's first user message includes
    the full body XML-fenced; if False (default), only the announcement
    is shown and the body must be loaded via ``read_artifact``.
    """

    text: str
    description: str = ""
    render_inline: bool = False


@dataclass(frozen=True)
class Artifact:
    """One named text blob in the store, with provenance + render metadata."""

    key: str
    text: str
    produced_by: str
    spawn_id: str | None = None
    round_idx: int | None = None
    description: str = ""
    render_inline: bool = False


class ArtifactStore:
    """Append-only k,v store of typed text artifacts threaded through a run."""

    def __init__(
        self,
        seed: Mapping[str, str | ArtifactSeed] | None = None,
    ) -> None:
        """Build the store, optionally seeding caller-supplied entries.

        Each seed value is either a plain string (treated as
        ``ArtifactSeed(text=v)`` with default description="" and
        render_inline=False) or an :class:`ArtifactSeed`.
        """
        self._items: dict[str, Artifact] = {}
        if seed:
            for k, v in seed.items():
                if isinstance(v, ArtifactSeed):
                    self.add(
                        k,
                        v.text,
                        produced_by=_INPUT_PRODUCER,
                        description=v.description,
                        render_inline=v.render_inline,
                    )
                else:
                    self.add(k, v, produced_by=_INPUT_PRODUCER)

    def add(
        self,
        key: str,
        text: str,
        *,
        produced_by: str,
        spawn_id: str | None = None,
        round_idx: int | None = None,
        description: str = "",
        render_inline: bool = False,
    ) -> Artifact:
        """Insert an artifact under ``key``. Raises on collision."""
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
            description=description,
            render_inline=render_inline,
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
        containing arbitrary markdown can't collide with the boundary
        marker. Format:

        ```
        ## Artifacts

        <artifact key="pair_text" chars="23142" from="input" desc="...">
        <text>
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

    def render_seed_inline_block(self) -> str:
        """Render only seed artifacts with ``render_inline=True``.

        Used for the spine's first user message to splice full bodies
        of inline-flagged artifacts. Other seed artifacts get only
        the announcement (see :meth:`announce_seed`).
        """
        inline_keys = [
            k
            for k, a in self._items.items()
            if a.produced_by == _INPUT_PRODUCER and a.render_inline
        ]
        if not inline_keys:
            return ""
        return self.render_block(inline_keys)

    def _render_one(self, art: Artifact) -> str:
        provenance = (
            "input" if art.produced_by == _INPUT_PRODUCER else f"delegate:{art.produced_by}"
        )
        desc_attr = ""
        if art.description:
            escaped = art.description.replace('"', "&quot;")
            desc_attr = f' desc="{escaped}"'
        return (
            f'<artifact key="{art.key}" chars="{len(art.text)}" '
            f'from="{provenance}"{desc_attr}>\n{art.text}\n</artifact>'
        )

    def announce(self, key: str) -> str:
        """One-line announcement for a delegate's tool_result message."""
        art = self._items.get(key)
        if art is None:
            return f"[announce-error] no artifact at key {key!r}"
        chars = f"{len(art.text):,}"
        provenance = (
            "input" if art.produced_by == _INPUT_PRODUCER else f"delegate:{art.produced_by}"
        )
        round_part = "" if art.round_idx is None else f" round {art.round_idx}"
        desc_part = f" — {art.description}" if art.description else ""
        return (
            f"Produced artifact `{key}` ({chars} chars, from {provenance}{round_part}){desc_part}."
        )

    def announce_seed(self) -> list[str]:
        """One-line announcement per seed entry — emitted in the initial user message.

        Includes the description (if set) and a hint about whether the
        body is rendered inline below or must be loaded via
        ``read_artifact``.
        """
        out: list[str] = []
        for k, a in self._items.items():
            if a.produced_by != _INPUT_PRODUCER:
                continue
            chars = f"{len(a.text):,}"
            desc_part = f" — {a.description}" if a.description else ""
            mode = "rendered inline below" if a.render_inline else "load via read_artifact"
            out.append(f"`{k}` ({chars} chars){desc_part}. ({mode})")
        return out

    def require_keys(self, keys: Iterable[str]) -> list[str]:
        """Return the subset of ``keys`` that aren't in the store."""
        return [k for k in keys if k not in self._items]
