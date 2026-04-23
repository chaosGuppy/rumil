"""
Interactive chat modes for the research workspace.

Three modes:
- run_chat(): read-only Q&A about existing research (existing)
- run_scoping_chat(): pre-investigation scoping, refines question then kicks off
- run_continuation_chat(): post-investigation discussion, then continues
"""

import asyncio
import json
import logging
import os
import re
import sys
import tempfile
from collections.abc import Awaitable, Callable, Sequence
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import anthropic
from anthropic.types import TextBlock, ToolUseBlock
from pydantic import BaseModel, Field

from rumil.calls.common import execute_tool_uses, prepare_tools
from rumil.constants import MIN_TWOPHASE_BUDGET
from rumil.context import build_embedding_based_context, format_page
from rumil.database import DB
from rumil.embeddings import search_pages
from rumil.events import PageCreatedEvent, fire
from rumil.llm import (
    Tool,
    call_api,
    structured_call,
)
from rumil.models import (
    LinkType,
    Page,
    PageDetail,
    PageLayer,
    PageLink,
    PageType,
    Workspace,
)
from rumil.orchestrators import Orchestrator
from rumil.settings import get_settings
from rumil.sources import create_source_page, run_ingest_calls
from rumil.summary import build_research_tree

log = logging.getLogger(__name__)

PROMPTS_DIR = Path(__file__).parent.parent.parent / "prompts"

# True-colour ANSI: warm off-white for human, cool off-white for assistant
_RESET = "\033[0m"
_HUMAN = "\033[38;2;255;240;210m"  # warm cream
_AI = "\033[38;2;210;230;255m"  # cool blue-white
_DIM = "\033[38;2;150;150;150m"  # for system messages


_SPINNER_FRAMES = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"


@asynccontextmanager
async def _spinner(label: str = "Thinking"):
    """Async context manager that shows an animated spinner on the current line."""
    stop = asyncio.Event()
    is_tty = sys.stdout.isatty()

    async def _animate() -> None:
        i = 0
        while not stop.is_set():
            frame = _SPINNER_FRAMES[i % len(_SPINNER_FRAMES)]
            sys.stdout.write(f"\r{_DIM}{frame} {label}...{_RESET}")
            sys.stdout.flush()
            i += 1
            with suppress(TimeoutError):
                await asyncio.wait_for(stop.wait(), timeout=0.08)
        sys.stdout.write("\r\033[2K")
        sys.stdout.flush()

    if not is_tty:
        yield
        return

    task = asyncio.create_task(_animate())
    try:
        yield
    finally:
        stop.set()
        await task


def _enable_ansi_windows() -> None:
    """Enable ANSI escape codes on Windows if needed."""
    if os.name == "nt":
        import ctypes

        kernel32 = ctypes.windll.kernel32
        kernel32.SetConsoleMode(kernel32.GetStdHandle(-11), 7)


def human(text: str) -> str:
    return f"{_HUMAN}{text}{_RESET}"


def ai(text: str) -> str:
    return f"{_AI}{text}{_RESET}"


def dim(text: str) -> str:
    return f"{_DIM}{text}{_RESET}"


@dataclass
class ChatIO:
    """IO callbacks for the chat loop, decoupling from CLI."""

    get_input: Callable[[], Awaitable[str | None]]
    send_output: Callable[[str], Awaitable[None]]
    send_system: Callable[[str], Awaitable[None]]


def cli_chat_io() -> ChatIO:
    """Create a ChatIO wired to stdin/stdout with ANSI colors."""
    _enable_ansi_windows()

    async def get_input() -> str | None:
        try:
            return input(human("\nYou: ")).strip()
        except (EOFError, KeyboardInterrupt):
            return None

    async def send_output(text: str) -> None:
        print(f"\n{ai('Assistant:')} {ai(text)}")

    async def send_system(text: str) -> None:
        print(dim(text))

    return ChatIO(
        get_input=get_input,
        send_output=send_output,
        send_system=send_system,
    )


SCOPING_HELP_TEXT = (
    "\n"
    "Commands:\n"
    "  /go or /start or /done     Finalize question and begin investigation\n"
    "  /exit                      Cancel without investigating\n"
    "  /help or help              Show this message\n"
)

CONTINUATION_HELP_TEXT = (
    "\n"
    "Commands:\n"
    "  /add <question text>               Add a sub-question for later investigation\n"
    "  /investigate <question text>       Add and immediately investigate (default budget: 4)\n"
    "  /done or /continue                 End chat and resume investigation\n"
    "  /exit                              Cancel without continuing\n"
    "  /help or help                      Show this message\n"
)

READONLY_HELP_TEXT = (
    "\n"
    "Commands:\n"
    "  /add <question text>               Add a sub-question for later investigation\n"
    "  /investigate <question text>       Add and immediately investigate (default budget: 4)\n"
    "  /exit                              End the chat\n"
    "  /help or help                      Show this message\n"
)

DEFAULT_INVESTIGATE_BUDGET = MIN_TWOPHASE_BUDGET


def _load_prompt(name: str) -> str:
    return (PROMPTS_DIR / name).read_text(encoding="utf-8")


def _parse_slash_command(text: str) -> tuple[str, str, int | None] | None:
    """Parse a slash command. Returns (command, question_text, budget) or None."""
    text = text.strip()
    if not text.startswith("/"):
        return None

    budget = None
    budget_match = re.search(r"--budget\s+(\d+)", text)
    if budget_match:
        budget = int(budget_match.group(1))
        text = text[: budget_match.start()].strip()

    parts = text.split(None, 1)
    command = parts[0].lstrip("/").lower()
    question_text = parts[1].strip().strip('"').strip("'") if len(parts) > 1 else ""

    return command, question_text, budget


async def _add_question(question_text: str, parent_id: str, db: DB) -> str:
    """Create a question page linked as a child of parent_id. Returns new page ID."""
    page = Page(
        page_type=PageType.QUESTION,
        layer=PageLayer.SQUIDGY,
        workspace=Workspace.RESEARCH,
        content=question_text,
        headline=question_text,
        provenance_model="human",
        provenance_call_type="chat",
        provenance_call_id="chat",
        extra={"status": "open"},
    )
    await db.save_page(page)
    link = PageLink(
        from_page_id=parent_id,
        to_page_id=page.id,
        link_type=LinkType.CHILD_QUESTION,
        reasoning="Added during chat session",
    )
    await db.save_link(link)
    return page.id


async def _default_slash_handler(
    command: str,
    question_text: str,
    budget: int | None,
    scope_question_id: str,
    db: DB,
    io: ChatIO,
) -> bool:
    """Handle slash commands in the existing chat. Returns True if handled."""
    if not question_text:
        await io.send_system(f"  Usage: /{command} <question text>")
        return True

    if command == "add":
        page_id = await _add_question(question_text, scope_question_id, db)
        await io.send_system(f"\n  Question added: {page_id}")
        await io.send_system(
            f"  To investigate later: python main.py --continue {page_id} --budget N"
        )
        return True

    if command == "investigate":
        effective_budget = budget if budget is not None else DEFAULT_INVESTIGATE_BUDGET
        page_id = await _add_question(question_text, scope_question_id, db)
        await io.send_system(f"\n  Question added: {page_id}")
        await io.send_system(f"  Investigating with budget {effective_budget}...\n")
        await db.init_budget(effective_budget)
        try:
            await Orchestrator(db).run(page_id)
        except Exception as e:
            log.error("Investigation failed: %s", e, exc_info=True)
            await io.send_system(f"\n  Investigation failed: {e}")
            return True
        total, used = await db.get_budget()
        await io.send_system(f"\n  Investigation complete. Budget used: {used}/{total}")
        return True

    await io.send_system(f"  Unknown command: /{command}. Type /help for available commands.")
    return True


def make_workspace_search_tool(db: DB) -> Tool:
    """Create a tool that searches the workspace by embedding similarity."""

    async def search_fn(inp: dict) -> str:
        query = inp.get("query", "")
        if not query:
            return "Error: query is required"
        results = await search_pages(db, query, match_count=8)
        if not results:
            return "No matching pages found."
        parts: list[str] = []
        for page, score in results:
            formatted = await format_page(page, PageDetail.ABSTRACT, db=db)
            parts.append(f"(similarity: {score:.2f})\n{formatted}")
        return "\n\n---\n\n".join(parts)

    return Tool(
        name="search_workspace",
        description=(
            "Search the research workspace for pages relevant to a "
            "natural-language query — phrase the query as a question, "
            "topic, or claim you want to look up. Returns the most "
            "similar pages with abstracts. Use this when the conversation "
            "touches on a topic that might already be covered in the "
            "workspace."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": (
                        "Natural-language search query — a question, "
                        "topic, or claim to look up in the workspace."
                    ),
                },
            },
            "required": ["query"],
        },
        fn=search_fn,
    )


def _build_source_context(source_pages: Sequence[Page]) -> str:
    """Format source page content for inclusion in a chat system prompt."""
    if not source_pages:
        return ""
    parts = ["## Provided Source Material\n"]
    for page in source_pages:
        content = page.content or ""
        if len(content) > 15000:
            content = content[:15000] + "\n\n[Truncated — full document is longer]"
        parts.append(f"### {page.headline}\n\n{content}\n")
    return "\n\n---\n\n" + "\n".join(parts)


def _render_transcript_markdown(history: Sequence[dict]) -> str:
    """Render a message history as a markdown transcript."""
    parts = ["# Chat Transcript\n"]
    for msg in history:
        role = msg.get("role", "unknown")
        content = msg.get("content", "")
        if role == "user":
            if isinstance(content, str):
                parts.append(f"## Human\n\n{content}\n")
            elif isinstance(content, list):
                # tool_result messages — skip in transcript
                continue
        elif role == "assistant":
            if isinstance(content, str):
                parts.append(f"## Assistant\n\n{content}\n")
            elif isinstance(content, list):
                text_parts: list[str] = []
                for block in content:
                    if isinstance(block, dict):
                        if block.get("type") == "text":
                            text_parts.append(block["text"])
                        elif block.get("type") == "tool_use":
                            block.get("name", "unknown")
                            tool_input = block.get("input", {})
                            query = tool_input.get("query", json.dumps(tool_input))
                            text_parts.append(f"*[Searched workspace for: {query}]*")
                if text_parts:
                    parts.append("## Assistant\n\n" + "\n\n".join(text_parts) + "\n")
    return "\n".join(parts)


async def _package_transcript(
    history: Sequence[dict],
    question_id: str,
    db: DB,
    io: ChatIO,
) -> Page | None:
    """Save chat transcript as a source page and ingest it."""
    markdown = _render_transcript_markdown(history)
    if len(markdown.strip()) < 50:
        return None

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    with tempfile.NamedTemporaryFile(
        mode="w",
        suffix=".md",
        prefix=f"chat_transcript_{timestamp}_",
        delete=False,
        encoding="utf-8",
    ) as f:
        f.write(markdown)
        filepath = f.name

    try:
        await io.send_system("  Packaging chat transcript...")
        source_page = await create_source_page(filepath, db)
        if source_page:
            await io.send_system("  Ingesting transcript...")
            await run_ingest_calls([source_page], question_id, db)
            return source_page
        return None
    finally:
        Path(filepath).unlink(missing_ok=True)


async def _chat_loop(
    system_prompt: str,
    io: ChatIO,
    db: DB,
    tools: Sequence[Tool] = (),
    slash_handler: Callable[[str, str, int | None, str, DB, ChatIO], Awaitable[bool]] | None = None,
    scope_question_id: str | None = None,
    proceed_commands: Sequence[str] = (),
    help_text: str = READONLY_HELP_TEXT,
    initial_message: str | None = None,
) -> tuple[list[dict], str]:
    """Core multi-turn chat loop with tool support.

    Returns ``(history, exit_command)`` where *exit_command* is the slash
    command that ended the loop (e.g. ``"exit"``, ``"go"``, ``"done"``),
    or ``""`` for EOF/Ctrl-C.

    If *initial_message* is provided it is sent as the first user turn
    without waiting for input, so the LLM responds immediately.
    """
    settings = get_settings()
    client = anthropic.AsyncAnthropic(api_key=settings.require_anthropic_key())
    model = settings.model

    tool_defs: list[dict] = []
    tool_fns: dict = {}
    if tools:
        tool_defs, tool_fns = prepare_tools(list(tools))

    exit_commands = {"exit", "quit", "q"} | set(proceed_commands)

    history: list[dict] = []
    exit_command = ""
    pending_input: str | None = initial_message

    while True:
        if pending_input is not None:
            user_input = pending_input
            pending_input = None
        else:
            user_input = await io.get_input()
            if user_input is None:
                await io.send_system("\n\nExiting chat.")
                break

            if not user_input:
                continue

            # "help" works without slash
            if user_input.strip().lower() == "help":
                await io.send_system(help_text)
                continue

            # All control commands use /
            parsed = _parse_slash_command(user_input)
            if parsed is not None:
                command, question_text, budget = parsed

                if command == "help":
                    await io.send_system(help_text)
                    continue

                if command in exit_commands:
                    exit_command = command
                    await io.send_system(f"Ending chat (/{command}).")
                    break

                if slash_handler and scope_question_id:
                    await slash_handler(
                        command,
                        question_text,
                        budget,
                        scope_question_id,
                        db,
                        io,
                    )
                else:
                    await io.send_system(
                        f"  Unknown command: /{command}. Type /help for available commands."
                    )
                continue

        # Normal message — add to history and call API
        history.append({"role": "user", "content": user_input})

        try:
            # Inner tool loop: keep calling API until no more tool use
            messages = list(history)
            round_num = 0
            while True:
                label = "Thinking" if round_num == 0 else "Searching"
                async with _spinner(label):
                    api_resp = await call_api(
                        client,
                        model,
                        system_prompt,
                        messages,
                        tools=tool_defs or None,
                        cache=True,
                    )

                response = api_resp.message
                text_parts: list[str] = []
                tool_uses: list[ToolUseBlock] = []
                for block in response.content:
                    if isinstance(block, TextBlock):
                        text_parts.append(block.text)
                    elif isinstance(block, ToolUseBlock):
                        tool_uses.append(block)

                if tool_uses:
                    # Execute tools and continue the loop
                    _, tool_results = await execute_tool_uses(tool_uses, tool_fns)

                    # Build the assistant content blocks for history
                    assistant_content = [block.model_dump() for block in response.content]
                    messages.append({"role": "assistant", "content": assistant_content})
                    messages.append({"role": "user", "content": tool_results})

                    if text_parts:
                        await io.send_system("  " + " ".join(text_parts))

                    if response.stop_reason == "end_turn":
                        break
                    round_num += 1
                else:
                    # No tool use — display response and break
                    response_text = "\n".join(text_parts)
                    await io.send_output(response_text)
                    messages.append({"role": "assistant", "content": response_text})
                    break

            # Sync history with the full messages (includes tool rounds)
            history = messages

        except Exception as e:
            log.error("Chat LLM call failed: %s", e, exc_info=True)
            await io.send_system(f"\n[chat] Error: {e}")
            history.pop()  # Remove the failed user message

    return history, exit_command


class RefinedQuestion(BaseModel):
    """Extracted question from a scoping chat transcript."""

    headline: str = Field(
        description=(
            "The final refined question wording. 10-20 words, "
            "phrased as a question. Use the last version the "
            "user agreed to, or the original if none was refined."
        ),
    )
    abstract: str = Field(
        default="",
        description=(
            "1-3 sentence summary of the question's scope and "
            "purpose, as clarified during the conversation."
        ),
    )
    content: str = Field(
        default="",
        description=(
            "Fuller description incorporating the key scoping decisions, "
            "constraints, and context from the conversation."
        ),
    )


async def _extract_question(
    history: Sequence[dict],
    original_question: str,
) -> RefinedQuestion:
    """Extract the final agreed question from a scoping chat transcript."""
    transcript = _render_transcript_markdown(history)
    result = await structured_call(
        (
            "You are extracting the final research question from a scoping conversation. "
            "The researcher started with an initial question and refined it through "
            "discussion. Extract the final version they agreed on — the most recent "
            "question formulation the researcher accepted or proposed. If no refinement "
            "was made, use the original question.\n\n"
            f"Original question: {original_question}"
        ),
        user_message=transcript,
        response_model=RefinedQuestion,
    )
    if result.parsed:
        return result.parsed
    return RefinedQuestion(headline=original_question, content=original_question)


async def run_scoping_chat(
    initial_question: str,
    db: DB,
    budget: int,
    io: ChatIO | None = None,
    source_pages: Sequence[Page] = (),
) -> str:
    """Pre-investigation scoping chat. Returns the question_id of the created question."""
    if io is None:
        io = cli_chat_io()

    await io.send_system(f"\nScoping chat for: {initial_question}")
    await io.send_system("Searching workspace for relevant context...\n")

    # Build initial context from workspace (may be empty)
    context_result = await build_embedding_based_context(initial_question, db)
    context_section = ""
    if context_result.context_text.strip():
        context_section = (
            "\n\n---\n\n## Existing Workspace Context\n\n" + context_result.context_text
        )
    context_section += _build_source_context(source_pages)

    system_prompt = _load_prompt("scoping_chat.md") + context_section
    search_tool = make_workspace_search_tool(db)

    await io.send_system("Ready. Discuss your question to refine it before investigation.")
    await io.send_system("Type /go, /start, or /done when ready to begin investigation.")
    await io.send_system("Type /exit to cancel. Type /help for commands.\n")
    await io.send_system("-" * 60)

    history, exit_cmd = await _chat_loop(
        system_prompt,
        io,
        db,
        tools=[search_tool],
        proceed_commands=("go", "start", "done"),
        help_text=SCOPING_HELP_TEXT,
        initial_message=initial_question,
    )

    if exit_cmd in ("exit", "quit", "q", ""):
        await io.send_system("Chat cancelled.")
        return ""

    if not history:
        await io.send_system("No conversation recorded. Using original question.")
        question = await _extract_question([], initial_question)
    else:
        await io.send_system("\nExtracting refined question from conversation...")
        question = await _extract_question(history, initial_question)

    await io.send_system(f"\nRefined question: {question.headline}")

    # Create the question page
    page = Page(
        page_type=PageType.QUESTION,
        layer=PageLayer.SQUIDGY,
        workspace=Workspace.RESEARCH,
        content=question.content or question.abstract or question.headline,
        headline=question.headline,
        abstract=question.abstract,
        provenance_model="human",
        provenance_call_type="scoping_chat",
        provenance_call_id="scoping_chat",
        extra={"status": "open"},
    )
    await db.save_page(page)
    await io.send_system(f"Question created: {page.id}")

    # Print trace info early, before ingestion logging starts
    frontend = get_settings().frontend_url.rstrip("/")
    print(f"\nStarting investigation with budget {budget}...")
    print(f"Question ID: {page.id}")
    print(f"Trace: {frontend}/traces/{db.run_id}\n")

    # Init budget before ingestion so the ingest call has budget to use
    await db.init_budget(budget)

    # Package and ingest the transcript
    if history:
        await _package_transcript(history, page.id, db, io)

    # Ingest user-provided source files
    if source_pages:
        print(f"Ingesting {len(source_pages)} source file(s)...")
        await run_ingest_calls(list(source_pages), page.id, db)

    # Fire AFTER ingest so any registered hook (e.g. auto-CreateView) sees the
    # ingested considerations in context rather than a bare question.
    await fire(
        PageCreatedEvent(
            page_id=page.id,
            page_type=PageType.QUESTION,
            run_id=db.run_id,
            staged=db.staged,
            db=db,
        )
    )

    await Orchestrator(db).run(page.id)

    return page.id


async def run_continuation_chat(
    question_id: str,
    db: DB,
    budget: int,
    io: ChatIO | None = None,
    source_pages: Sequence[Page] = (),
) -> None:
    """Post-investigation chat, then continue with more budget."""
    if io is None:
        io = cli_chat_io()

    question = await db.get_page(question_id)
    if not question:
        await io.send_system(f"Question {question_id} not found.")
        return

    await io.send_system(f"\nLoading research context for: {question.headline[:80]}")

    # Build rich context: embedding-based for page details + research tree for structure
    embed_result = await build_embedding_based_context(
        question.headline,
        db,
        scope_question_id=question_id,
    )
    research_tree = await build_research_tree(question_id, db)

    context_parts = []
    if embed_result.context_text.strip():
        context_parts.append("## Workspace Pages\n\n" + embed_result.context_text)
    if research_tree.strip():
        context_parts.append("## Research Tree\n\n" + research_tree)

    context_section = ""
    if context_parts:
        context_section = "\n\n---\n\n" + "\n\n---\n\n".join(context_parts)
    context_section += _build_source_context(source_pages)

    system_prompt = _load_prompt("continuation_chat.md") + context_section
    search_tool = make_workspace_search_tool(db)

    await io.send_system(
        "Ready. Discuss the findings, ask questions, or provide additional context."
    )
    await io.send_system("Type /done or /continue to end chat and resume investigation.")
    await io.send_system("Type /exit to cancel. Type /help for commands.\n")
    await io.send_system("-" * 60)

    history, exit_cmd = await _chat_loop(
        system_prompt,
        io,
        db,
        tools=[search_tool],
        slash_handler=_default_slash_handler,
        scope_question_id=question_id,
        proceed_commands=("done", "continue"),
        help_text=CONTINUATION_HELP_TEXT,
    )

    if exit_cmd in ("exit", "quit", "q", ""):
        await io.send_system("Chat cancelled. Investigation not continued.")
        return

    if not history:
        await io.send_system("No conversation recorded.")
        return

    # Print trace info early, before ingestion logging starts
    frontend = get_settings().frontend_url.rstrip("/")
    print(f"\nContinuing investigation with budget {budget}...")
    print(f"Trace: {frontend}/traces/{db.run_id}\n")

    # Add budget before ingestion so the ingest call has budget to use
    await db.add_budget(budget)

    # Package and ingest transcript
    await _package_transcript(history, question_id, db, io)

    # Ingest user-provided source files
    if source_pages:
        print(f"Ingesting {len(source_pages)} source file(s)...")
        await run_ingest_calls(list(source_pages), question_id, db)

    orch = Orchestrator(db)
    # Set ingest hint about the chat
    orch.ingest_hint = (
        "The researcher just had a conversation about the investigation findings. "
        "The transcript has been ingested as a source. The researcher may have "
        "provided corrections, additional context, or steering for the next phase. "
        "The View for this question has NOT yet been updated to reflect this new "
        "material — any corrections or new context from the conversation are not "
        "yet factored into View scores or item selection."
    )
    await orch.run(question_id)


async def run_chat(question_id: str, db: DB) -> None:
    """Start an interactive read-only chat about existing research."""
    question = await db.get_page(question_id)
    if not question:
        print(f"Question {question_id} not found.")
        return

    print(f"\nLoading research context for: {question.headline[:80]}")
    research_tree = await build_research_tree(question_id, db)

    if not research_tree.strip():
        print("No research found for this question yet.")
        return

    system_prompt = f"{_load_prompt('chat.md')}\n\n---\n\n## Research Context\n\n{research_tree}"

    io = cli_chat_io()
    await io.send_system("Ready. Ask anything about this research.")
    await io.send_system("Type /help for commands, /exit to quit.\n")
    await io.send_system("-" * 60)

    await _chat_loop(
        system_prompt,
        io,
        db,
        slash_handler=_default_slash_handler,
        scope_question_id=question_id,
        help_text=READONLY_HELP_TEXT,
    )
