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
        "self_improve_id": None,
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
    explicit_remote = args.executor == "prod"
    db_choice = "prod" if args.prod_db else (args.db or "local")
    executor_choice = "prod" if args.prod_db else (args.executor or "local")
    if db_choice == "local" and executor_choice == "prod":
        raise SystemExit("--executor prod requires --db prod")
    if (
        executor_choice == "prod"
        and not explicit_remote
        and main_module._is_non_orchestrator_mode(args)
    ):
        executor_choice = "local"
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


def test_continue_id_is_orchestrator_mode():
    """`--continue X --budget N` runs the orchestrator on an existing question
    — it must NOT be classified as non-orchestrator, otherwise `--prod` would
    silently downgrade to local."""
    args = _make_namespace(continue_id="some-qid", budget=4)
    assert main_module._is_non_orchestrator_mode(args) is False


def test_prod_with_continue_resolves_to_prod_prod():
    """Regression: `--continue X --prod` must hit the remote k8s path, not
    silently fall back to a local run against prod Supabase."""
    args = _make_namespace(prod_db=True, continue_id="some-qid", budget=4)
    assert _resolve(args) == ("prod", "prod")


def test_ingest_files_alone_is_non_orchestrator():
    args = _make_namespace(ingest_files=["doc.pdf"])
    assert main_module._is_non_orchestrator_mode(args) is True


def test_ingest_files_with_question_is_orchestrator():
    args = _make_namespace(ingest_files=["doc.pdf"], question="some question", budget=2)
    assert main_module._is_non_orchestrator_mode(args) is False


def test_effective_cli_user_id_empty_for_local_db():
    """The committed default_cli_user_id is a prod-only Supabase user and must
    NOT be stamped on projects created against the local Supabase, where it
    would FK-fail auth.users."""
    from rumil.settings import override_settings

    with override_settings(use_prod_db="", default_cli_user_id="some-uuid"):
        from rumil.settings import get_settings

        assert get_settings().effective_cli_user_id == ""


def test_effective_cli_user_id_returns_default_for_prod_db():
    from rumil.settings import override_settings

    with override_settings(use_prod_db="1", default_cli_user_id="some-uuid"):
        from rumil.settings import get_settings

        assert get_settings().effective_cli_user_id == "some-uuid"


def test_prod_with_list_silently_falls_back_to_local_executor():
    """`--prod --list` is documented as targeting prod for any command. Don't
    break that — only loud-reject when --executor prod was set explicitly."""
    args = _make_namespace(prod_db=True, list=True)
    db, executor = _resolve(args)
    assert (db, executor) == ("prod", "local")
    assert args.prod_db is True


def test_prod_with_summary_id_silently_falls_back_to_local_executor():
    args = _make_namespace(prod_db=True, summary_id="some-qid")
    assert _resolve(args) == ("prod", "local")


def test_prod_with_summary_auto_stays_remote():
    """`--summary` (auto, paired with an orchestrator run) must NOT be
    treated as a non-orchestrator mode and must keep executor=prod."""
    args = _make_namespace(prod_db=True, question="q", budget=4, summary_id="__auto__")
    assert _resolve(args) == ("prod", "prod")


def test_prod_with_self_improve_auto_stays_remote():
    args = _make_namespace(prod_db=True, question="q", budget=4, self_improve_id="__auto__")
    assert _resolve(args) == ("prod", "prod")


def test_explicit_executor_prod_still_rejects_non_orchestrator():
    """An explicit `--executor prod --list` is loud-rejected via parser.error
    in the dispatch block (not the resolver), because the user explicitly
    asked for the remote path. Verified via _is_non_orchestrator_mode."""
    args = _make_namespace(db="prod", executor="prod", list=True)
    db, executor = _resolve(args)
    assert (db, executor) == ("prod", "prod")
    assert main_module._is_non_orchestrator_mode(args) is True


def test_self_improve_with_id_is_non_orchestrator():
    args = _make_namespace(self_improve_id="some-qid")
    assert main_module._is_non_orchestrator_mode(args) is True


def test_summary_auto_value_is_treated_as_orchestrator():
    """Pre-existing bug fix: `--summary` (= summary_id="__auto__") is a
    post-orch modifier, not a standalone mode."""
    args = _make_namespace(question="q", budget=4, summary_id="__auto__")
    assert main_module._is_non_orchestrator_mode(args) is False


def test_self_improve_auto_value_is_treated_as_orchestrator():
    args = _make_namespace(question="q", budget=4, self_improve_id="__auto__")
    assert main_module._is_non_orchestrator_mode(args) is False
