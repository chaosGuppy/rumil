"""CLI flag resolution for --prod/--db/--executor.

We don't try to run the orchestrator end-to-end; we exercise the argparse
namespace and the resolver that lives at the top of `async_main` (extracted
into local helpers reachable via `main` import).
"""

from __future__ import annotations

import argparse

import pytest

import main as main_module


def _make_namespace(**overrides) -> argparse.Namespace:
    """Build an argparse.Namespace with the fields the resolver / mode helper read."""
    defaults: dict = {
        "question": None,
        "budget": None,
        "workspace_name": "default",
        "prod_db": False,
        "db": None,
        "executor": None,
        "list": False,
        "list_workspaces": False,
        "evaluate_id": None,
        "ground_call_id": None,
        "feedback_call_id": None,
        "feedback_file": None,
        "show_evaluation_id": None,
        "scope_question": None,
        "chat_id": None,
        "add_question": None,
        "summary_id": None,
        "report_id": None,
        "continue_id": None,
        "batch_file": None,
        "ingest_files": None,
        "run_eval_id": None,
        "ab_eval_ids": None,
        "stage_run_id": None,
        "commit_run_id": None,
    }
    defaults.update(overrides)
    return argparse.Namespace(**defaults)


def _resolve(args: argparse.Namespace) -> tuple[str, str]:
    """Mirror of the resolution block at the top of async_main."""
    if args.prod_db and (args.db is not None or args.executor is not None):
        raise SystemExit("--prod cannot be combined with explicit --db or --executor")
    db_choice = "prod" if args.prod_db else (args.db or "local")
    executor_choice = "prod" if args.prod_db else (args.executor or "local")
    if db_choice == "local" and executor_choice == "prod":
        raise SystemExit("--executor prod requires --db prod")
    args.prod_db = db_choice == "prod"
    return db_choice, executor_choice


def test_default_resolves_to_local_local():
    args = _make_namespace()
    assert _resolve(args) == ("local", "local")
    assert args.prod_db is False


def test_prod_shorthand_resolves_to_prod_prod():
    args = _make_namespace(prod_db=True)
    assert _resolve(args) == ("prod", "prod")
    assert args.prod_db is True


def test_explicit_combo_matches_prod_shorthand():
    explicit = _make_namespace(db="prod", executor="prod")
    shorthand = _make_namespace(prod_db=True)
    assert _resolve(explicit) == _resolve(shorthand)
    assert explicit.prod_db is True
    assert shorthand.prod_db is True


def test_db_prod_executor_local_keeps_local_run():
    args = _make_namespace(db="prod", executor="local")
    assert _resolve(args) == ("prod", "local")
    assert args.prod_db is True


def test_local_db_with_remote_executor_is_rejected():
    args = _make_namespace(db="local", executor="prod")
    with pytest.raises(SystemExit):
        _resolve(args)


def test_prod_with_explicit_db_is_rejected():
    args = _make_namespace(prod_db=True, db="prod")
    with pytest.raises(SystemExit):
        _resolve(args)


def test_prod_with_explicit_executor_is_rejected():
    args = _make_namespace(prod_db=True, executor="prod")
    with pytest.raises(SystemExit):
        _resolve(args)


def test_orchestrator_question_is_not_a_non_orchestrator_mode():
    args = _make_namespace(question="is the sky blue?", budget=1)
    assert main_module._is_non_orchestrator_mode(args) is False


@pytest.mark.parametrize(
    "flag,value",
    [
        ("list", True),
        ("list_workspaces", True),
        ("evaluate_id", "abc"),
        ("ground_call_id", "xyz"),
        ("feedback_call_id", "xyz"),
        ("feedback_file", ("p", "f")),
        ("show_evaluation_id", "z"),
        ("scope_question", "q"),
        ("chat_id", "c"),
        ("add_question", "q"),
        ("summary_id", "s"),
        ("report_id", "r"),
        ("continue_id", "c"),
        ("batch_file", "b.jsonl"),
        ("run_eval_id", "e"),
        ("ab_eval_ids", ["a", "b"]),
        ("stage_run_id", "s"),
        ("commit_run_id", "c"),
    ],
)
def test_each_non_orchestrator_flag_is_detected(flag, value):
    args = _make_namespace(**{flag: value})
    assert main_module._is_non_orchestrator_mode(args) is True


def test_ingest_files_alone_is_non_orchestrator():
    args = _make_namespace(ingest_files=["doc.pdf"])
    assert main_module._is_non_orchestrator_mode(args) is True


def test_ingest_files_with_question_is_orchestrator():
    args = _make_namespace(ingest_files=["doc.pdf"], question="some question", budget=2)
    assert main_module._is_non_orchestrator_mode(args) is False
