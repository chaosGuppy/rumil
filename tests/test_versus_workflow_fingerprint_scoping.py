"""Tests for per-Workflow ``code_fingerprint`` scoping (issue #425).

Pre-#425 the fingerprint was a single fat hash covering every
orchestrator + every call + every prompt. Editing
``orchestrators/experimental.py`` would fork TwoPhase row hashes —
spurious churn. Post-#425 each Workflow declares its own
``code_paths``; the fingerprint splits into a cross-cutting harness
hash + a per-workflow hash, and unrelated orchestrator edits no
longer cross-contaminate.

These tests pin the acceptance criteria from #425:

- ``experimental.py`` is NOT in ``TwoPhaseWorkflow.code_paths`` →
  edits to it can't fork TwoPhase row hashes via the workflow scope.
- ``two_phase.py`` IS in ``TwoPhaseWorkflow.code_paths`` → edits to
  it do fork TwoPhase row hashes.
- The cross-cutting (shared) fingerprint excludes the orchestrators
  directory entirely — workflow-specific orchestrator code never
  shows up as a shared-axis change.
"""

from __future__ import annotations

import sys
from pathlib import Path

_VERSUS_SRC = Path(__file__).resolve().parents[1] / "versus" / "src"
if str(_VERSUS_SRC) not in sys.path:
    sys.path.insert(0, str(_VERSUS_SRC))

from versus.versions import (  # noqa: E402
    SHARED_CODE_FINGERPRINT_DIRS,
    SHARED_CODE_FINGERPRINT_FILES,
)
from versus.versus_config import (  # noqa: E402
    compute_shared_code_fingerprint,
    compute_workflow_code_fingerprint,
)

from rumil.versus_workflow import TwoPhaseWorkflow  # noqa: E402


def test_two_phase_code_paths_includes_two_phase_module():
    """``two_phase.py`` is in TwoPhaseWorkflow.code_paths so edits to
    it fork TwoPhase row hashes through the workflow fingerprint.
    """
    assert "src/rumil/orchestrators/two_phase.py" in TwoPhaseWorkflow.code_paths


def test_two_phase_code_paths_excludes_experimental_module():
    """``experimental.py`` is a sibling orchestrator unrelated to
    TwoPhase; it must NOT be in TwoPhaseWorkflow.code_paths so editing
    it doesn't fork TwoPhase row hashes.
    """
    assert "src/rumil/orchestrators/experimental.py" not in TwoPhaseWorkflow.code_paths


def test_shared_fingerprint_excludes_orchestrators_directory():
    """Cross-cutting (shared) fingerprint is the harness layer only.
    Orchestrator code is per-workflow — including ``orchestrators/``
    in the shared dirs would fork *every* workflow's hash on an
    unrelated experimental edit, defeating the point of the split.
    """
    shared_dirs = {rel for rel, _pattern in SHARED_CODE_FINGERPRINT_DIRS}
    assert "src/rumil/orchestrators/" not in shared_dirs


def test_shared_fingerprint_excludes_calls_directory():
    """Calls are dispatched per-workflow; same reasoning as
    orchestrators. TwoPhaseWorkflow.code_paths declares ``calls/``
    explicitly so per-call edits fork TwoPhase rows but not other
    workflows' rows.
    """
    shared_dirs = {rel for rel, _pattern in SHARED_CODE_FINGERPRINT_DIRS}
    assert "src/rumil/calls/" not in shared_dirs


def test_shared_fingerprint_includes_harness_files():
    """Versus harness layer — runner, closer, workflow protocol,
    bridge — must show up on the shared fingerprint so any harness
    edit forks every workflow's hash.
    """
    expected = {
        "src/rumil/versus_runner.py",
        "src/rumil/versus_closer.py",
        "src/rumil/versus_workflow.py",
        "src/rumil/versus_bridge.py",
        "src/rumil/prompts/preamble.md",
    }
    assert expected.issubset(set(SHARED_CODE_FINGERPRINT_FILES))


def test_compute_workflow_code_fingerprint_covers_two_phase_paths():
    """Smoke-test: compute_workflow_code_fingerprint produces an entry
    for every path in code_paths and every entry is non-empty (the
    paths actually exist on disk).
    """
    wf = TwoPhaseWorkflow(budget=4)
    fp = compute_workflow_code_fingerprint(wf)
    assert set(fp.keys()) == set(wf.code_paths)
    for path, sha in fp.items():
        assert sha, f"missing-path sentinel for {path!r}; expected real content"


def test_compute_workflow_code_fingerprint_records_missing_paths_as_empty(tmp_path, monkeypatch):
    """Missing path → empty string (matches compute_file_fingerprint
    semantics; absence shows up in diffs rather than crashing or
    silently dropping).
    """
    from versus import versus_config as jc

    monkeypatch.setattr(jc, "_repo_root", lambda: tmp_path)

    class _FakeWorkflow:
        code_paths = ("nope/missing.py", "also/missing/")

    fp = compute_workflow_code_fingerprint(_FakeWorkflow())
    assert fp == {"nope/missing.py": "", "also/missing/": ""}


def test_compute_workflow_code_fingerprint_picks_up_file_content_changes(tmp_path, monkeypatch):
    """Editing a file in code_paths flips its sha — the per-workflow
    hash is content-sensitive.
    """
    from versus import versus_config as jc

    monkeypatch.setattr(jc, "_repo_root", lambda: tmp_path)
    target = tmp_path / "code.py"
    target.write_text("alpha")

    class _FakeWorkflow:
        code_paths = ("code.py",)

    fp_a = compute_workflow_code_fingerprint(_FakeWorkflow())
    target.write_text("beta")
    fp_b = compute_workflow_code_fingerprint(_FakeWorkflow())
    assert fp_a["code.py"] != fp_b["code.py"]


def test_compute_workflow_code_fingerprint_picks_up_dir_content_changes(tmp_path, monkeypatch):
    """Editing a file *inside* a directory listed in code_paths flips
    the directory's folded sha. Pins the recursive-glob walk used for
    directory entries.
    """
    from versus import versus_config as jc

    monkeypatch.setattr(jc, "_repo_root", lambda: tmp_path)
    pkg = tmp_path / "pkg"
    pkg.mkdir()
    (pkg / "a.py").write_text("alpha")
    (pkg / "b.py").write_text("two")

    class _FakeWorkflow:
        code_paths = ("pkg",)

    fp_a = compute_workflow_code_fingerprint(_FakeWorkflow())
    (pkg / "a.py").write_text("alpha2")
    fp_b = compute_workflow_code_fingerprint(_FakeWorkflow())
    assert fp_a["pkg"] != fp_b["pkg"]


def test_compute_workflow_code_fingerprint_picks_up_nested_file_changes(tmp_path, monkeypatch):
    """Recursive glob: nested files (e.g. subpackage modules) are
    folded into the directory hash too.
    """
    from versus import versus_config as jc

    monkeypatch.setattr(jc, "_repo_root", lambda: tmp_path)
    pkg = tmp_path / "pkg"
    sub = pkg / "sub"
    sub.mkdir(parents=True)
    (sub / "deep.py").write_text("alpha")

    class _FakeWorkflow:
        code_paths = ("pkg",)

    fp_a = compute_workflow_code_fingerprint(_FakeWorkflow())
    (sub / "deep.py").write_text("beta")
    fp_b = compute_workflow_code_fingerprint(_FakeWorkflow())
    assert fp_a["pkg"] != fp_b["pkg"]


def test_shared_fingerprint_invariant_under_orchestrator_edits(tmp_path, monkeypatch):
    """The acceptance criterion: editing an orchestrator file does
    NOT change the shared fingerprint. Simulated by patching the
    repo root at a tmp_path that mirrors the relevant directories
    and confirming an orchestrator-file write is invisible.
    """
    from versus import versus_config as jc

    # Build a minimal mirror layout: just enough that
    # SHARED_CODE_FINGERPRINT_DIRS / FILES find content.
    for rel_dir, _pattern in SHARED_CODE_FINGERPRINT_DIRS:
        d = tmp_path / rel_dir
        d.mkdir(parents=True, exist_ok=True)
        # Drop one matching file so the dir sha is non-empty.
        (d / "stub.py").write_text("ok\n")
    for rel in SHARED_CODE_FINGERPRINT_FILES:
        f = tmp_path / rel
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text("shared\n")

    monkeypatch.setattr(jc, "_repo_root", lambda: tmp_path)

    fp_before = compute_shared_code_fingerprint()

    # Simulate an orchestrator-file edit. Orchestrators dir is NOT in
    # SHARED_CODE_FINGERPRINT_DIRS (post-#425), so this edit shouldn't
    # affect the shared hash.
    orch_dir = tmp_path / "src" / "rumil" / "orchestrators"
    orch_dir.mkdir(parents=True, exist_ok=True)
    (orch_dir / "experimental.py").write_text("v1\n")
    fp_after_v1 = compute_shared_code_fingerprint()
    (orch_dir / "experimental.py").write_text("v2\n")
    fp_after_v2 = compute_shared_code_fingerprint()

    assert fp_before == fp_after_v1 == fp_after_v2
