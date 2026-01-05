import base64
import os
import json
import subprocess
import queue
import sys
import threading
import tempfile
import time
from contextlib import redirect_stderr, redirect_stdout
from io import BytesIO, StringIO, TextIOBase
from pathlib import Path
from zipfile import ZipFile

import boto3
from django.http import HttpResponse, JsonResponse, StreamingHttpResponse
from django.shortcuts import render
from django.views.decorators.csrf import csrf_exempt

from feature import gcp_vpn, terraform_vpc, box_project

from inventory.services.task_registry import (
    TaskExecutionError,
    TaskExecutionResult,
    automation_registry,
)
from ecr2artifact import list_ecr_repositories, list_ecr_images, configure_boto3_session

BACKEND_ROOT = Path(__file__).resolve().parents[2]
EKS_MANIFEST_SCRIPT = BACKEND_ROOT / "feature" / "eks2gke-manifest-local.py"
ACCESS_DENIED_MARKERS = (
    "accessdenied",
    "unauthorized",
    "forbidden",
    "provide credentials",
    "cluster access request is unauthorized",
    "requested credentials",
)


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


def _maybe_append_eks_access_hint(message: str, cluster: str, region: str) -> str:
    text = message or ""
    lowered = text.lower()
    if any(marker in lowered for marker in ACCESS_DENIED_MARKERS):
        hint = (
            "\nIt looks like this IAM principal does not have access to the EKS cluster. "
            "Grant yourself access via the AWS CLI:\n\n"
            "Step 1: Add yourself as an EKS access entry\n"
            f"aws eks create-access-entry \\\n"
            f"  --cluster-name {cluster} \\\n"
            "  --principal-arn <YOUR_IAM_PRINCIPAL_ARN> \\\n"
            "  --type STANDARD \\\n"
            f"  --region {region}\n\n"
            "Step 2: Attach the admin policy\n"
            f"aws eks associate-access-policy \\\n"
            f"  --cluster-name {cluster} \\\n"
            "  --principal-arn <YOUR_IAM_PRINCIPAL_ARN> \\\n"
            "  --policy-arn arn:aws:eks::aws:cluster-access-policy/AmazonEKSClusterAdminPolicy \\\n"
            "  --access-scope type=cluster \\\n"
            f"  --region {region}\n"
        )
        return f"{text}{hint}"
    return text


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


@csrf_exempt
def aws_eks_clusters_api(request):
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
        eks = _boto3_client("eks", region, creds)
        paginator = eks.get_paginator("list_clusters")
        clusters: list[str] = []
        for page in paginator.paginate():
            for name in page.get("clusters", []):
                if isinstance(name, str) and name.strip():
                    clusters.append(name.strip())
    except Exception as exc:  # pragma: no cover - runtime guard
        return JsonResponse({"error": f"Failed to list EKS clusters: {exc}"}, status=500)

    return JsonResponse({"clusters": sorted(set(clusters))})


@csrf_exempt
def aws_eks_namespaces_api(request):
    if request.method != "POST":
        return JsonResponse({"error": "Only POST is allowed."}, status=405)
    if not EKS_MANIFEST_SCRIPT.exists():
        return JsonResponse({"error": "eks2gke-manifest-local.py is not available on the server."}, status=500)

    try:
        payload = json.loads(request.body or "{}")
    except json.JSONDecodeError:
        return JsonResponse({"error": "Invalid JSON payload."}, status=400)

    region = payload.get("region")
    cluster = payload.get("cluster")
    if not region or not cluster:
        return JsonResponse({"error": "Fields 'region' and 'cluster' are required."}, status=400)

    env = os.environ.copy()
    env.setdefault("AWS_PAGER", "")
    env.setdefault("PYTHONUNBUFFERED", "1")
    env["AWS_REGION"] = region
    env["AWS_DEFAULT_REGION"] = region
    if payload.get("access_key"):
        env["AWS_ACCESS_KEY_ID"] = payload["access_key"]
    if payload.get("secret_key"):
        env["AWS_SECRET_ACCESS_KEY"] = payload["secret_key"]
    if payload.get("session_token"):
        env["AWS_SESSION_TOKEN"] = payload["session_token"]

    with tempfile.TemporaryDirectory(prefix="eks_ns_") as tempdir:
        cmd = [
            sys.executable,
            str(EKS_MANIFEST_SCRIPT),
            "--cluster",
            cluster,
            "--region",
            region,
            "--outdir",
            tempdir,
            "--list-namespaces",
        ]
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            cwd=str(EKS_MANIFEST_SCRIPT.parent),
            env=env,
        )

    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "").strip() or "Unknown error."
        detail = _maybe_append_eks_access_hint(detail, cluster, region)
        return JsonResponse({"error": f"Failed to list namespaces: {detail}"}, status=500)

    namespaces: list[str] = []
    parsed_payload = False
    stdout = (proc.stdout or "").strip()
    for line in reversed(stdout.splitlines()):
        line = line.strip()
        if not line:
            continue
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            continue
        namespace_list = payload.get("namespaces")
        if isinstance(namespace_list, list):
            namespaces = [str(entry).strip() for entry in namespace_list if str(entry).strip()]
            parsed_payload = True
            break

    if not parsed_payload:
        detail = stdout.strip() or "Could not parse namespace output."
        detail = _maybe_append_eks_access_hint(detail, cluster, region)
        return JsonResponse({"error": detail}, status=500)

    return JsonResponse({"namespaces": sorted(set(namespaces))})


@csrf_exempt
def aws_ec2_instances_api(request):
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
        ec2 = _boto3_client("ec2", region, creds)
        paginator = ec2.get_paginator("describe_instances")
        instances: list[dict] = []
        for page in paginator.paginate():
            for reservation in page.get("Reservations", []):
                for instance in reservation.get("Instances", []):
                    instance_id = instance.get("InstanceId", "")
                    instance_name = instance_id
                    tags = instance.get("Tags", [])
                    for tag in tags:
                        if tag.get("Key") == "Name":
                            instance_name = tag.get("Value", instance_id)
                            break
                    instances.append({
                        "id": instance_id,
                        "name": instance_name,
                        "instance_type": instance.get("InstanceType", ""),
                        "state": instance.get("State", {}).get("Name", ""),
                        "private_ip": instance.get("PrivateIpAddress", ""),
                        "public_ip": instance.get("PublicIpAddress", ""),
                    })
    except Exception as exc:  # pragma: no cover - runtime guard
        return JsonResponse({"error": f"Failed to list EC2 instances: {exc}"}, status=500)

    return JsonResponse({"instances": sorted(instances, key=lambda x: x["name"])})


@csrf_exempt
def gcp_compute_instances_api(request):
    if request.method != "POST":
        return JsonResponse({"error": "Only POST is allowed."}, status=405)

    try:
        payload = json.loads(request.body or "{}")
    except json.JSONDecodeError:
        return JsonResponse({"error": "Invalid JSON payload."}, status=400)

    project = payload.get("project")
    zone = payload.get("zone")  # Optional
    service_key = payload.get("service_key")
    if not project:
        return JsonResponse({"error": "Missing required field 'project'."}, status=400)
    if not service_key:
        return JsonResponse({"error": "Missing required field 'service_key'."}, status=400)

    env = os.environ.copy()
    env.setdefault("PYTHONUNBUFFERED", "1")
    
    # Write service key to temporary file
    try:
        try:
            decoded = base64.b64decode(service_key).decode("utf-8")
        except Exception:
            decoded = service_key
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
            f.write(decoded)
            env["GOOGLE_APPLICATION_CREDENTIALS"] = f.name
    except Exception as exc:
        return JsonResponse({"error": f"Failed to process service account key: {exc}"}, status=400)

    try:
        gcloud_cmd = "gcloud.cmd" if os.name == "nt" else "gcloud"
        cmd = [
            gcloud_cmd,
            "compute",
            "instances",
            "list",
            "--project",
            project,
            "--format",
            "json",
        ]
        if zone:
            # Check if it's a zone (contains a dash and letter at the end like us-central1-a)
            # or a region (just region name like us-central1)
            # Zone format: us-central1-a, us-west1-b, etc. (ends with -a, -b, -c, etc.)
            if "-" in zone and len(zone.split("-")) >= 3 and zone[-1].isalpha() and zone[-2] == "-":
                # It's a zone (e.g., us-central1-a)
                cmd.extend(["--zones", zone])
            else:
                # It's a region (e.g., us-central1), filter by zone pattern
                cmd.extend(["--filter", f"zone:{zone}*"])
        
        # Activate service account
        try:
            activate_cmd = [
                gcloud_cmd,
                "auth",
                "activate-service-account",
                "--key-file",
                env["GOOGLE_APPLICATION_CREDENTIALS"],
                "--quiet",
            ]
            subprocess.run(activate_cmd, capture_output=True, env=env, timeout=10)
        except Exception:
            pass  # Continue even if activation fails
        
        proc = subprocess.run(cmd, text=True, capture_output=True, env=env, timeout=30)
        
        # Clean up temp file
        try:
            if env["GOOGLE_APPLICATION_CREDENTIALS"] and os.path.exists(env["GOOGLE_APPLICATION_CREDENTIALS"]):
                os.unlink(env["GOOGLE_APPLICATION_CREDENTIALS"])
        except Exception:
            pass
        
        if proc.returncode != 0:
            error_msg = (proc.stderr or proc.stdout or "").strip()
            return JsonResponse({"error": f"Failed to list GCP instances: {error_msg}"}, status=500)
        
        data = json.loads(proc.stdout) if proc.stdout.strip() else []
        instances = []
        for instance in data:
            instances.append({
                "id": instance.get("id", ""),
                "name": instance.get("name", ""),
                "machine_type": instance.get("machineType", "").split("/")[-1] if instance.get("machineType") else "",
                "status": instance.get("status", ""),
                "zone": instance.get("zone", "").split("/")[-1] if instance.get("zone") else "",
            })
    except FileNotFoundError:
        return JsonResponse({"error": "gcloud command not found. Please install Google Cloud SDK."}, status=500)
    except json.JSONDecodeError as exc:
        return JsonResponse({"error": f"Could not parse gcloud output: {exc}"}, status=500)
    except Exception as exc:  # pragma: no cover - safety
        return JsonResponse({"error": f"Failed to list GCP instances: {exc}"}, status=500)

    return JsonResponse({"instances": sorted(instances, key=lambda x: x["name"])})


@csrf_exempt
def gcp_instance_docker_containers_api(request):
    if request.method != "POST":
        return JsonResponse({"error": "Only POST is allowed."}, status=405)

    try:
        payload = json.loads(request.body or "{}")
    except json.JSONDecodeError:
        return JsonResponse({"error": "Invalid JSON payload."}, status=400)

    project = payload.get("project")
    instance_name = payload.get("instance_name")
    zone = payload.get("zone")
    service_key = payload.get("service_key")
    if not project or not instance_name or not zone:
        return JsonResponse({"error": "Missing required fields: project, instance_name, zone."}, status=400)
    if not service_key:
        return JsonResponse({"error": "Missing required field 'service_key'."}, status=400)

    env = os.environ.copy()
    env.setdefault("PYTHONUNBUFFERED", "1")
    
    # Write service key to temporary file
    try:
        try:
            decoded = base64.b64decode(service_key).decode("utf-8")
        except Exception:
            decoded = service_key
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
            f.write(decoded)
            env["GOOGLE_APPLICATION_CREDENTIALS"] = f.name
    except Exception as exc:
        return JsonResponse({"error": f"Failed to process service account key: {exc}"}, status=400)

    try:
        gcloud_cmd = "gcloud.cmd" if os.name == "nt" else "gcloud"
        
        # Activate service account
        try:
            activate_cmd = [
                gcloud_cmd,
                "auth",
                "activate-service-account",
                "--key-file",
                env["GOOGLE_APPLICATION_CREDENTIALS"],
                "--quiet",
            ]
            subprocess.run(activate_cmd, capture_output=True, env=env, timeout=10)
        except Exception:
            pass
        
        # Get running containers
        docker_ps_cmd = [
            gcloud_cmd,
            "compute",
            "ssh",
            instance_name,
            "--project",
            project,
            "--zone",
            zone,
            "--command",
            "sudo docker ps --format '{{.ID}}|{{.Image}}|{{.Names}}|{{.Status}}'",
            "--quiet",
        ]
        
        result = subprocess.run(docker_ps_cmd, text=True, capture_output=True, timeout=30, env=env)
        containers = []
        if result.returncode == 0 and result.stdout and result.stdout.strip():
            for line in result.stdout.strip().splitlines():
                if "|" in line:
                    parts = line.split("|")
                    if len(parts) >= 4:
                        containers.append({
                            "id": parts[0][:12],
                            "image": parts[1],
                            "name": parts[2],
                            "status": parts[3],
                        })
        
        # Get Docker images
        docker_images_cmd = [
            gcloud_cmd,
            "compute",
            "ssh",
            instance_name,
            "--project",
            project,
            "--zone",
            zone,
            "--command",
            "sudo docker images --format '{{.Repository}}:{{.Tag}}|{{.ID}}'",
            "--quiet",
        ]
        
        images = []
        result = subprocess.run(docker_images_cmd, text=True, capture_output=True, timeout=30, env=env)
        if result.returncode == 0 and result.stdout and result.stdout.strip():
            for line in result.stdout.strip().splitlines():
                if "|" in line:
                    parts = line.split("|")
                    if len(parts) >= 2:
                        images.append({
                            "image": parts[0],
                            "id": parts[1][:12],
                        })
        
        # Get detailed information (ports, env vars) from containers
        env_vars = {}
        for container in containers:
            # Get environment variables
            env_cmd = [
                gcloud_cmd,
                "compute",
                "ssh",
                instance_name,
                "--project",
                project,
                "--zone",
                zone,
                "--command",
                f"sudo docker inspect {container['id']} --format '{{{{range .Config.Env}}}}{{{{.}}}}\\n{{{{end}}}}'",
                "--quiet",
            ]
            
            # Get full container inspect for ports
            inspect_cmd = [
                gcloud_cmd,
                "compute",
                "ssh",
                instance_name,
                "--project",
                project,
                "--zone",
                zone,
                "--command",
                f"sudo docker inspect {container['id']} --format '{{{{json .}}}}'",
                "--quiet",
            ]
            
            try:
                # Get env vars
                result = subprocess.run(env_cmd, text=True, capture_output=True, timeout=30, env=env)
                if result.returncode == 0 and result.stdout and result.stdout.strip():
                    container_env_vars = {}
                    for line in result.stdout.strip().splitlines():
                        if "=" in line:
                            key, value = line.split("=", 1)
                            container_env_vars[key] = value
                    if container_env_vars:
                        env_vars[container["name"]] = container_env_vars
                
                # Get ports from full inspect
                result = subprocess.run(inspect_cmd, text=True, capture_output=True, timeout=30, env=env)
                if result.returncode == 0 and result.stdout and result.stdout.strip():
                    try:
                        import json
                        inspect_data = json.loads(result.stdout.strip())
                        # Extract exposed ports
                        exposed_ports = []
                        config_ports = inspect_data.get("Config", {}).get("ExposedPorts", {})
                        if config_ports:
                            for port_spec in config_ports.keys():
                                if "/" in port_spec:
                                    port, protocol = port_spec.split("/", 1)
                                else:
                                    port, protocol = port_spec, "tcp"
                                try:
                                    exposed_ports.append({"port": int(port), "protocol": protocol.upper()})
                                except ValueError:
                                    pass
                        
                        # Extract published ports
                        published_ports = []
                        network_settings = inspect_data.get("NetworkSettings", {}).get("Ports", {})
                        if network_settings:
                            for port_spec, bindings in network_settings.items():
                                if "/" in port_spec:
                                    port, protocol = port_spec.split("/", 1)
                                else:
                                    port, protocol = port_spec, "tcp"
                                try:
                                    port_num = int(port)
                                    if bindings and isinstance(bindings, list) and len(bindings) > 0:
                                        host_port = bindings[0].get("HostPort")
                                        published_ports.append({
                                            "container_port": port_num,
                                            "host_port": int(host_port) if host_port else None,
                                            "protocol": protocol.upper()
                                        })
                                    else:
                                        published_ports.append({
                                            "container_port": port_num,
                                            "host_port": None,
                                            "protocol": protocol.upper()
                                        })
                                except (ValueError, TypeError):
                                    pass
                        
                        # Use published ports if available, otherwise exposed ports
                        if published_ports:
                            container["ports"] = published_ports
                        elif exposed_ports:
                            container["ports"] = exposed_ports
                        else:
                            container["ports"] = []
                    except json.JSONDecodeError:
                        pass
            except Exception:
                pass
        
        # Clean up temp file
        try:
            if env["GOOGLE_APPLICATION_CREDENTIALS"] and os.path.exists(env["GOOGLE_APPLICATION_CREDENTIALS"]):
                os.unlink(env["GOOGLE_APPLICATION_CREDENTIALS"])
        except Exception:
            pass
        
        return JsonResponse({
            "containers": containers,
            "images": images,
            "env_vars": env_vars,
        })
    except FileNotFoundError:
        return JsonResponse({"error": "gcloud command not found. Please install Google Cloud SDK."}, status=500)
    except subprocess.TimeoutExpired:
        return JsonResponse({"error": "SSH connection timeout"}, status=500)
    except subprocess.CalledProcessError as exc:
        stderr = exc.stderr.decode("utf-8", errors="ignore") if exc.stderr else str(exc)
        if "not found" in stderr.lower() or "does not exist" in stderr.lower():
            return JsonResponse({"error": "Instance not found or SSH access denied"}, status=500)
        elif "permission denied" in stderr.lower() or "permission" in stderr.lower():
            return JsonResponse({"error": "Permission denied accessing Docker (may need sudo or Docker group membership)"}, status=500)
        else:
            return JsonResponse({"error": f"Could not SSH to instance or Docker not installed: {stderr}"}, status=500)
    except Exception as exc:  # pragma: no cover - safety
        return JsonResponse({"error": f"Failed to get Docker containers: {exc}"}, status=500)


@csrf_exempt
def aws_instance_docker_containers_api(request):
    if request.method != "POST":
        return JsonResponse({"error": "Only POST is allowed."}, status=405)

    try:
        payload = json.loads(request.body or "{}")
    except json.JSONDecodeError:
        return JsonResponse({"error": "Invalid JSON payload."}, status=400)

    instance_id = payload.get("instance_id")
    region = payload.get("region")
    access_key = payload.get("access_key")
    secret_key = payload.get("secret_key")
    if not instance_id or not region:
        return JsonResponse({"error": "Missing required fields: instance_id, region."}, status=400)
    if not access_key or not secret_key:
        return JsonResponse({"error": "Missing required fields: access_key, secret_key."}, status=400)

    env = os.environ.copy()
    env.update({
        "AWS_ACCESS_KEY_ID": access_key,
        "AWS_SECRET_ACCESS_KEY": secret_key,
        "AWS_DEFAULT_REGION": region,
        "AWS_REGION": region,
        "AWS_PAGER": "",
    })

    try:
        # Check if Docker is installed and get running containers
        docker_ps_cmd = [
            "aws",
            "ssm",
            "send-command",
            "--instance-ids",
            instance_id,
            "--region",
            region,
            "--document-name",
            "AWS-RunShellScript",
            "--parameters",
            "commands=['docker ps --format \"{{.ID}}|{{.Image}}|{{.Names}}|{{.Status}}\"']",
            "--output",
            "json",
        ]
        
        result = subprocess.run(docker_ps_cmd, text=True, capture_output=True, timeout=30, env=env)
        if result.returncode != 0:
            return JsonResponse({"error": "SSM command failed or instance not accessible"}, status=500)
        
        command_data = json.loads(result.stdout) if result.stdout else {}
        command_id = command_data.get("Command", {}).get("CommandId", "")
        
        if not command_id:
            return JsonResponse({"error": "Could not send SSM command"}, status=500)
        
        # Wait for command to execute
        time.sleep(2)
        
        # Get command output
        output_cmd = [
            "aws",
            "ssm",
            "get-command-invocation",
            "--command-id",
            command_id,
            "--instance-id",
            instance_id,
            "--region",
            region,
            "--output",
            "json",
        ]
        
        output_result = subprocess.run(output_cmd, text=True, capture_output=True, timeout=30, env=env)
        invocation = json.loads(output_result.stdout) if output_result.stdout else {}
        
        containers = []
        status = invocation.get("Status", "")
        if status == "Success":
            stdout = invocation.get("StandardOutputContent", "").strip()
            if stdout:
                for line in stdout.splitlines():
                    if "|" in line:
                        parts = line.split("|")
                        if len(parts) >= 4:
                            containers.append({
                                "id": parts[0][:12],
                                "image": parts[1],
                                "name": parts[2],
                                "status": parts[3],
                            })
        else:
            error_msg = invocation.get("StandardErrorContent", "") or status
            if "InvalidInstanceId" in error_msg or "not registered" in error_msg.lower():
                return JsonResponse({"error": "SSM agent not installed or instance not registered with SSM"}, status=500)
            elif "AccessDenied" in error_msg or "UnauthorizedOperation" in error_msg:
                return JsonResponse({"error": "Insufficient IAM permissions for SSM (requires ssm:SendCommand)"}, status=500)
            else:
                return JsonResponse({"error": f"SSM command failed: {error_msg}"}, status=500)
        
        # Get Docker images
        docker_images_cmd = [
            "aws",
            "ssm",
            "send-command",
            "--instance-ids",
            instance_id,
            "--region",
            region,
            "--document-name",
            "AWS-RunShellScript",
            "--parameters",
            "commands=['docker images --format \"{{.Repository}}:{{.Tag}}|{{.ID}}\"']",
            "--output",
            "json",
        ]
        
        images = []
        result = subprocess.run(docker_images_cmd, text=True, capture_output=True, timeout=30, env=env)
        if result.returncode == 0:
            command_data = json.loads(result.stdout) if result.stdout else {}
            command_id = command_data.get("Command", {}).get("CommandId", "")
            
            if command_id:
                time.sleep(2)
                output_result = subprocess.run(output_cmd, text=True, capture_output=True, timeout=30, env=env)
                invocation = json.loads(output_result.stdout) if output_result.stdout else {}
                
                if invocation.get("Status") == "Success":
                    stdout = invocation.get("StandardOutputContent", "").strip()
                    if stdout:
                        for line in stdout.splitlines():
                            if "|" in line:
                                parts = line.split("|")
                                if len(parts) >= 2:
                                    images.append({
                                        "image": parts[0],
                                        "id": parts[1][:12],
                                    })
        
        # Get environment variables from containers
        env_vars = {}
        for container in containers:
            env_cmd = [
                "aws",
                "ssm",
                "send-command",
                "--instance-ids",
                instance_id,
                "--region",
                region,
                "--document-name",
                "AWS-RunShellScript",
                "--parameters",
                f"commands=['docker inspect {container['id']} --format \"{{{{range .Config.Env}}}}{{{{.}}}}\\n{{{{end}}}}\"']",
                "--output",
                "json",
            ]
            
            try:
                result = subprocess.run(env_cmd, text=True, capture_output=True, timeout=30, env=env)
                if result.returncode == 0:
                    command_data = json.loads(result.stdout) if result.stdout else {}
                    command_id = command_data.get("Command", {}).get("CommandId", "")
                    
                    if command_id:
                        time.sleep(2)
                        output_result = subprocess.run(output_cmd, text=True, capture_output=True, timeout=30, env=env)
                        invocation = json.loads(output_result.stdout) if output_result.stdout else {}
                        
                        if invocation.get("Status") == "Success":
                            stdout = invocation.get("StandardOutputContent", "").strip()
                            if stdout:
                                container_env_vars = {}
                                for line in stdout.splitlines():
                                    if "=" in line:
                                        key, value = line.split("=", 1)
                                        container_env_vars[key] = value
                                if container_env_vars:
                                    env_vars[container["name"]] = container_env_vars
                
                # Get ports from full inspect
                ports_cmd = [
                    "aws",
                    "ssm",
                    "send-command",
                    "--instance-ids",
                    instance_id,
                    "--region",
                    region,
                    "--document-name",
                    "AWS-RunShellScript",
                    "--parameters",
                    f"commands=['docker inspect {container['id']} --format \"{{{{json .}}}}\"']",
                    "--output",
                    "json",
                ]
                
                result = subprocess.run(ports_cmd, text=True, capture_output=True, timeout=30, env=env)
                if result.returncode == 0:
                    command_data = json.loads(result.stdout) if result.stdout else {}
                    command_id = command_data.get("Command", {}).get("CommandId", "")
                    
                    if command_id:
                        time.sleep(2)
                        output_result = subprocess.run(output_cmd, text=True, capture_output=True, timeout=30, env=env)
                        invocation = json.loads(output_result.stdout) if output_result.stdout else {}
                        
                        if invocation.get("Status") == "Success":
                            stdout = invocation.get("StandardOutputContent", "").strip()
                            if stdout:
                                try:
                                    inspect_data = json.loads(stdout)
                                    # Extract exposed ports
                                    exposed_ports = []
                                    config_ports = inspect_data.get("Config", {}).get("ExposedPorts", {})
                                    if config_ports:
                                        for port_spec in config_ports.keys():
                                            if "/" in port_spec:
                                                port, protocol = port_spec.split("/", 1)
                                            else:
                                                port, protocol = port_spec, "tcp"
                                            try:
                                                exposed_ports.append({"port": int(port), "protocol": protocol.upper()})
                                            except ValueError:
                                                pass
                                    
                                    # Extract published ports
                                    published_ports = []
                                    network_settings = inspect_data.get("NetworkSettings", {}).get("Ports", {})
                                    if network_settings:
                                        for port_spec, bindings in network_settings.items():
                                            if "/" in port_spec:
                                                port, protocol = port_spec.split("/", 1)
                                            else:
                                                port, protocol = port_spec, "tcp"
                                            try:
                                                port_num = int(port)
                                                if bindings and isinstance(bindings, list) and len(bindings) > 0:
                                                    host_port = bindings[0].get("HostPort")
                                                    published_ports.append({
                                                        "container_port": port_num,
                                                        "host_port": int(host_port) if host_port else None,
                                                        "protocol": protocol.upper()
                                                    })
                                                else:
                                                    published_ports.append({
                                                        "container_port": port_num,
                                                        "host_port": None,
                                                        "protocol": protocol.upper()
                                                    })
                                            except (ValueError, TypeError):
                                                pass
                                    
                                    # Use published ports if available, otherwise exposed ports
                                    if published_ports:
                                        container["ports"] = published_ports
                                    elif exposed_ports:
                                        container["ports"] = exposed_ports
                                    else:
                                        container["ports"] = []
                                except json.JSONDecodeError:
                                    pass
            except Exception:
                pass
        
        return JsonResponse({
            "containers": containers,
            "images": images,
            "env_vars": env_vars,
        })
    except subprocess.CalledProcessError as exc:
        return JsonResponse({"error": f"Failed to get Docker containers: {exc}"}, status=500)
    except Exception as exc:  # pragma: no cover - safety
        return JsonResponse({"error": f"Failed to get Docker containers: {exc}"}, status=500)


@csrf_exempt
def box_project_metadata_api(request):
    if request.method != "POST":
        return JsonResponse({"error": "Only POST is allowed."}, status=405)
    try:
        payload = json.loads(request.body or "{}")
    except json.JSONDecodeError:
        return JsonResponse({"error": "Invalid JSON payload."}, status=400)
    cloud = (payload.get("cloud_provider") or "").lower()
    if cloud not in box_project.TOP_SERVICES:
        return JsonResponse({"error": "Unknown cloud provider."}, status=400)

    services = [
        {"id": svc, "label": label}
        for svc, label in box_project.TOP_SERVICES.get(cloud, [])
    ]
    inputs = box_project.MODULE_INPUTS.get(cloud, {})
    return JsonResponse({"services": services, "inputs": inputs})
