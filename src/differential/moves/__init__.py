"""Move definitions for the research workspace.

Each move is fully defined in its own module. Key sub-modules:
  - base: MoveDef, MoveResult, MoveState, and shared helpers
  - registry: MOVES dict
"""

from differential.moves.base import MoveDef, MoveResult, MoveState
from differential.moves.registry import MOVES

__all__ = [
    "MoveDef",
    "MoveResult",
    "MoveState",
    "MOVES",
]
