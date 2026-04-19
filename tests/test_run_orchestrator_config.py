"""Tests for orchestrator-name injection into runs.config.

The trace UI reads ``config["orchestrator"]`` to label a run with the
canonical orchestrator that produced it, falling back to
``config["prioritizer_variant"]`` (which only reflects the factory-dispatch
setting). Orchestrators that bypass the factory — e.g. the refine-artifact
loop — must explicitly inject ``orchestrator`` at ``create_run()`` time so
the UI doesn't show a misleading default.
"""

from pytest_mock import MockerFixture

from rumil.database import DB


async def test_create_run_injects_orchestrator_into_config(tmp_db: DB) -> None:
    await tmp_db.create_run(
        name="r1",
        question_id=None,
        config={"prioritizer_variant": "two_phase", "model": "claude-sonnet-4-6"},
        orchestrator="refine_artifact",
    )

    row = await tmp_db.get_run(tmp_db.run_id)
    assert row is not None
    config = row["config"]
    assert config["orchestrator"] == "refine_artifact"
    assert config["prioritizer_variant"] == "two_phase"
    assert config["model"] == "claude-sonnet-4-6"


async def test_create_run_without_orchestrator_omits_key(tmp_db: DB) -> None:
    await tmp_db.create_run(
        name="r2",
        question_id=None,
        config={"prioritizer_variant": "two_phase"},
    )

    row = await tmp_db.get_run(tmp_db.run_id)
    assert row is not None
    assert "orchestrator" not in row["config"]
    assert row["config"]["prioritizer_variant"] == "two_phase"


async def test_create_run_does_not_mutate_caller_config(tmp_db: DB) -> None:
    caller_config = {"prioritizer_variant": "two_phase"}
    await tmp_db.create_run(
        name="r3",
        question_id=None,
        config=caller_config,
        orchestrator="claim_investigation",
    )

    assert "orchestrator" not in caller_config


async def test_create_run_with_none_config_and_orchestrator(tmp_db: DB) -> None:
    await tmp_db.create_run(
        name="r4",
        question_id=None,
        config=None,
        orchestrator="two_phase",
    )

    row = await tmp_db.get_run(tmp_db.run_id)
    assert row is not None
    assert row["config"] == {"orchestrator": "two_phase"}


async def test_cmd_refine_artifact_records_refine_artifact_orchestrator(
    tmp_db: DB, mocker: MockerFixture
) -> None:
    """The refine-artifact CLI entrypoint must tag its run with
    orchestrator='refine_artifact' so the trace UI shows the real
    orchestrator rather than the default prioritizer_variant."""

    import main
    from rumil.models import Page, PageLayer, PageType, Workspace
    from rumil.orchestrators.refine_artifact import RefineArtifactResult

    question = Page(
        page_type=PageType.QUESTION,
        layer=PageLayer.SQUIDGY,
        workspace=Workspace.RESEARCH,
        content="q?",
        headline="q?",
    )
    question.project_id = tmp_db.project_id
    await tmp_db.save_page(question)

    class _FakeOrch:
        def __init__(self, *args, **kwargs):
            pass

        async def run(self):
            return RefineArtifactResult(
                outcome="accepted",
                final_artifact_id=None,
                iterations=[],
            )

    mocker.patch("rumil.orchestrators.RefineArtifactOrchestrator", _FakeOrch)

    await main.cmd_refine_artifact(
        question_id=question.id,
        shape="strategy_brief",
        db=tmp_db,
        budget=1,
        max_iterations=1,
    )

    row = await tmp_db.get_run(tmp_db.run_id)
    assert row is not None
    assert row["config"]["orchestrator"] == "refine_artifact"
