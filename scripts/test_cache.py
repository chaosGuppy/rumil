"""Test whether parse() busts cache when messages contain SDK objects (tool use)."""

import asyncio

import anthropic
from pydantic import BaseModel, Field

from differential.settings import get_settings


SYSTEM = "You are a helpful assistant. " * 2000  # ~8k tokens to exceed 4096 minimum
TOOLS = [
    {
        "name": "do_thing",
        "description": "Does a thing. Call this tool with any input.",
        "input_schema": {"type": "object", "properties": {"x": {"type": "string"}}},
    }
]
USER_MSG = "Please call the do_thing tool with x='hello'."
FOLLOW_UP = "Now rate your confidence from 0-10."
MODEL = "claude-opus-4-6"
BP = {"type": "ephemeral"}


class Rating(BaseModel):
    score: int = Field(description="0-10 rating")
    reason: str = Field(description="Why")


def report(label: str, usage):
    cache_write = getattr(usage, "cache_creation_input_tokens", 0) or 0
    cache_read = getattr(usage, "cache_read_input_tokens", 0) or 0
    print(
        f"  {label}: input={usage.input_tokens} "
        f"cache_write={cache_write} cache_read={cache_read} "
        f"output={usage.output_tokens}"
    )


def add_bp(messages):
    """Add cache breakpoint to last message (shallow copy)."""
    msgs = list(messages)
    last = dict(msgs[-1])
    content = last.get("content")
    if isinstance(content, str):
        last["content"] = [{"type": "text", "text": content, "cache_control": BP}]
    elif isinstance(content, list) and content:
        content = list(content)
        block = content[-1]
        if isinstance(block, dict):
            content[-1] = {**block, "cache_control": BP}
        else:
            content[-1] = {**block.model_dump(), "cache_control": BP}
        last["content"] = content
    msgs[-1] = last
    return msgs


async def main():
    api_key = get_settings().require_anthropic_key()
    client = anthropic.AsyncAnthropic(api_key=api_key)

    messages = [{"role": "user", "content": USER_MSG}]

    # Call 1: create() with tools — force a tool call
    print("Call 1: create() — get a tool call")
    r1 = await client.messages.create(
        model=MODEL, max_tokens=256, system=SYSTEM,
        tools=TOOLS, messages=add_bp(messages),
    )
    report("create", r1.usage)

    # Append assistant response (contains ToolUseBlock SDK objects)
    messages.append({"role": "assistant", "content": r1.content})
    print(f"  assistant content types: {[type(b).__name__ for b in r1.content]}")

    # Append tool result
    tool_use = next(b for b in r1.content if b.type == "tool_use")
    messages.append({
        "role": "user",
        "content": [
            {"type": "tool_result", "tool_use_id": tool_use.id, "content": "Done!"},
            {"type": "text", "text": "You have 3 rounds remaining."},
        ],
    })

    # Call 2: create() with tool results — should read cache from call 1
    print("Call 2: create() after tool use — cache hit?")
    r2 = await client.messages.create(
        model=MODEL, max_tokens=256, system=SYSTEM,
        tools=TOOLS, messages=add_bp(messages),
    )
    report("create", r2.usage)

    # Append final assistant response (end_turn, has SDK TextBlock objects)
    messages.append({"role": "assistant", "content": r2.content})
    print(f"  assistant content types: {[type(b).__name__ for b in r2.content]}")

    # Now mimic fruit check: append a user message and try parse()
    fruit_messages = list(messages) + [
        {"role": "user", "content": FOLLOW_UP},
    ]

    # Call 3: create() with fruit-check-style appended msg — cache hit?
    print("Call 3: create() with appended msg — cache hit?")
    r3 = await client.messages.create(
        model=MODEL, max_tokens=256, system=SYSTEM,
        tools=TOOLS, messages=add_bp(fruit_messages),
    )
    report("create", r3.usage)

    # Call 4: parse() with same messages — cache hit?
    print("Call 4: parse() with same messages + output_format — cache hit?")
    r4 = await client.messages.parse(
        model=MODEL, max_tokens=256, system=SYSTEM,
        tools=TOOLS, messages=add_bp(fruit_messages),
        output_format=Rating,
    )
    report("parse", r4.usage)

    # Call 5: parse() again — should at least read call 4's cache
    print("Call 5: parse() again — should read call 4's cache")
    r5 = await client.messages.parse(
        model=MODEL, max_tokens=256, system=SYSTEM,
        tools=TOOLS, messages=add_bp(fruit_messages),
        output_format=Rating,
    )
    report("parse again", r5.usage)


asyncio.run(main())
