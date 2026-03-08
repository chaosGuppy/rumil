"""
Parse structured moves from LLM output.

Moves are delimited by XML-style tags:
    <move type="CREATE_CLAIM">
    { ...json payload... }
    </move>

The closing review uses a separate tag:
    <review>
    { ...json payload... }
    </review>
"""

import json
import re
from dataclasses import dataclass
from typing import Any, Optional

from differential.models import DISPATCHABLE_CALL_TYPES, CallType, MoveType


MOVE_PATTERN = re.compile(
    r'<move\s+type=["\']([^"\']+)["\']\s*>\s*(.*?)\s*</move>',
    re.DOTALL | re.IGNORECASE,
)

REVIEW_PATTERN = re.compile(
    r"<review>\s*(.*?)\s*</review>",
    re.DOTALL | re.IGNORECASE,
)

DISPATCH_PATTERN = re.compile(
    r'<dispatch\s+type=["\']([^"\']+)["\']\s*>\s*(.*?)\s*</dispatch>',
    re.DOTALL | re.IGNORECASE,
)


@dataclass
class Move:
    move_type: MoveType
    payload: dict[str, Any]
    raw: str


@dataclass
class Dispatch:
    call_type: CallType
    payload: dict[str, Any]


@dataclass
class ParsedOutput:
    moves: list[Move]
    review: Optional[dict[str, Any]]
    dispatches: list[Dispatch]  # for prioritization calls
    load_page_ids: list[str]  # short IDs from LOAD_PAGE moves
    raw: str


def _parse_json_payload(text: str, move_type: str) -> dict[str, Any]:
    """Parse JSON from a move payload, with helpful error messages."""
    text = text.strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError as e:
        if move_type != MoveType.LOAD_PAGE.value:
            # LOAD_PAGE payloads are sometimes bare IDs rather than JSON; suppress noise
            print(f"  [parser] Warning: could not parse JSON in {move_type} move: {e}")
            print(f"  [parser] Raw payload: {text[:200]}")
        return {"_parse_error": str(e), "_raw": text}


def parse_output(raw: str) -> ParsedOutput:
    """Parse all moves, dispatches, and review from a raw LLM response."""
    moves = []
    load_page_ids = []
    for match in MOVE_PATTERN.finditer(raw):
        raw_type = match.group(1).strip().upper()
        try:
            move_type = MoveType(raw_type)
        except ValueError:
            print(f"  [parser] Unknown move type: {raw_type}")
            continue
        payload = _parse_json_payload(match.group(2), raw_type)
        moves.append(Move(move_type=move_type, payload=payload, raw=match.group(0)))
        if move_type is MoveType.LOAD_PAGE:
            pid = payload.get("page_id", "")
            if not pid:
                pid = payload.get("_raw", "").strip().strip('"')
            if pid:
                load_page_ids.append(pid)

    dispatches = []
    for match in DISPATCH_PATTERN.finditer(raw):
        raw_dispatch_type = match.group(1).strip().lower()
        try:
            call_type = CallType(raw_dispatch_type)
        except ValueError:
            print(f"  [parser] Unknown dispatch type: {raw_dispatch_type}")
            continue
        if call_type not in DISPATCHABLE_CALL_TYPES:
            print(f"  [parser] Non-dispatchable call type: {raw_dispatch_type}")
            continue
        payload = _parse_json_payload(match.group(2), f"dispatch:{raw_dispatch_type}")
        dispatches.append(Dispatch(call_type=call_type, payload=payload))

    review = None
    review_match = REVIEW_PATTERN.search(raw)
    if review_match:
        review = _parse_json_payload(review_match.group(1), "review")

    return ParsedOutput(
        moves=moves,
        review=review,
        dispatches=dispatches,
        load_page_ids=load_page_ids,
        raw=raw,
    )
