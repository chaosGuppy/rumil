"""Shared fixtures for tests."""

import sys
import uuid
from pathlib import Path

import pytest
import pytest_asyncio

_SKILLS_LIB = Path(__file__).resolve().parent.parent / ".claude" / "lib"
if str(_SKILLS_LIB) not in sys.path:
    sys.path.insert(0, str(_SKILLS_LIB))

from rumil.calls.common import RunCallResult
from rumil.database import DB
from rumil.models import (
    Call,
    CallStatus,
    CallType,
    LinkType,
    Page,
    PageLayer,
    PageLink,
    PageType,
    Workspace,
)
from rumil.prioritisers.dispatch import DispatchRunner
from rumil.settings import override_settings


@pytest.fixture(autouse=True, scope="session")
def _test_settings():
    """Activate test-mode settings for the entire test session."""
    with override_settings(rumil_test_mode="1"):
        yield


@pytest.fixture(autouse=True)
def _block_real_llm_calls(request, monkeypatch):
    """Fail loudly if a non-LLM test reaches an unmocked LLM path.

    Every Anthropic client construction in rumil routes through
    ``Settings.require_anthropic_key``. Tests marked ``llm`` need a real
    key; everything else should be fully mocked. If a supposedly-mocked
    test still reaches this chokepoint, it's silently making real API
    calls — raise instead.
    """
    if request.node.get_closest_marker("llm"):
        return
    from rumil.settings import Settings

    def _raise(self):
        raise RuntimeError(
            f"Test {request.node.nodeid} reached an unmocked LLM call. "
            "Add the missing mock (see prio_harness for orchestrator patterns) "
            "or mark the test with @pytest.mark.llm."
        )

    monkeypatch.setattr(Settings, "require_anthropic_key", _raise)


def pytest_addoption(parser):
    parser.addoption(
        "--llm",
        action="store_true",
        default=False,
        help="Run tests that call the real LLM API",
    )
    parser.addoption(
        "--integration",
        action="store_true",
        default=False,
        help="Run slow integration tests (implies --llm)",
    )


def pytest_configure(config):
    config.addinivalue_line("markers", "llm: tests that call the real LLM API")
    config.addinivalue_line(
        "markers", "integration: slow integration tests that call the real LLM API"
    )


def pytest_collection_modifyitems(config, items):
    run_llm = config.getoption("--llm")
    run_integration = config.getoption("--integration")
    if run_integration:
        run_llm = True

    for item in items:
        if "integration" in item.keywords and not run_integration:
            item.add_marker(pytest.mark.skip(reason="needs --integration flag to run"))
        elif "llm" in item.keywords and not run_llm:
            item.add_marker(pytest.mark.skip(reason="needs --llm flag to run"))


@pytest_asyncio.fixture
async def envelope_cleanup():
    """Track envelope run_ids and clean up their data + projects on teardown.

    Tests append run_ids produced by ensure_chat_envelope(). The fixture
    looks up the project_id from each run row, deletes all run data first,
    then deletes the unique projects last (avoiding FK violations).
    """
    run_ids: list[str] = []

    yield run_ids

    project_ids: set[str] = set()
    for run_id in reversed(run_ids):
        cleanup_db = await DB.create(run_id=run_id)
        try:
            rows = (
                await cleanup_db._execute(
                    cleanup_db.client.table("runs").select("project_id").eq("id", run_id)
                )
            ).data
            if rows and rows[0].get("project_id"):
                project_ids.add(rows[0]["project_id"])
            await cleanup_db.delete_run_data()
        finally:
            await cleanup_db.close()
    if project_ids:
        cleanup_db = await DB.create(run_id="cleanup")
        try:
            for pid in project_ids:
                await cleanup_db._execute(
                    cleanup_db.client.table("projects").delete().eq("id", pid)
                )
        finally:
            await cleanup_db.close()


@pytest_asyncio.fixture
async def tmp_db():
    """Create a DB with a unique run_id and throwaway project for test isolation."""
    run_id = str(uuid.uuid4())
    db = await DB.create(run_id=run_id)
    project = await db.get_or_create_project(f"test-{run_id[:8]}")
    db.project_id = project.id
    await db.init_budget(100)
    yield db
    await db.delete_run_data(delete_project=True)


@pytest_asyncio.fixture
async def question_page(tmp_db):
    """Create and return a question page in the DB (TAI-framed so LLM tests don't get refused)."""
    page = Page(
        page_type=PageType.QUESTION,
        layer=PageLayer.SQUIDGY,
        workspace=Workspace.RESEARCH,
        content="How quickly will frontier AI automate routine cognitive labour?",
        headline="How quickly will frontier AI automate routine cognitive labour?",
    )
    await tmp_db.save_page(page)
    return page


@pytest_asyncio.fixture
async def scout_call(tmp_db, question_page):
    """Create a pending find_considerations call, saved to DB."""
    call = Call(
        call_type=CallType.FIND_CONSIDERATIONS,
        workspace=Workspace.RESEARCH,
        scope_page_id=question_page.id,
        status=CallStatus.PENDING,
    )
    await tmp_db.save_call(call)
    return call


@pytest_asyncio.fixture
async def assess_call(tmp_db, question_page):
    """Create a pending assess call, saved to DB."""
    call = Call(
        call_type=CallType.ASSESS,
        workspace=Workspace.RESEARCH,
        scope_page_id=question_page.id,
        status=CallStatus.PENDING,
    )
    await tmp_db.save_call(call)
    return call


@pytest_asyncio.fixture
async def prioritization_call(tmp_db, question_page):
    """Create a pending prioritization call, saved to DB."""
    call = Call(
        call_type=CallType.PRIORITIZATION,
        workspace=Workspace.PRIORITIZATION,
        scope_page_id=question_page.id,
        status=CallStatus.PENDING,
        budget_allocated=5,
    )
    await tmp_db.save_call(call)
    return call


@pytest_asyncio.fixture
async def child_question_page(tmp_db, question_page):
    """Create a child question linked under question_page via CHILD_QUESTION."""
    child = Page(
        page_type=PageType.QUESTION,
        layer=PageLayer.SQUIDGY,
        workspace=Workspace.RESEARCH,
        content="Which cognitive tasks will be automated first?",
        headline="Which cognitive tasks will be automated first?",
    )
    await tmp_db.save_page(child)
    link = PageLink(
        from_page_id=question_page.id,
        to_page_id=child.id,
        link_type=LinkType.CHILD_QUESTION,
    )
    await tmp_db.save_link(link)
    return child


class PrioHarness:
    """Captures prio call invocations and simulates dispatch handlers.

    Tests set ``prio_queue`` to a list of RunCallResult values; each call to
    the mocked ``run_prioritization_call`` pops the next one (or returns an
    empty result when the queue is drained). Each simulated dispatch
    realistically consumes 1 unit of budget (respecting ``force``) and
    creates a call row so tests can inspect DB state.

    Attributes:
        prio_queue: RunCallResult values to return on successive prio calls.
        prio_calls: kwargs of each run_prioritization_call invocation.
        dispatched: dicts describing each dispatched call, in order.
    """

    def __init__(self, db: DB):
        self.db = db
        self.prio_queue: list[RunCallResult] = []
        self.prio_calls: list[dict] = []
        self.dispatched: list[dict] = []

    async def simulate_dispatch(
        self,
        call_type: CallType,
        question_id: str,
        workspace: Workspace = Workspace.RESEARCH,
        **kwargs,
    ) -> str | None:
        force = kwargs.get("force", False)
        ok = await self.db.consume_budget(1)
        if not ok:
            if force:
                await self.db.add_budget(1)
                ok = await self.db.consume_budget(1)
            if not ok:
                return None
        call = await self.db.create_call(
            call_type,
            scope_page_id=question_id,
            parent_call_id=kwargs.get("parent_call_id"),
            call_id=kwargs.get("call_id"),
            sequence_id=kwargs.get("sequence_id"),
            sequence_position=kwargs.get("sequence_position"),
            workspace=workspace,
        )
        call.status = CallStatus.COMPLETE
        await self.db.save_call(call)
        self.dispatched.append(
            {
                "question_id": question_id,
                "call_type": call_type.value,
                "sequence_id": call.sequence_id,
                "sequence_position": call.sequence_position,
                "call_id": call.id,
                "parent_call_id": kwargs.get("parent_call_id"),
                "force": force,
            }
        )
        return call.id


@pytest_asyncio.fixture
async def prio_harness(tmp_db, mocker):
    """Mock LLM-dependent plumbing so TwoPhaseOrchestrator can run without a real LLM.

    Mocks:
    - ``run_prioritization_call`` at its two_phase.py import site — returns
      scripted RunCallResult objects from ``harness.prio_queue``.
    - The 5 dispatch-handler helpers + ``_run_simple_call_dispatch`` to
      consume 1 unit of budget and create a call row per dispatch, honoring
      ``force``.
    - ``score_items_sequentially`` to return empty score lists (scoring is
      an independent LLM call path we don't test here).

    Returns the ``PrioHarness`` instance.
    """
    harness = PrioHarness(tmp_db)

    async def _fake_prio(task, context_text, call, db, **kwargs):
        harness.prio_calls.append({"task": task, "call": call, **kwargs})
        if harness.prio_queue:
            return harness.prio_queue.pop(0)
        return RunCallResult()

    mocker.patch(
        "rumil.orchestrators.two_phase.run_prioritization_call",
        side_effect=_fake_prio,
    )
    mocker.patch(
        "rumil.orchestrators.claim_investigation.run_prioritization_call",
        side_effect=_fake_prio,
    )

    async def _fake_fc(question_id, db, **kwargs):
        cid = await harness.simulate_dispatch(CallType.FIND_CONSIDERATIONS, question_id, **kwargs)
        return (0, [cid] if cid else [])

    async def _fake_assess(question_id, db, **kwargs):
        kwargs.pop("summarise", None)
        return await harness.simulate_dispatch(CallType.ASSESS, question_id, **kwargs)

    async def _fake_create_view(question_id, db, **kwargs):
        return await harness.simulate_dispatch(CallType.CREATE_VIEW, question_id, **kwargs)

    async def _fake_update_view(question_id, db, **kwargs):
        return await harness.simulate_dispatch(CallType.UPDATE_VIEW, question_id, **kwargs)

    async def _fake_web(question_id, db, **kwargs):
        return await harness.simulate_dispatch(CallType.WEB_RESEARCH, question_id, **kwargs)

    mocker.patch(
        "rumil.orchestrators.dispatch_handlers.find_considerations_until_done",
        side_effect=_fake_fc,
    )
    mocker.patch(
        "rumil.orchestrators.dispatch_handlers.assess_question",
        side_effect=_fake_assess,
    )
    mocker.patch(
        "rumil.orchestrators.claim_investigation.assess_question",
        side_effect=_fake_assess,
    )
    mocker.patch(
        "rumil.orchestrators.dispatch_handlers.create_view_for_question",
        side_effect=_fake_create_view,
    )
    mocker.patch(
        "rumil.orchestrators.dispatch_handlers.update_view_for_question",
        side_effect=_fake_update_view,
    )
    mocker.patch(
        "rumil.orchestrators.dispatch_handlers.web_research_question",
        side_effect=_fake_web,
    )
    mocker.patch(
        "rumil.orchestrators.two_phase.update_view_for_question",
        side_effect=_fake_update_view,
    )
    mocker.patch(
        "rumil.orchestrators.two_phase.create_view_for_question",
        side_effect=_fake_create_view,
    )
    mocker.patch(
        "rumil.orchestrators.two_phase.score_items_sequentially",
        return_value=[],
    )
    mocker.patch(
        "rumil.orchestrators.claim_investigation.score_items_sequentially",
        return_value=[],
    )

    async def _fake_simple(self, question_id, call_type, cls, parent_call_id, **kwargs):
        kwargs.setdefault("parent_call_id", parent_call_id)
        kwargs["parent_call_id"] = parent_call_id
        return await harness.simulate_dispatch(call_type, question_id, **kwargs)

    mocker.patch.object(DispatchRunner, "_run_simple_call_dispatch", _fake_simple)

    return harness
