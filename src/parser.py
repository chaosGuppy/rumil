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


MOVE_PATTERN = re.compile(
    r'<move\s+type=["\']([^"\']+)["\']\s*>\s*(.*?)\s*</move>',
    re.DOTALL | re.IGNORECASE,
)

REVIEW_PATTERN = re.compile(
    r'<review>\s*(.*?)\s*</review>',
    re.DOTALL | re.IGNORECASE,
)

DISPATCH_PATTERN = re.compile(
    r'<dispatch\s+type=["\']([^"\']+)["\']\s*>\s*(.*?)\s*</dispatch>',
    re.DOTALL | re.IGNORECASE,
)


@dataclass
class Move:
    move_type: str
    payload: dict[str, Any]
    raw: str


@dataclass
class ParsedOutput:
    moves: list[Move]
    review: Optional[dict[str, Any]]
    dispatches: list[dict[str, Any]]   # for prioritization calls
    load_page_ids: list[str]           # short IDs from LOAD_PAGE moves (phase 1 only)
    raw: str


def _parse_json_payload(text: str, move_type: str) -> dict[str, Any]:
    """Parse JSON from a move payload, with helpful error messages."""
    text = text.strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError as e:
        if move_type != "LOAD_PAGE":
            # LOAD_PAGE payloads are sometimes bare IDs rather than JSON; suppress noise
            print(f"  [parser] Warning: could not parse JSON in {move_type} move: {e}")
            print(f"  [parser] Raw payload: {text[:200]}")
        return {"_parse_error": str(e), "_raw": text}


def parse_output(raw: str) -> ParsedOutput:
    """Parse all moves, dispatches, and review from a raw LLM response."""
    moves = []
    load_page_ids = []
    for match in MOVE_PATTERN.finditer(raw):
        move_type = match.group(1).strip().upper()
        payload = _parse_json_payload(match.group(2), move_type)
        moves.append(Move(move_type=move_type, payload=payload, raw=match.group(0)))
        if move_type == "LOAD_PAGE":
            pid = payload.get("page_id", "")
            if pid:
                load_page_ids.append(pid)

    dispatches = []
    for match in DISPATCH_PATTERN.finditer(raw):
        dispatch_type = match.group(1).strip().lower()
        payload = _parse_json_payload(match.group(2), f"dispatch:{dispatch_type}")
        payload["call_type"] = dispatch_type
        dispatches.append(payload)

    review = None
    review_match = REVIEW_PATTERN.search(raw)
    if review_match:
        review = _parse_json_payload(review_match.group(1), "review")

    return ParsedOutput(
        moves=moves, review=review, dispatches=dispatches,
        load_page_ids=load_page_ids, raw=raw,
    )
