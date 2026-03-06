"""
Interactive chat mode: ask questions about research with the full context loaded.
Reads from the workspace; can add questions for investigation via slash commands.
"""
import json
import os
import re
from pathlib import Path

from database import DB
from llm import run_llm
from models import Page, PageLayer, PageLink, PageType, LinkType, Workspace
from orchestrator import Orchestrator
from summary import build_research_tree

PROMPTS_DIR = Path(__file__).parent.parent / "prompts"
DEFAULT_INVESTIGATE_BUDGET = 3

# True-colour ANSI: warm off-white for human, cool off-white for assistant
# Works on Windows 11 terminal and most modern terminals
_RESET  = "\033[0m"
_HUMAN  = "\033[38;2;255;240;210m"   # warm cream
_AI     = "\033[38;2;210;230;255m"   # cool blue-white
_DIM    = "\033[38;2;150;150;150m"   # for system messages / slash command feedback


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

HELP_TEXT = """
Slash commands:
  /add <question text>               Add a sub-question for later investigation
  /investigate <question text>       Add and immediately investigate (default budget: 3)
  /investigate <text> --budget N     Investigate with a specific budget
  /help                              Show this message
  exit / quit                        End the chat
"""


def _load_chat_prompt() -> str:
    return (PROMPTS_DIR / "chat.md").read_text(encoding="utf-8")


def _parse_slash_command(text: str) -> tuple[str, str, int | None] | None:
    """
    Parse a slash command. Returns (command, question_text, budget) or None.
    Handles:
      /add some question text
      /investigate some question text
      /investigate some question text --budget 5
    """
    text = text.strip()
    if not text.startswith("/"):
        return None

    budget = None
    budget_match = re.search(r"--budget\s+(\d+)", text)
    if budget_match:
        budget = int(budget_match.group(1))
        text = text[:budget_match.start()].strip()

    parts = text.split(None, 1)
    command = parts[0].lstrip("/").lower()
    question_text = parts[1].strip().strip('"').strip("'") if len(parts) > 1 else ""

    return command, question_text, budget


def _add_question(question_text: str, parent_id: str, db: DB) -> str:
    """Create a question page linked as a child of parent_id. Returns new page ID."""
    page = Page(
        page_type=PageType.QUESTION,
        layer=PageLayer.SQUIDGY,
        workspace=Workspace.RESEARCH,
        content=question_text,
        summary=question_text[:120],
        epistemic_status=2.5,
        epistemic_type="open question",
        provenance_model="human",
        provenance_call_type="chat",
        provenance_call_id="chat",
        extra=json.dumps({"status": "open"}),
    )
    db.save_page(page)
    link = PageLink(
        from_page_id=parent_id,
        to_page_id=page.id,
        link_type=LinkType.CHILD_QUESTION,
        reasoning="Added during chat session",
    )
    db.save_link(link)
    return page.id


def _handle_slash_command(
    command: str,
    question_text: str,
    budget: int | None,
    scope_question_id: str,
    db: DB,
) -> None:
    if command == "help":
        print(dim(HELP_TEXT))
        return

    if not question_text:
        print(dim(f"  Usage: /{command} <question text>"))
        return

    if command == "add":
        page_id = _add_question(question_text, scope_question_id, db)
        print(dim(f"\n  Question added: {page_id}"))
        print(dim(f"  To investigate later: python main.py --continue {page_id} --budget N"))

    elif command == "investigate":
        effective_budget = budget if budget is not None else DEFAULT_INVESTIGATE_BUDGET
        page_id = _add_question(question_text, scope_question_id, db)
        print(dim(f"\n  Question added: {page_id}"))
        print(dim(f"  Investigating with budget {effective_budget}...\n"))
        db.add_budget(effective_budget)
        Orchestrator(db).run(page_id)
        total, used = db.get_budget()
        print(dim(f"\n  Investigation complete. Budget used: {used}/{total}"))

    else:
        print(dim(f"  Unknown command: /{command}. Type /help for available commands."))


def run_chat(question_id: str, db: DB) -> None:
    """
    Start an interactive chat session grounded in the research for a question.
    Maintains conversation history for follow-up questions.
    Use slash commands to add questions for investigation.
    """
    question = db.get_page(question_id)
    if not question:
        print(f"Question {question_id} not found.")
        return

    print(f"\nLoading research context for: {question.summary[:80]}")
    research_tree = build_research_tree(question_id, db)

    if not research_tree.strip():
        print("No research found for this question yet.")
        return

    system_prompt = (
        f"{_load_chat_prompt()}\n\n"
        f"---\n\n"
        f"## Research Context\n\n"
        f"{research_tree}"
    )

    _enable_ansi_windows()
    print(dim("Ready. Ask anything about this research."))
    print(dim("Type /help for commands, 'exit' to quit.\n"))
    print(dim("-" * 60))

    history: list[dict] = []

    while True:
        try:
            user_input = input(human("\nYou: ")).strip()
        except (EOFError, KeyboardInterrupt):
            print(dim("\n\nExiting chat."))
            break

        if not user_input:
            continue

        if user_input.lower() in ("exit", "quit", "q"):
            print(dim("Exiting chat."))
            break

        # Handle slash commands
        parsed = _parse_slash_command(user_input)
        if parsed is not None:
            command, question_text, budget = parsed
            _handle_slash_command(command, question_text, budget, question_id, db)
            continue

        # Normal chat message
        history.append({"role": "user", "content": user_input})

        try:
            response = run_llm(
                system_prompt=system_prompt,
                messages=history,
                max_tokens=1024,
            )
        except Exception as e:
            print(dim(f"\n[chat] Error: {e}"))
            history.pop()
            continue

        print(f"\n{ai('Assistant:')} {ai(response)}")
        history.append({"role": "assistant", "content": response})
