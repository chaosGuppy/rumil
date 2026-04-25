"""Server-side Kubernetes Job submission for orchestrator runs.

Lives inside the API container and is invoked by `rumil.api.jobs`.
Uses the in-cluster ServiceAccount (`rumil-api`) for k8s API access; the
RBAC needed (jobs/pods/pods.log/deployments) lives in
`deploy/chart/templates/api-rbac.yaml`.

Design:
- Image and runtime env are read live from the running `rumil-api`
  Deployment, so orchestrator pods always run the same code as the
  currently-deployed API.
- The orchestrator pod is launched with a separate, RBAC-less SA
  (`rumil-orchestrator-job`) to limit blast radius if the orchestrator
  container is ever exploited.
- Logs are streamed back to the API caller via an async generator that
  bridges the kubernetes client's blocking iterator into asyncio.
"""

from __future__ import annotations

import asyncio
import logging
import re
import secrets
import threading
from collections.abc import AsyncIterator, Iterator, Sequence
from importlib import resources
from typing import Any, cast

import yaml
from kubernetes import client, config
from kubernetes.client.exceptions import ApiException

from rumil.k8s.types import OrchestratorRunRequest

NAMESPACE = "rumil"
API_DEPLOYMENT_NAME = "rumil-api"
ORCH_SA_NAME = "rumil-orchestrator-job"
JOB_LABEL_RUN_KIND = "rumil.ink/run-kind"
JOB_LABEL_OWNER = "rumil.ink/owner-user-id"
JOB_LABEL_RUN_KIND_VALUE = "orchestrator"

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


def _kube_clients() -> tuple[client.BatchV1Api, client.CoreV1Api, client.AppsV1Api]:
    _load_kube_config()
    return client.BatchV1Api(), client.CoreV1Api(), client.AppsV1Api()


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
) -> tuple[str, list[client.V1EnvVar], list[client.V1EnvFromSource]]:
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


def _build_container_command(spec: OrchestratorRunRequest) -> list[str]:
    """Translate the request into the in-pod CLI invocation."""
    args: list[str] = [
        "python",
        "main.py",
        spec.question,
        "--budget",
        str(spec.budget),
        "--workspace",
        spec.workspace,
        "--db",
        "prod",
        "--executor",
        "local",
    ]
    if spec.smoke_test:
        args.append("--smoke-test")
    if spec.no_summary:
        args.append("--no-summary")
    if spec.quiet:
        args.append("--quiet")
    if spec.debug:
        args.append("--debug")
    if spec.force_twophase_recurse:
        args.append("--force-twophase-recurse")
    if spec.no_trace:
        args.append("--no-trace")
    if spec.available_moves is not None:
        args += ["--available-moves", spec.available_moves]
    if spec.available_calls is not None:
        args += ["--available-calls", spec.available_calls]
    if spec.view_variant is not None:
        args += ["--view-variant", spec.view_variant]
    if spec.ingest_num_claims is not None:
        args += ["--ingest-num-claims", str(spec.ingest_num_claims)]
    if spec.run_name is not None:
        args += ["--run-name", spec.run_name]
    return args


def _build_job(
    spec: OrchestratorRunRequest,
    *,
    name: str,
    owner_user_id: str,
    image: str,
    env: Sequence[client.V1EnvVar],
    env_from: Sequence[client.V1EnvFromSource],
) -> dict[str, Any]:
    manifest = _load_manifest()
    manifest.setdefault("metadata", {})
    metadata: dict[str, Any] = manifest["metadata"]
    metadata["name"] = name
    metadata["namespace"] = NAMESPACE
    labels = dict(metadata.get("labels") or {})
    labels[JOB_LABEL_RUN_KIND] = JOB_LABEL_RUN_KIND_VALUE
    if owner_user_id:
        labels[JOB_LABEL_OWNER] = owner_user_id
    metadata["labels"] = labels

    spec_block: dict[str, Any] = manifest["spec"]
    pod_spec: dict[str, Any] = spec_block["template"]["spec"]
    containers: list[dict[str, Any]] = pod_spec["containers"]
    container = containers[0]
    container["image"] = image
    container["command"] = _build_container_command(spec)
    container["env"] = [_env_var_to_dict(e) for e in env]
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


def submit_orchestrator_job(spec: OrchestratorRunRequest, *, owner_user_id: str) -> str:
    """Create the Job and return its name. Raises on k8s API errors."""
    batch, _, apps = _kube_clients()
    image, env, env_from = _read_api_runtime(apps)
    if spec.container_tag:
        image = _override_image_tag(image, spec.container_tag)
    name = _job_name(spec)
    body = _build_job(
        spec, name=name, owner_user_id=owner_user_id, image=image, env=env, env_from=env_from
    )
    batch.create_namespaced_job(namespace=NAMESPACE, body=body)
    log.info(
        "submitted orchestrator job name=%s owner=%s image=%s",
        name,
        owner_user_id or "<anon>",
        image,
    )
    return name


def get_orchestrator_job(name: str) -> client.V1Job | None:
    """Fetch a Job, returning None if it doesn't exist or isn't an orchestrator run."""
    batch, _, _ = _kube_clients()
    try:
        job = cast(client.V1Job, batch.read_namespaced_job(name=name, namespace=NAMESPACE))
    except ApiException as exc:
        if exc.status == 404:
            return None
        raise
    metadata = job.metadata
    labels = (metadata.labels or {}) if metadata is not None else {}
    if labels.get(JOB_LABEL_RUN_KIND) != JOB_LABEL_RUN_KIND_VALUE:
        return None
    return job


def _find_pod_for_job(core: client.CoreV1Api, job_name: str) -> client.V1Pod | None:
    pod_list = cast(
        client.V1PodList,
        core.list_namespaced_pod(namespace=NAMESPACE, label_selector=f"job-name={job_name}"),
    )
    pods: list[client.V1Pod] = list(pod_list.items or [])
    if not pods:
        pod_list = cast(
            client.V1PodList,
            core.list_namespaced_pod(
                namespace=NAMESPACE,
                label_selector=f"batch.kubernetes.io/job-name={job_name}",
            ),
        )
        pods = list(pod_list.items or [])
    if not pods:
        return None

    def _created_at(p: client.V1Pod) -> str:
        ts = p.metadata.creation_timestamp if p.metadata is not None else None
        return str(ts) if ts is not None else ""

    pods.sort(key=_created_at)
    return pods[-1]


def _pod_terminal_phase(pod: client.V1Pod) -> tuple[str, int | None]:
    """Return (phase, exit_code) once the pod has terminated, else ('', None)."""
    status = pod.status
    statuses = (status.container_statuses if status is not None else None) or []
    for s in statuses:
        if s.state and s.state.terminated is not None:
            phase = (status.phase if status is not None else None) or "Unknown"
            return phase, int(s.state.terminated.exit_code or 0)
    if status is not None and status.phase in {"Succeeded", "Failed"}:
        return status.phase, None
    return "", None


async def _wait_for_pod_ready(
    core: client.CoreV1Api, job_name: str, *, timeout_s: float = 120.0
) -> client.V1Pod:
    deadline = asyncio.get_running_loop().time() + timeout_s
    while True:
        pod = await asyncio.to_thread(_find_pod_for_job, core, job_name)
        if pod is not None and pod.status is not None and pod.status.container_statuses:
            state = pod.status.container_statuses[0].state
            if state and (state.running or state.terminated):
                return pod
        if asyncio.get_running_loop().time() > deadline:
            raise TimeoutError(
                f"pod for job {job_name} did not become ready within {timeout_s:.0f}s"
            )
        await asyncio.sleep(1.0)


def _iter_pod_log_lines(core: client.CoreV1Api, pod_name: str) -> Iterator[bytes]:
    """Blocking iterator over log bytes from a pod (follow=True)."""
    response = core.read_namespaced_pod_log(
        name=pod_name,
        namespace=NAMESPACE,
        follow=True,
        _preload_content=False,
    )
    raw_iter = response.stream(decode_content=False)  # type: ignore[union-attr]
    try:
        for raw in raw_iter:
            if raw:
                yield cast(bytes, raw)
    finally:
        response.close()  # type: ignore[union-attr]


async def stream_job_logs(job_name: str) -> AsyncIterator[bytes]:
    """Yield raw log bytes from the orchestrator pod, then a trailing marker.

    Marker format: `\\n[rumil-job] phase=<phase> exit_code=<code>\\n`
    """
    _, core, _ = _kube_clients()
    try:
        pod = await _wait_for_pod_ready(core, job_name)
    except TimeoutError as exc:
        yield f"\n[rumil-job] phase=PodPending exit_code=-1 error={exc}\n".encode()
        return

    if pod.metadata is None or not pod.metadata.name:
        yield b"\n[rumil-job] phase=Unknown exit_code=-1 error=pod has no name\n"
        return
    pod_name = pod.metadata.name

    queue: asyncio.Queue[bytes | None] = asyncio.Queue()
    loop = asyncio.get_running_loop()

    def _producer() -> None:
        try:
            for chunk in _iter_pod_log_lines(core, pod_name):
                loop.call_soon_threadsafe(queue.put_nowait, chunk)
        except Exception as exc:
            msg = f"\n[rumil-job] log-stream error: {exc}\n".encode()
            loop.call_soon_threadsafe(queue.put_nowait, msg)
        finally:
            loop.call_soon_threadsafe(queue.put_nowait, None)

    threading.Thread(target=_producer, daemon=True, name=f"k8s-logs-{pod_name}").start()

    while True:
        chunk = await queue.get()
        if chunk is None:
            break
        yield chunk

    final_pod = cast(
        client.V1Pod,
        await asyncio.to_thread(core.read_namespaced_pod, pod_name, NAMESPACE),
    )
    phase, exit_code = _pod_terminal_phase(final_pod)
    if not phase:
        phase = "Unknown"
    code_str = str(exit_code) if exit_code is not None else "unknown"
    yield f"\n[rumil-job] phase={phase} exit_code={code_str}\n".encode()
