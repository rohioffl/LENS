import base64
import json
import queue
import threading
from contextlib import redirect_stderr, redirect_stdout
from io import BytesIO, StringIO, TextIOBase
from zipfile import ZipFile

import boto3
from django.http import HttpResponse, JsonResponse, StreamingHttpResponse
from django.shortcuts import render
from django.views.decorators.csrf import csrf_exempt

from feature import gcp_vpn, terraform_vpc

from inventory.services.task_registry import (
    TaskExecutionError,
    TaskExecutionResult,
    automation_registry,
)
from ecr2artifact import list_ecr_repositories, list_ecr_images, configure_boto3_session


def inventory_request_view(request):
    tasks = automation_registry.list()
    if not tasks:
        return HttpResponse("No automation tasks registered.", status=503)

    task_id = (
        request.POST.get("task_id")
        or request.GET.get("task")
        or automation_registry.default_task_id
    )
    try:
        task_def = automation_registry.get(task_id)
    except KeyError:
        task_def = automation_registry.get(automation_registry.default_task_id)
        task_id = task_def.task_id

    if request.method == "POST":
        form = task_def.form_class(request.POST)
        if form.is_valid():
            try:
                result = task_def.runner(form.cleaned_data)
            except TaskExecutionError as exc:
                form.add_error(None, str(exc))
            except Exception as exc:
                form.add_error(None, f"Unexpected error while running '{task_def.label}': {exc}")
            else:
                return _build_download_response(result)
    else:
        form = task_def.form_class(initial={"task_id": task_id})

    context = {
        "form": form,
        "tasks": tasks,
        "active_task_id": task_id,
    }
    return render(request, "inventory/index.html", context)


def _build_download_response(result: TaskExecutionResult) -> HttpResponse:
    artifacts = list(result.artifacts)
    if not artifacts:
        raise TaskExecutionError("The task completed without producing any downloadable files.")

    if len(artifacts) == 1:
        artifact = artifacts[0]
        response = HttpResponse(artifact.content, content_type=artifact.content_type)
        response["Content-Disposition"] = f'attachment; filename="{artifact.filename}"'
        return response

    archive = BytesIO()
    with ZipFile(archive, "w") as zip_file:
        for artifact in artifacts:
            zip_file.writestr(artifact.filename, artifact.content)
    archive.seek(0)
    archive_name = result.archive_name or "automation_artifacts.zip"

    response = HttpResponse(archive.getvalue(), content_type="application/zip")
    response["Content-Disposition"] = f'attachment; filename="{archive_name}"'
    return response


def _serialize_artifacts(result: TaskExecutionResult):
    return [
        {
            "filename": artifact.filename,
            "content_type": artifact.content_type,
            "data": base64.b64encode(artifact.content).decode("ascii"),
        }
        for artifact in result.artifacts
    ]


@csrf_exempt
def run_task_api(request):
    if request.method != "POST":
        return JsonResponse({"error": "Only POST is allowed."}, status=405)

    try:
        payload = json.loads(request.body or "{}")
    except json.JSONDecodeError:
        return JsonResponse({"error": "Invalid JSON payload."}, status=400)

    task_id = payload.get("task_id") or automation_registry.default_task_id
    form_data = payload.get("data") or {}
    form_data["task_id"] = task_id

    try:
        task_def = automation_registry.get(task_id)
    except KeyError:
        return JsonResponse({"error": f"Unknown task '{task_id}'."}, status=400)

    form = task_def.form_class(form_data)
    if not form.is_valid():
        return JsonResponse({"error": "Validation failed.", "details": form.errors}, status=400)

    log_stream = StringIO()
    try:
        with redirect_stdout(log_stream), redirect_stderr(log_stream):
            result = task_def.runner(form.cleaned_data)
    except TaskExecutionError as exc:
        return JsonResponse({"error": str(exc), "logs": log_stream.getvalue()}, status=400)
    except Exception as exc:
        return JsonResponse(
            {
                "error": f"Unexpected failure while running '{task_def.label}'.",
                "details": str(exc),
                "logs": log_stream.getvalue(),
            },
            status=500,
        )

    return JsonResponse(
        {
            "status": "ok",
            "task_id": task_id,
            "archive_name": result.archive_name,
            "artifacts": _serialize_artifacts(result),
            "logs": log_stream.getvalue(),
        }
    )


class _StreamingWriter(TextIOBase):
    def __init__(self, push):
        super().__init__()
        self._push = push
        self._buffer = ""

    def write(self, data):
        if not data:
            return 0
        self._buffer += data.replace("\r", "")
        while "\n" in self._buffer:
            line, self._buffer = self._buffer.split("\n", 1)
            if line:
                self._push(line)
        return len(data)

    def flush(self):
        if self._buffer.strip():
            self._push(self._buffer.strip())
        self._buffer = ""


@csrf_exempt
def run_task_stream(request):
    if request.method != "POST":
        return JsonResponse({"error": "Only POST is allowed."}, status=405)

    try:
        payload = json.loads(request.body or "{}")
    except json.JSONDecodeError:
        return JsonResponse({"error": "Invalid JSON payload."}, status=400)

    task_id = payload.get("task_id") or automation_registry.default_task_id
    form_data = payload.get("data") or {}
    form_data["task_id"] = task_id

    try:
        task_def = automation_registry.get(task_id)
    except KeyError:
        return JsonResponse({"error": f"Unknown task '{task_id}'."}, status=400)

    form = task_def.form_class(form_data)
    if not form.is_valid():
        return JsonResponse({"error": "Validation failed.", "details": form.errors}, status=400)

    event_queue: "queue.Queue[dict | None]" = queue.Queue()

    def push_event(event: dict):
        event_queue.put(event)

    def worker():
        writer = _StreamingWriter(lambda message: push_event({"event": "log", "message": message}))
        try:
            with redirect_stdout(writer), redirect_stderr(writer):
                result = task_def.runner(form.cleaned_data)
            writer.flush()
            push_event(
                {
                    "event": "result",
                    "status": "ok",
                    "task_id": task_id,
                    "archive_name": result.archive_name,
                    "artifacts": _serialize_artifacts(result),
                }
            )
        except TaskExecutionError as exc:
            push_event({"event": "result", "status": "error", "error": str(exc)})
        except Exception as exc:  # pragma: no cover - safety
            push_event(
                {
                    "event": "result",
                    "status": "error",
                    "error": f"Unexpected failure while running '{task_def.label}': {exc}",
                }
            )
        finally:
            event_queue.put(None)

    threading.Thread(target=worker, daemon=True).start()

    def event_stream():
        while True:
            item = event_queue.get()
            if item is None:
                break
            yield (json.dumps(item) + "\n").encode("utf-8")

    return StreamingHttpResponse(event_stream(), content_type="application/x-ndjson")


def _aws_creds_from_payload(payload):
    return {
        "access_key": payload.get("access_key"),
        "secret_key": payload.get("secret_key"),
        "session_token": payload.get("session_token"),
    }


@csrf_exempt
def aws_vpcs_api(request):
    if request.method != "POST":
        return JsonResponse({"error": "Only POST is allowed."}, status=405)

    try:
        payload = json.loads(request.body or "{}")
    except json.JSONDecodeError:
        return JsonResponse({"error": "Invalid JSON payload."}, status=400)

    region = payload.get("region")
    if not region:
        return JsonResponse({"error": "Missing required field 'region'."}, status=400)

    creds = _aws_creds_from_payload(payload)
    try:
        terraform_vpc.configure_boto3_session(**creds)
        vpcs = terraform_vpc.list_aws_vpcs(region)
    except Exception as exc:  # pragma: no cover - safety
        return JsonResponse({"error": f"Failed to list VPCs: {exc}"}, status=500)

    data = [
        {
            "id": vpc.get("VpcId"),
            "name": vpc.get("Name") or "",
            "cidr": vpc.get("CidrBlock", ""),
            "is_default": vpc.get("IsDefault", False),
        }
        for vpc in vpcs
    ]
    return JsonResponse({"vpcs": data})


@csrf_exempt
def aws_subnets_api(request):
    if request.method != "POST":
        return JsonResponse({"error": "Only POST is allowed."}, status=405)

    try:
        payload = json.loads(request.body or "{}")
    except json.JSONDecodeError:
        return JsonResponse({"error": "Invalid JSON payload."}, status=400)

    region = payload.get("region")
    vpc_id = payload.get("vpc_id")
    if not region or not vpc_id:
        return JsonResponse({"error": "Fields 'region' and 'vpc_id' are required."}, status=400)

    creds = _aws_creds_from_payload(payload)
    try:
        terraform_vpc.configure_boto3_session(**creds)
        vpc = terraform_vpc.discover_aws_vpc(vpc_id, region)
    except Exception as exc:
        return JsonResponse({"error": f"Failed to load VPC: {exc}"}, status=500)

    attached_vgw = terraform_vpc.discover_attached_vgw(region, vpc_id)
    items = []
    for subnet in vpc.subnets:
        items.append(
            {
                "id": subnet.id,
                "name": subnet.name,
                "cidr": subnet.cidr,
                "az": subnet.az,
                "map_public_ip_on_launch": subnet.map_public_ip_on_launch,
                "suggested_name": terraform_vpc.sanitize_name(subnet.name or subnet.id),
            }
        )

    return JsonResponse({"subnets": items, "attached_vgw": attached_vgw})


@csrf_exempt
def gcp_projects_api(request):
    if request.method != "POST":
        return JsonResponse({"error": "Only POST is allowed."}, status=405)

    try:
        payload = json.loads(request.body or "{}")
    except json.JSONDecodeError:
        return JsonResponse({"error": "Invalid JSON payload."}, status=400)

    service_key = payload.get("service_key")
    if not service_key:
        return JsonResponse({"error": "Field 'service_key' is required."}, status=400)

    try:
        default_project, projects = gcp_vpn.list_gcp_projects(service_key)
    except gcp_vpn.GcpVpnError as exc:
        return JsonResponse({"error": str(exc)}, status=400)
    except SystemExit as exc:  # pragma: no cover - missing deps
        return JsonResponse({"error": str(exc)}, status=400)
    except Exception as exc:  # pragma: no cover - safety
        return JsonResponse({"error": f"Failed to list GCP projects: {exc}"}, status=500)

    return JsonResponse({"project_id": default_project, "projects": projects})


@csrf_exempt
def gcp_networks_api(request):
    if request.method != "POST":
        return JsonResponse({"error": "Only POST is allowed."}, status=405)

    try:
        payload = json.loads(request.body or "{}")
    except json.JSONDecodeError:
        return JsonResponse({"error": "Invalid JSON payload."}, status=400)

    service_key = payload.get("service_key")
    project = payload.get("gcp_project")
    if not service_key:
        return JsonResponse({"error": "Field 'service_key' is required."}, status=400)

    try:
        resolved_project, networks = gcp_vpn.list_gcp_networks(service_key, project)
    except gcp_vpn.GcpVpnError as exc:
        return JsonResponse({"error": str(exc)}, status=400)
    except SystemExit as exc:  # missing deps from ensure_compute_client
        return JsonResponse({"error": str(exc)}, status=400)
    except Exception as exc:  # pragma: no cover - safety
        return JsonResponse({"error": f"Failed to list GCP networks: {exc}"}, status=500)

    return JsonResponse(
        {
            "project_id": resolved_project,
            "networks": networks,
        }
    )


@csrf_exempt
def gcp_network_detail_api(request):
    if request.method != "POST":
        return JsonResponse({"error": "Only POST is allowed."}, status=405)

    try:
        payload = json.loads(request.body or "{}")
    except json.JSONDecodeError:
        return JsonResponse({"error": "Invalid JSON payload."}, status=400)

    service_key = payload.get("service_key")
    project = payload.get("gcp_project")
    network = payload.get("gcp_network")
    region_filter = payload.get("gcp_region")
    if not service_key or not project or not network:
        return JsonResponse({"error": "Fields 'service_key', 'gcp_project', and 'gcp_network' are required."}, status=400)

    try:
        network_data = gcp_vpn.get_gcp_network(service_key, project, network, region_filter=region_filter)
    except gcp_vpn.GcpVpnError as exc:
        return JsonResponse({"error": str(exc)}, status=400)
    except SystemExit as exc:  # pragma: no cover - dependency hint
        return JsonResponse({"error": str(exc)}, status=400)
    except Exception as exc:  # pragma: no cover - safety
        return JsonResponse({"error": f"Failed to load GCP network: {exc}"}, status=500)

    return JsonResponse({"network": network_data})


@csrf_exempt
def aws_ecr_repos_api(request):
    if request.method != "POST":
        return JsonResponse({"error": "Only POST is allowed."}, status=405)

    try:
        payload = json.loads(request.body or "{}")
    except json.JSONDecodeError:
        return JsonResponse({"error": "Invalid JSON payload."}, status=400)

    region = payload.get("region")
    if not region:
        return JsonResponse({"error": "Missing required field 'region'."}, status=400)

    creds = _aws_creds_from_payload(payload)
    try:
        configure_boto3_session(
            access_key=creds.get("access_key"),
            secret_key=creds.get("secret_key"),
            session_token=None,
            profile_name=None,
        )
        repos = list_ecr_repositories(region)
    except Exception as exc:  # pragma: no cover - safety
        return JsonResponse({"error": f"Failed to list ECR repos: {exc}"}, status=500)

    data = [
        {
            "name": repo.get("repositoryName", ""),
            "uri": repo.get("repositoryUri", ""),
            "image_count": len(list_ecr_images(region, repo.get("repositoryName", ""))) if repo.get("repositoryName") else 0,
        }
        for repo in repos
    ]
    return JsonResponse({"repos": data})


def _boto3_client(service_name: str, region: str, creds: dict):
    session = boto3.Session(
        aws_access_key_id=creds.get("access_key"),
        aws_secret_access_key=creds.get("secret_key"),
        aws_session_token=creds.get("session_token"),
    )
    return session.client(service_name, region_name=region)


@csrf_exempt
def aws_ecs_clusters_api(request):
    if request.method != "POST":
        return JsonResponse({"error": "Only POST is allowed."}, status=405)

    try:
        payload = json.loads(request.body or "{}")
    except json.JSONDecodeError:
        return JsonResponse({"error": "Invalid JSON payload."}, status=400)

    region = payload.get("region")
    if not region:
        return JsonResponse({"error": "Missing required field 'region'."}, status=400)

    creds = _aws_creds_from_payload(payload)
    try:
        ecs = _boto3_client("ecs", region, creds)
        paginator = ecs.get_paginator("list_clusters")
        names: list[str] = []
        for page in paginator.paginate():
            for arn in page.get("clusterArns", []):
                if not isinstance(arn, str):
                    continue
                names.append(arn.split("/")[-1] if "/" in arn else arn)
    except Exception as exc:  # pragma: no cover - runtime guard
        return JsonResponse({"error": f"Failed to list ECS clusters: {exc}"}, status=500)

    deduped = sorted(set(names))
    return JsonResponse({"clusters": deduped})


@csrf_exempt
def aws_ecs_services_api(request):
    if request.method != "POST":
        return JsonResponse({"error": "Only POST is allowed."}, status=405)

    try:
        payload = json.loads(request.body or "{}")
    except json.JSONDecodeError:
        return JsonResponse({"error": "Invalid JSON payload."}, status=400)

    region = payload.get("region")
    cluster = payload.get("cluster")
    if not region or not cluster:
        return JsonResponse({"error": "Fields 'region' and 'cluster' are required."}, status=400)

    creds = _aws_creds_from_payload(payload)
    try:
        ecs = _boto3_client("ecs", region, creds)
        paginator = ecs.get_paginator("list_services")
        names: list[str] = []
        for page in paginator.paginate(cluster=cluster):
            for arn in page.get("serviceArns", []):
                if not isinstance(arn, str):
                    continue
                names.append(arn.split("/")[-1] if "/" in arn else arn)
    except Exception as exc:  # pragma: no cover - runtime guard
        return JsonResponse({"error": f"Failed to list ECS services: {exc}"}, status=500)

    deduped = sorted(set(names))
    return JsonResponse({"services": deduped})
