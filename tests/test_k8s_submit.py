"""Pure-function tests for rumil.k8s.submit.

We don't talk to a real cluster — we exercise the command-builder, name
slugger, and manifest-renderer with mocked kubernetes client returns.
"""

from __future__ import annotations

import re

import pytest
from kubernetes import client
from pydantic import ValidationError

from rumil.k8s.submit import (
    JOB_ANNOTATION_QUESTION,
    JOB_ANNOTATION_WORKSPACE_NAME,
    JOB_LABEL_OWNER,
    JOB_LABEL_RUN_ID,
    JOB_LABEL_RUN_KIND,
    JOB_LABEL_RUN_KIND_VALUE,
    JOB_LABEL_WORKSPACE,
    NAMESPACE,
    QUESTION_ANNOTATION_MAX_CHARS,
    _build_container_command,
    _build_job,
    _job_name,
    _override_image_tag,
    _read_api_runtime,
    _slug,
    build_logs_url,
)
from rumil.k8s.types import OrchestratorRunRequest
from rumil.settings import override_settings

_RUN_ID = "00000000-0000-0000-0000-000000000001"


def _spec(**overrides) -> OrchestratorRunRequest:
    payload: dict = {"question": "is the sky blue?", "budget": 1, "workspace": "wp"}
    payload.update(overrides)
    return OrchestratorRunRequest(**payload)


def test_container_command_starts_with_python_main_and_question():
    spec = _spec(question="why?", budget=3, workspace="myws")
    cmd = _build_container_command(spec, run_id=_RUN_ID)
    assert cmd[:3] == ["python", "main.py", "why?"]


def test_container_command_forwards_budget_and_workspace():
    spec = _spec(budget=7, workspace="ws")
    cmd = _build_container_command(spec, run_id=_RUN_ID)
    assert "--budget" in cmd and cmd[cmd.index("--budget") + 1] == "7"
    assert "--workspace" in cmd and cmd[cmd.index("--workspace") + 1] == "ws"


def test_container_command_forwards_run_id():
    cmd = _build_container_command(_spec(), run_id=_RUN_ID)
    assert "--run-id" in cmd and cmd[cmd.index("--run-id") + 1] == _RUN_ID


def test_container_command_pins_db_prod_and_executor_local():
    """The in-pod CLI must NOT recurse: --db prod --executor local always."""
    cmd = _build_container_command(_spec(), run_id=_RUN_ID)
    assert cmd[cmd.index("--db") + 1] == "prod"
    assert cmd[cmd.index("--executor") + 1] == "local"


def test_container_command_does_not_forward_prod_shorthand():
    cmd = _build_container_command(_spec(), run_id=_RUN_ID)
    assert "--prod" not in cmd


def test_container_command_forwards_smoke_test_flag():
    cmd = _build_container_command(_spec(smoke_test=True), run_id=_RUN_ID)
    assert "--smoke-test" in cmd


def test_container_command_forwards_auto_summary_as_bare_summary_flag():
    cmd = list(_build_container_command(_spec(auto_summary=True), run_id=_RUN_ID))
    assert "--summary" in cmd
    # The flag must NOT be followed by a value; main.py's argparse uses
    # nargs="?" with const="__auto__", which only triggers when --summary
    # is bare.
    idx = cmd.index("--summary")
    assert idx == len(cmd) - 1 or cmd[idx + 1].startswith("--")


def test_container_command_forwards_auto_self_improve_as_bare_flag():
    cmd = list(_build_container_command(_spec(auto_self_improve=True), run_id=_RUN_ID))
    assert "--self-improve" in cmd
    idx = cmd.index("--self-improve")
    assert idx == len(cmd) - 1 or cmd[idx + 1].startswith("--")


def test_container_command_omits_auto_flags_by_default():
    cmd = _build_container_command(_spec(), run_id=_RUN_ID)
    assert "--summary" not in cmd
    assert "--self-improve" not in cmd


def test_container_command_omits_unset_optional_flags():
    cmd = _build_container_command(_spec(), run_id=_RUN_ID)
    assert "--smoke-test" not in cmd
    assert "--available-moves" not in cmd
    assert "--name" not in cmd


def test_container_command_forwards_value_flags():
    spec = _spec(available_moves="extra", run_name="my-run", ingest_num_claims=8)
    cmd = _build_container_command(spec, run_id=_RUN_ID)
    assert cmd[cmd.index("--available-moves") + 1] == "extra"
    assert cmd[cmd.index("--name") + 1] == "my-run"
    assert cmd[cmd.index("--ingest-num-claims") + 1] == "8"


_DNS1123_LABEL = re.compile(r"^[a-z0-9]([-a-z0-9]*[a-z0-9])?$")


def test_job_name_is_dns_1123_compliant_for_uppercase_workspace():
    name = _job_name(_spec(workspace="MyResearch_Project"))
    assert _DNS1123_LABEL.match(name), name
    assert len(name) <= 63


def test_job_name_handles_very_long_workspace():
    name = _job_name(_spec(workspace="x" * 200))
    assert len(name) <= 63
    assert _DNS1123_LABEL.match(name)


def test_job_name_handles_special_chars():
    name = _job_name(_spec(workspace="weird/!@#name with spaces"))
    assert _DNS1123_LABEL.match(name), name


def test_slug_falls_back_when_input_has_no_valid_chars():
    assert _slug("!!!") == "ws"


def test_slug_lowercases_and_collapses():
    assert _slug("My Workspace 1") == "my-workspace-1"


def _fake_deployment() -> client.V1Deployment:
    container = client.V1Container(
        name="rumil-api",
        image="us-central1-docker.pkg.dev/proj/rumil/rumil-api:abc123",
        env=[
            client.V1EnvVar(name="USE_PROD_DB", value="1"),
            client.V1EnvVar(name="SUPABASE_PROD_URL", value="https://x.supabase.co"),
        ],
        env_from=[
            client.V1EnvFromSource(secret_ref=client.V1SecretEnvSource(name="rumil-api-secrets"))
        ],
    )
    pod = client.V1PodSpec(containers=[container])
    template = client.V1PodTemplateSpec(spec=pod)
    return client.V1Deployment(spec=client.V1DeploymentSpec(selector={}, template=template))


def test_read_api_runtime_extracts_image_and_env(mocker):
    apps = mocker.MagicMock(spec=client.AppsV1Api)
    apps.read_namespaced_deployment.return_value = _fake_deployment()
    image, env, env_from = _read_api_runtime(apps)
    assert image.endswith(":abc123")
    assert {e.name for e in env} == {"USE_PROD_DB", "SUPABASE_PROD_URL"}
    secret_ref = env_from[0].secret_ref
    assert secret_ref is not None and secret_ref.name == "rumil-api-secrets"


def test_build_job_stamps_dynamic_fields():
    spec = _spec(workspace="ws", question="q", budget=2)
    body = _build_job(
        spec,
        name="rumil-orch-ws-aaaa",
        owner_user_id="user-123",
        run_id=_RUN_ID,
        image="image:tag",
        env=[client.V1EnvVar(name="A", value="1")],
        env_from=[
            client.V1EnvFromSource(secret_ref=client.V1SecretEnvSource(name="rumil-api-secrets"))
        ],
    )
    assert body["metadata"]["name"] == "rumil-orch-ws-aaaa"
    assert body["metadata"]["namespace"] == NAMESPACE
    assert body["metadata"]["labels"][JOB_LABEL_RUN_KIND] == JOB_LABEL_RUN_KIND_VALUE
    assert body["metadata"]["labels"][JOB_LABEL_OWNER] == "user-123"
    assert body["metadata"]["labels"][JOB_LABEL_RUN_ID] == _RUN_ID
    assert body["metadata"]["labels"][JOB_LABEL_WORKSPACE] == "ws"
    assert body["metadata"]["annotations"][JOB_ANNOTATION_WORKSPACE_NAME] == "ws"
    assert body["metadata"]["annotations"][JOB_ANNOTATION_QUESTION] == "q"

    container = body["spec"]["template"]["spec"]["containers"][0]
    assert container["image"] == "image:tag"
    assert container["command"][:3] == ["python", "main.py", "q"]
    assert "--run-id" in container["command"]
    assert {e["name"] for e in container["env"]} == {"A"}
    assert container["envFrom"][0]["secretRef"]["name"] == "rumil-api-secrets"


def test_build_job_preserves_unslugged_workspace_in_annotation():
    """Workspace label is DNS-1123 slugged, but annotation keeps the original."""
    spec = _spec(workspace="My Research Project")
    body = _build_job(
        spec,
        name="x",
        owner_user_id="",
        run_id=_RUN_ID,
        image="img",
        env=[],
        env_from=[],
    )
    assert body["metadata"]["labels"][JOB_LABEL_WORKSPACE] == "my-research-project"
    assert body["metadata"]["annotations"][JOB_ANNOTATION_WORKSPACE_NAME] == "My Research Project"


def test_build_job_truncates_long_question_annotation():
    spec = _spec(question="q" * 500)
    body = _build_job(
        spec,
        name="x",
        owner_user_id="",
        run_id=_RUN_ID,
        image="img",
        env=[],
        env_from=[],
    )
    assert (
        len(body["metadata"]["annotations"][JOB_ANNOTATION_QUESTION])
        == QUESTION_ANNOTATION_MAX_CHARS
    )


def test_build_job_preserves_static_lifecycle_settings():
    body = _build_job(
        _spec(),
        name="x",
        owner_user_id="",
        run_id=_RUN_ID,
        image="img",
        env=[],
        env_from=[],
    )
    assert body["spec"]["backoffLimit"] == 0
    assert body["spec"]["template"]["spec"]["restartPolicy"] == "Never"
    assert body["spec"]["template"]["spec"]["serviceAccountName"] == "rumil-orchestrator-job"


def test_build_job_omits_owner_label_when_anonymous():
    body = _build_job(
        _spec(), name="x", owner_user_id="", run_id=_RUN_ID, image="img", env=[], env_from=[]
    )
    assert JOB_LABEL_OWNER not in (body["metadata"]["labels"] or {})


def test_request_validation_rejects_empty_question():
    with pytest.raises(ValidationError):
        OrchestratorRunRequest(question="", budget=1, workspace="ws")


def test_request_validation_rejects_zero_budget():
    with pytest.raises(ValidationError):
        OrchestratorRunRequest(question="q", budget=0, workspace="ws")


def test_request_validation_rejects_neither_question_nor_continue_id():
    with pytest.raises(ValidationError, match="exactly one of"):
        OrchestratorRunRequest(budget=1, workspace="ws")


def test_request_validation_rejects_both_question_and_continue_id():
    with pytest.raises(ValidationError, match="exactly one of"):
        OrchestratorRunRequest(question="q", continue_id="qid", budget=1, workspace="ws")


def test_request_accepts_continue_id_alone():
    spec = OrchestratorRunRequest(continue_id="qid", budget=1, workspace="ws")
    assert spec.continue_id == "qid"
    assert spec.question is None


def test_container_command_uses_continue_flag_when_continuing():
    spec = OrchestratorRunRequest(continue_id="qid-xyz", budget=3, workspace="ws")
    cmd = list(_build_container_command(spec, run_id=_RUN_ID))
    assert cmd[:2] == ["python", "main.py"]
    assert "--continue" in cmd and cmd[cmd.index("--continue") + 1] == "qid-xyz"
    # The positional question slot must be empty so argparse doesn't think
    # the run_id (or any later flag) is a new question.
    assert "qid-xyz" not in cmd[: cmd.index("--continue")]


def test_build_job_default_annotation_for_continue_run():
    spec = OrchestratorRunRequest(continue_id="qid-abcdef", budget=1, workspace="ws")
    body = _build_job(
        spec,
        name="x",
        owner_user_id="",
        run_id=_RUN_ID,
        image="img",
        env=[],
        env_from=[],
    )
    annotation = body["metadata"]["annotations"][JOB_ANNOTATION_QUESTION]
    assert "qid-abcdef" in annotation
    assert "continue" in annotation.lower()


def test_build_job_uses_question_annotation_override():
    """Caller can supply a resolved headline (e.g. from a DB lookup) to
    override the default `(continue) <id>` placeholder."""
    spec = OrchestratorRunRequest(continue_id="qid", budget=1, workspace="ws")
    body = _build_job(
        spec,
        name="x",
        owner_user_id="",
        run_id=_RUN_ID,
        image="img",
        env=[],
        env_from=[],
        question_annotation="Why is the sky blue?",
    )
    assert body["metadata"]["annotations"][JOB_ANNOTATION_QUESTION] == "Why is the sky blue?"


@pytest.mark.parametrize(
    "image,expected",
    [
        (
            "us-central1-docker.pkg.dev/p/rumil/rumil-api:abc123",
            "us-central1-docker.pkg.dev/p/rumil/rumil-api:NEW",
        ),
        ("ghcr.io/foo/bar:latest", "ghcr.io/foo/bar:NEW"),
        ("ghcr.io/foo/bar", "ghcr.io/foo/bar:NEW"),
        ("repo/name@sha256:deadbeefdeadbeef", "repo/name:NEW"),
        ("localhost:5000/foo/bar:old", "localhost:5000/foo/bar:NEW"),
        ("localhost:5000/foo/bar", "localhost:5000/foo/bar:NEW"),
    ],
)
def test_override_image_tag(image, expected):
    assert _override_image_tag(image, "NEW") == expected


def test_build_job_uses_container_tag_when_set(mocker):
    """Submitter overrides the live deployment's tag when spec.container_tag is set."""
    from rumil.k8s import submit as submit_mod

    fake_batch = mocker.MagicMock()
    fake_apps = mocker.MagicMock()
    fake_apps.read_namespaced_deployment.return_value = _fake_deployment()
    mocker.patch.object(submit_mod, "_kube_clients", return_value=(fake_batch, fake_apps))

    spec = _spec(container_tag="exp-deadbeef")
    submit_mod.submit_orchestrator_job(spec, owner_user_id="u")

    body = fake_batch.create_namespaced_job.call_args.kwargs["body"]
    image = body["spec"]["template"]["spec"]["containers"][0]["image"]
    assert image.endswith(":exp-deadbeef")
    # Repository unchanged from the live deployment.
    assert image.startswith("us-central1-docker.pkg.dev/proj/rumil/rumil-api:")


def test_build_job_uses_live_tag_when_container_tag_unset(mocker):
    from rumil.k8s import submit as submit_mod

    fake_batch = mocker.MagicMock()
    fake_apps = mocker.MagicMock()
    fake_apps.read_namespaced_deployment.return_value = _fake_deployment()
    mocker.patch.object(submit_mod, "_kube_clients", return_value=(fake_batch, fake_apps))

    submit_mod.submit_orchestrator_job(_spec(), owner_user_id="u")
    body = fake_batch.create_namespaced_job.call_args.kwargs["body"]
    image = body["spec"]["template"]["spec"]["containers"][0]["image"]
    assert image.endswith(":abc123")


@pytest.mark.parametrize(
    "bad_tag",
    ["", "../etc/passwd", "tag with space", "tag:colon", "tag/slash", "-leading-dash"],
)
def test_request_rejects_unsafe_container_tags(bad_tag):
    with pytest.raises(ValidationError):
        OrchestratorRunRequest(question="q", budget=1, workspace="ws", container_tag=bad_tag)


@pytest.mark.parametrize("good_tag", ["abc123", "exp-20260425-deadbe", "v1.2.3", "tag_under"])
def test_request_accepts_normal_container_tags(good_tag):
    spec = OrchestratorRunRequest(question="q", budget=1, workspace="ws", container_tag=good_tag)
    assert spec.container_tag == good_tag


def test_build_job_appends_extra_env_entries():
    spec = _spec(extra_env={"AVAILABLE_MOVES": "extra"})
    body = _build_job(
        spec,
        name="x",
        owner_user_id="",
        run_id=_RUN_ID,
        image="img",
        env=[client.V1EnvVar(name="USE_PROD_DB", value="1")],
        env_from=[],
    )
    container = body["spec"]["template"]["spec"]["containers"][0]
    names = [e["name"] for e in container["env"]]
    assert "AVAILABLE_MOVES" in names
    by_name = {e["name"]: e["value"] for e in container["env"]}
    assert by_name["AVAILABLE_MOVES"] == "extra"


def test_build_job_drops_inherited_entries_shadowed_by_extra_env():
    """If extra_env names a key already in inherited env, the inherited
    one is dropped — never two entries with the same name."""
    spec = _spec(extra_env={"USE_PROD_DB": "0"})
    body = _build_job(
        spec,
        name="x",
        owner_user_id="",
        run_id=_RUN_ID,
        image="img",
        env=[
            client.V1EnvVar(name="USE_PROD_DB", value="1"),
            client.V1EnvVar(name="OTHER", value="keep"),
        ],
        env_from=[],
    )
    container = body["spec"]["template"]["spec"]["containers"][0]
    names = [e["name"] for e in container["env"]]
    assert names.count("USE_PROD_DB") == 1
    by_name = {e["name"]: e["value"] for e in container["env"]}
    assert by_name["USE_PROD_DB"] == "0"  # the override won
    assert by_name["OTHER"] == "keep"  # unrelated inherited entry preserved


def test_build_job_extra_env_appends_after_inherited():
    """Inherited env first, overrides last — order reflects intent for
    anyone reading the manifest later."""
    spec = _spec(extra_env={"AVAILABLE_MOVES": "extra"})
    body = _build_job(
        spec,
        name="x",
        owner_user_id="",
        run_id=_RUN_ID,
        image="img",
        env=[
            client.V1EnvVar(name="USE_PROD_DB", value="1"),
            client.V1EnvVar(name="OTHER", value="keep"),
        ],
        env_from=[],
    )
    names = [e["name"] for e in body["spec"]["template"]["spec"]["containers"][0]["env"]]
    assert names == ["USE_PROD_DB", "OTHER", "AVAILABLE_MOVES"]


def test_build_job_no_extra_env_preserves_inherited_order():
    body = _build_job(
        _spec(),
        name="x",
        owner_user_id="",
        run_id=_RUN_ID,
        image="img",
        env=[
            client.V1EnvVar(name="A", value="1"),
            client.V1EnvVar(name="B", value="2"),
        ],
        env_from=[],
    )
    names = [e["name"] for e in body["spec"]["template"]["spec"]["containers"][0]["env"]]
    assert names == ["A", "B"]


def test_build_logs_url_returns_empty_when_project_unset():
    with override_settings(gcp_project_id=""):
        assert build_logs_url("rumil-orch-ws-aaaa") == ""


def test_build_logs_url_includes_namespace_job_and_cluster():
    import urllib.parse

    with override_settings(gcp_project_id="my-proj", gcp_cluster_name="my-cluster"):
        url = build_logs_url("rumil-orch-ws-aaaa")
    assert url.startswith("https://console.cloud.google.com/logs/query;query=")
    assert url.endswith("?project=my-proj")
    decoded = urllib.parse.unquote(url)
    assert f'resource.labels.namespace_name="{NAMESPACE}"' in decoded
    assert 'labels."k8s-pod/job-name"="rumil-orch-ws-aaaa"' in decoded
    assert 'resource.labels.cluster_name="my-cluster"' in decoded
