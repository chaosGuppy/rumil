"""Server-side Kubernetes Job submission for orchestrator runs.

Lives inside the API container and is invoked by `rumil.api.jobs`.
Uses the in-cluster ServiceAccount (`rumil-api`) for k8s API access; the
RBAC needed (jobs and deployments) lives in
`deploy/chart/templates/api-rbac.yaml`.

Design:
- Image and runtime env are read live from the running `rumil-api`
  Deployment, so orchestrator pods always run the same code as the
  currently-deployed API.
- The orchestrator pod is launched with a separate, RBAC-less SA
  (`rumil-orchestrator-job`) to limit blast radius if the orchestrator
  container is ever exploited.
- Pod logs are surfaced via a pre-built Cloud Logging URL returned in
  the API response, not streamed back over HTTP. This avoids
  long-lived chunked responses being killed by the GKE Gateway timeout.
"""

import logging
import re
import secrets
import threading
import urllib.parse
import uuid
from collections.abc import Sequence
from importlib import resources
from typing import Any, cast

import yaml
from kubernetes import client, config

from rumil.k8s.types import OrchestratorRunRequest
from rumil.settings import get_settings

NAMESPACE = "rumil"
API_DEPLOYMENT_NAME = "rumil-api"
JOB_LABEL_RUN_KIND = "rumil.ink/run-kind"
JOB_LABEL_OWNER = "rumil.ink/owner-user-id"
JOB_LABEL_RUN_ID = "rumil.ink/run-id"
JOB_LABEL_WORKSPACE = "rumil.ink/workspace"
JOB_LABEL_RUN_KIND_VALUE = "orchestrator"
JOB_ANNOTATION_WORKSPACE_NAME = "rumil.ink/workspace-name"
JOB_ANNOTATION_QUESTION = "rumil.ink/question"
QUESTION_ANNOTATION_MAX_CHARS = 200

log = logging.getLogger(__name__)

_kube_loaded = False
_kube_loaded_lock = threading.Lock()


def _load_kube_config() -> None:
    global _kube_loaded
    with _kube_loaded_lock:
        if _kube_loaded:
            return
        try:
            config.load_incluster_config()
            log.info("loaded in-cluster kube config")
        except config.ConfigException:
            config.load_kube_config()
            log.info("loaded out-of-cluster kube config (fallback)")
        _kube_loaded = True


def _kube_clients() -> tuple[client.BatchV1Api, client.AppsV1Api]:
    _load_kube_config()
    return client.BatchV1Api(), client.AppsV1Api()


def _load_manifest() -> dict[str, Any]:
    text = (
        resources.files("rumil.k8s").joinpath("orchestrator_job.yaml").read_text(encoding="utf-8")
    )
    parsed = yaml.safe_load(text)
    if not isinstance(parsed, dict):
        raise RuntimeError("orchestrator_job.yaml did not parse to a mapping")
    return parsed


def _read_api_runtime(
    apps_v1: client.AppsV1Api,
) -> tuple[str, Sequence[client.V1EnvVar], Sequence[client.V1EnvFromSource]]:
    """Return (image, env, env_from) from the running rumil-api Deployment."""
    dep = cast(
        client.V1Deployment,
        apps_v1.read_namespaced_deployment(name=API_DEPLOYMENT_NAME, namespace=NAMESPACE),
    )
    if dep.spec is None or dep.spec.template.spec is None:
        raise RuntimeError(f"{API_DEPLOYMENT_NAME} Deployment has no pod spec")
    containers = dep.spec.template.spec.containers
    if not containers:
        raise RuntimeError(f"{API_DEPLOYMENT_NAME} Deployment has no containers")
    api_container = containers[0]
    image = api_container.image
    if not image:
        raise RuntimeError(f"{API_DEPLOYMENT_NAME} container has no image")
    env = list(api_container.env or [])
    env_from = list(api_container.env_from or [])
    return image, env, env_from


def _override_image_tag(image: str, tag: str) -> str:
    """Replace the tag portion of a docker image ref, preserving registry/repo.

    Examples:
        repo/name:abc            -> repo/name:<tag>
        repo/name                -> repo/name:<tag>
        repo/name@sha256:...     -> repo/name:<tag>
        host:5000/repo/name:abc  -> host:5000/repo/name:<tag>
    """
    base = image.split("@", 1)[0]
    repo, sep, after = base.rpartition(":")
    if sep and "/" not in after:
        return f"{repo}:{tag}"
    return f"{base}:{tag}"


_DNS1123_BAD = re.compile(r"[^a-z0-9-]+")


def _slug(value: str) -> str:
    s = _DNS1123_BAD.sub("-", value.lower()).strip("-")
    return s or "ws"


def _job_name(spec: OrchestratorRunRequest) -> str:
    workspace_slug = _slug(spec.workspace)[:20].rstrip("-")
    suffix = secrets.token_hex(4)
    name = f"rumil-orch-{workspace_slug}-{suffix}".strip("-")
    return name[:63]


def _build_container_command(spec: OrchestratorRunRequest, *, run_id: str) -> Sequence[str]:
    """Translate the request into the in-pod CLI invocation."""
    args: list[str] = ["python", "main.py"]
    if spec.continue_id:
        args += ["--continue", spec.continue_id]
    else:
        # Validated by OrchestratorRunRequest: exactly one of question/continue_id is set.
        assert spec.question is not None
        args.append(spec.question)
    args += [
        "--budget",
        str(spec.budget),
        "--workspace",
        spec.workspace,
        "--db",
        "prod",
        "--executor",
        "local",
        "--run-id",
        run_id,
    ]
    if spec.smoke_test:
        args.append("--smoke-test")
    if spec.quiet:
        args.append("--quiet")
    if spec.debug:
        args.append("--debug")
    if spec.force_twophase_recurse:
        args.append("--force-twophase-recurse")
    if spec.no_trace:
        args.append("--no-trace")
    if spec.auto_summary:
        args.append("--summary")
    if spec.auto_self_improve:
        args.append("--self-improve")
    if spec.improvement_instructions and spec.auto_self_improve:
        args += ["--improvement-instructions", spec.improvement_instructions]
    if spec.available_moves:
        args += ["--available-moves", spec.available_moves]
    if spec.available_calls:
        args += ["--available-calls", spec.available_calls]
    if spec.view_variant:
        args += ["--view-variant", spec.view_variant]
    if spec.ingest_num_claims is not None:
        args += ["--ingest-num-claims", str(spec.ingest_num_claims)]
    if spec.run_name:
        args += ["--name", spec.run_name]
    return args


def _question_annotation(spec: OrchestratorRunRequest) -> str:
    """Display string stored on the Job for the /jobs UI.

    For new investigations, the user-typed question is the natural label.
    For continue runs, we don't have the headline here (would require a DB
    lookup), so a callable annotation overrider can supply a richer string;
    by default we show the continue_id so the row is at least clickable.
    """
    if spec.question:
        return spec.question
    assert spec.continue_id is not None
    return f"(continue) {spec.continue_id}"


def _build_job(
    spec: OrchestratorRunRequest,
    *,
    name: str,
    owner_user_id: str,
    run_id: str,
    image: str,
    env: Sequence[client.V1EnvVar],
    env_from: Sequence[client.V1EnvFromSource],
    question_annotation: str | None = None,
) -> dict[str, Any]:
    manifest = _load_manifest()
    manifest.setdefault("metadata", {})
    metadata: dict[str, Any] = manifest["metadata"]
    metadata["name"] = name
    metadata["namespace"] = NAMESPACE
    labels = dict(metadata.get("labels") or {})
    labels[JOB_LABEL_RUN_KIND] = JOB_LABEL_RUN_KIND_VALUE
    labels[JOB_LABEL_RUN_ID] = run_id
    workspace_slug = _slug(spec.workspace)[:63].rstrip("-") or "ws"
    labels[JOB_LABEL_WORKSPACE] = workspace_slug
    if owner_user_id:
        labels[JOB_LABEL_OWNER] = owner_user_id
    metadata["labels"] = labels

    annotations = dict(metadata.get("annotations") or {})
    annotations[JOB_ANNOTATION_WORKSPACE_NAME] = spec.workspace
    annotation_label = question_annotation or _question_annotation(spec)
    annotations[JOB_ANNOTATION_QUESTION] = annotation_label[:QUESTION_ANNOTATION_MAX_CHARS]
    metadata["annotations"] = annotations

    spec_block: dict[str, Any] = manifest["spec"]
    pod_spec: dict[str, Any] = spec_block["template"]["spec"]
    containers: list[dict[str, Any]] = pod_spec["containers"]
    container = containers[0]
    container["image"] = image
    container["command"] = _build_container_command(spec, run_id=run_id)
    overrides = dict(spec.extra_env)
    inherited = [_env_var_to_dict(e) for e in env if e.name not in overrides]
    container["env"] = inherited + [{"name": k, "value": v} for k, v in overrides.items()]
    container["envFrom"] = [_env_from_to_dict(e) for e in env_from]
    return manifest


def _env_var_to_dict(e: client.V1EnvVar) -> dict[str, Any]:
    out: dict[str, Any] = {"name": e.name}
    if e.value is not None:
        out["value"] = e.value
    if e.value_from is not None:
        out["valueFrom"] = client.ApiClient().sanitize_for_serialization(e.value_from)
    return out


def _env_from_to_dict(e: client.V1EnvFromSource) -> dict[str, Any]:
    return cast(dict[str, Any], client.ApiClient().sanitize_for_serialization(e))


def submit_orchestrator_job(
    spec: OrchestratorRunRequest,
    *,
    owner_user_id: str,
    question_annotation: str | None = None,
) -> tuple[str, str]:
    """Create the Job and return (job_name, run_id). Raises on k8s API errors.

    The run_id is generated here and threaded into both the Job's
    `rumil.ink/run-id` label and the in-pod CLI's `--run-id` flag, so
    callers know the trace URL the moment the Job is submitted.

    `question_annotation` overrides the human-readable label stamped on
    the Job for the /jobs UI. Useful for `continue_id` runs, where the
    caller can resolve the question's headline before submission.
    """
    batch, apps = _kube_clients()
    image, env, env_from = _read_api_runtime(apps)
    if spec.container_tag:
        image = _override_image_tag(image, spec.container_tag)
    name = _job_name(spec)
    run_id = str(uuid.uuid4())
    body = _build_job(
        spec,
        name=name,
        owner_user_id=owner_user_id,
        run_id=run_id,
        image=image,
        env=env,
        env_from=env_from,
        question_annotation=question_annotation,
    )
    batch.create_namespaced_job(namespace=NAMESPACE, body=body)
    log.info(
        "submitted orchestrator job name=%s run_id=%s owner=%s image=%s",
        name,
        run_id,
        owner_user_id or "<anon>",
        image,
    )
    return name, run_id


def build_logs_url(job_name: str) -> str:
    """Cloud Logging URL filtered to this job's pod stdout/stderr.

    Returns an empty string when GCP_PROJECT_ID is not configured (e.g. local
    dev) so callers can present a friendly fallback.
    """
    settings = get_settings()
    project = settings.gcp_project_id
    if not project:
        return ""
    cluster = settings.gcp_cluster_name or ""
    query_lines = [
        'resource.type="k8s_container"',
        f'resource.labels.namespace_name="{NAMESPACE}"',
        f'labels."k8s-pod/job-name"="{job_name}"',
    ]
    if cluster:
        query_lines.append(f'resource.labels.cluster_name="{cluster}"')
    query = "\n".join(query_lines)
    encoded = urllib.parse.quote(query, safe="")
    return f"https://console.cloud.google.com/logs/query;query={encoded}?project={project}"
