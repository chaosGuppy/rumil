"""
Abstracted LLM interface. Claude under the hood for now; swappable later.
"""
import os
import time
from pathlib import Path

import anthropic

PROMPTS_DIR = Path(__file__).parent.parent / "prompts"
MODEL = "claude-opus-4-6"


def _load_file(name: str) -> str:
    path = PROMPTS_DIR / name
    if not path.exists():
        raise FileNotFoundError(f"Prompt file not found: {path}")
    return path.read_text(encoding="utf-8")


def build_system_prompt(call_type: str) -> str:
    """
    Combine preamble + call-type instructions into one system prompt.
    Section 1: general workspace preamble (shared across all call types)
    Section 3: call-type-specific instructions
    """
    preamble = _load_file("preamble.md")
    instructions = _load_file(f"{call_type}.md")
    return f"{preamble}\n\n---\n\n{instructions}"


def build_user_message(context_text: str, task_description: str) -> str:
    """
    Combine context dump + specific task into one user message.
    Section 2: context (workspace pages)
    Section 4: specific task for this call
    """
    if context_text:
        return f"{context_text}\n\n---\n\n{task_description}"
    return task_description


def run_llm(
    system_prompt: str,
    user_message: str = "",
    max_tokens: int = 4096,
    max_retries: int = 4,
    messages: list[dict] | None = None,
) -> str:
    """Make a Claude API call. Returns the raw text response.
    Pass `messages` for multi-turn conversations, or `user_message` for single-turn.
    Retries automatically on transient overload or rate-limit errors."""
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        raise EnvironmentError(
            "ANTHROPIC_API_KEY environment variable not set. "
            "Set it before running the workspace."
        )

    client = anthropic.Anthropic(api_key=api_key)
    msg_list = messages if messages is not None else [{"role": "user", "content": user_message}]

    for attempt in range(max_retries):
        try:
            message = client.messages.create(
                model=MODEL,
                max_tokens=max_tokens,
                system=system_prompt,
                messages=msg_list,
            )
            return message.content[0].text
        except Exception as e:
            status = getattr(e, "status_code", None)
            name = type(e).__name__.lower()
            retryable = (
                status in (429, 500, 529)
                or "overloaded" in name
                or "ratelimit" in name
                or "internalserver" in name
                or "overloaded" in str(e).lower()
            )
            if not retryable:
                raise
            if attempt == max_retries - 1:
                raise
            wait = 2 ** attempt  # 1s, 2s, 4s, 8s
            label = f"HTTP {status}" if status else name
            print(f"  [llm] API temporarily unavailable ({label}), "
                  f"retrying in {wait}s... (attempt {attempt + 1}/{max_retries})")
            time.sleep(wait)


def run_call(
    call_type: str,
    task_description: str,
    context_text: str = "",
    max_tokens: int = 4096,
) -> str:
    """
    Run a workspace call of the given type.
    Builds system prompt from preamble + call-type instructions.
    Builds user message from context + task description.
    """
    system_prompt = build_system_prompt(call_type)
    user_message = build_user_message(context_text, task_description)
    return run_llm(system_prompt, user_message, max_tokens=max_tokens)
