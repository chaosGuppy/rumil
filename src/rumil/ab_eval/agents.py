"""Backward-compatibility re-exports — canonical definitions live in run_eval."""

from rumil.run_eval.agents import EvalAgentSpec as ABEvalAgentSpec, EVAL_AGENTS

__all__ = ["ABEvalAgentSpec", "EVAL_AGENTS"]
