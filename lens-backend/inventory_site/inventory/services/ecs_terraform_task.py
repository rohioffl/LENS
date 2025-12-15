from __future__ import annotations

import importlib.util
import json
import os
import shutil
import tempfile
from contextlib import contextmanager
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, List, Sequence
from zipfile import ZipFile

from inventory.forms import EcsTerraformForm
from inventory.services.task_registry import (
    GeneratedArtifact,
    TaskDefinition,
    TaskExecutionError,
    TaskExecutionResult,
    automation_registry,
)

_HELPER_MODULE = None


def _helper_module():
    global _HELPER_MODULE  # noqa: PLW0603
    if _HELPER_MODULE is not None:
        return _HELPER_MODULE
    helper_path = Path(__file__).resolve().parents[3] / "feature" / "terraform-ecs2gke.py"
    if not helper_path.exists():
        raise TaskExecutionError("Helper script feature/terraform-ecs2gke.py not found.")
    spec = importlib.util.spec_from_file_location("feature.terraform_ecs2gke", helper_path)
    if spec is None or spec.loader is None:
        raise TaskExecutionError("Unable to load terraform-ecs2gke helper module.")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)  # type: ignore[attr-defined]
    _HELPER_MODULE = module
    return _HELPER_MODULE


@contextmanager
def _aws_env(access_key: str, secret_key: str, region: str):
    env_overrides = {
        "AWS_ACCESS_KEY_ID": access_key,
        "AWS_SECRET_ACCESS_KEY": secret_key,
        "AWS_DEFAULT_REGION": region,
        "AWS_REGION": region,
        "AWS_PAGER": "",
    }
    previous = {key: os.environ.get(key) for key in env_overrides}
    try:
        for key, value in env_overrides.items():
            if value:
                os.environ[key] = value
            elif key in os.environ:
                os.environ.pop(key, None)
        yield
    finally:
        for key, value in previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def _zip_directory(directory: Path) -> bytes:
    buffer = BytesIO()
    with ZipFile(buffer, "w") as archive:
        for file_path in directory.rglob("*"):
            if file_path.is_dir():
                continue
            archive.write(file_path, arcname=str(file_path.relative_to(directory)))
    buffer.seek(0)
    return buffer.getvalue()


def _split_csv(value: str | Sequence[str] | None) -> List[str]:
    if not value:
        return []
    if isinstance(value, str):
        items = value.split(",")
    else:
        items = list(value)
    return [item.strip() for item in items if str(item).strip()]


def _normalize_node_pools(data: dict) -> List[dict]:
    pools = data.get("node_pools")
    if isinstance(pools, str):
        try:
            pools = json.loads(pools)
        except json.JSONDecodeError as exc:  # pragma: no cover - validation guard
            raise TaskExecutionError("node_pools must be a JSON array.") from exc
    if not pools:
        name = data.get("node_pool_name")
        subnet = data.get("node_pool_subnet")
        zones = _split_csv(data.get("node_pool_zones"))
        if name or subnet or zones:
            return [
                {
                    "name": name or "primary",
                    "gcp_subnet": subnet or None,
                    "node_locations": zones,
                }
            ]
        return []
    if not isinstance(pools, list):
        raise TaskExecutionError("node_pools override must be a list.")
    normalized = []
    for pool in pools:
        if not isinstance(pool, dict):
            continue
        normalized.append(pool)
    return normalized


def _build_args(clean_data: dict, node_pools: List[dict]):
    helper = _helper_module()
    node_locations = clean_data.get("node_locations")
    if isinstance(node_locations, list):
        node_locations_str = ",".join(node_locations)
    else:
        node_locations_str = node_locations or ""
    return SimpleNamespace(
        cluster=clean_data["cluster_name"],
        region=clean_data["aws_region"],
        gcp_project=clean_data["gcp_project"],
        gcp_location=clean_data["gcp_location"],
        gke_cluster_name=clean_data.get("gke_cluster_name"),
        machine_type=clean_data.get("machine_type"),
        node_cpu=clean_data.get("node_cpu"),
        node_memory=clean_data.get("node_memory"),
        min_nodes=clean_data.get("min_nodes"),
        max_nodes=clean_data.get("max_nodes"),
        network=clean_data.get("network"),
        subnetwork=clean_data.get("subnetwork"),
        node_locations=node_locations_str,
        service_account=clean_data.get("service_account") or "",
        release_channel=clean_data.get("release_channel") or helper.DEFAULT_RELEASE_CHANNEL,
        private_nodes=bool(clean_data.get("private_nodes", True)),
        private_endpoint=bool(clean_data.get("private_endpoint")),
        master_ipv4_cidr=clean_data.get("master_ipv4_cidr"),
        node_pools=node_pools,
        output_root=".",
        skip_terraform_validate=bool(clean_data.get("skip_terraform_validate", False)),
    )


def _summarize_plan(plan: Dict[str, Any], recommendations: Sequence[str], structure_msgs: Sequence[str], validation_msgs: Sequence[str]) -> bytes:
    lines = [
        f"Project: {plan.get('project')}",
        f"Location: {plan.get('location')} ({plan.get('zone_count')} zones)",
        f"GKE Cluster: {plan.get('cluster_name')}",
        f"Machine Type: {plan.get('machine_type')} ({plan.get('node_cpu_vcpu')} vCPU / {plan.get('node_memory_mb')} MB)",
        f"Nodes (initial/min/max): {plan.get('total_initial_nodes')}/{plan.get('total_min_nodes')}/{plan.get('total_max_nodes')}",
        f"Workload footprint: {plan.get('workloads_cpu_vcpu'):.2f} vCPU / {plan.get('workloads_memory_mb')} MB",
    ]
    if plan.get("network"):
        lines.append(f"Network override: {plan.get('network')} / {plan.get('subnetwork') or '-'}")
    if structure_msgs:
        lines.append("")
        lines.append("Bundle checks:")
        lines.extend(f"- {msg}" for msg in structure_msgs)
    if validation_msgs:
        lines.append("")
        lines.append("Terraform validation:")
        lines.extend(f"- {msg}" for msg in validation_msgs)
    if recommendations:
        lines.append("")
        lines.append("Recommendations:")
        lines.extend(f"- {rec}" for rec in recommendations)
    return "\n".join(lines).strip().encode("utf-8")


def run_ecs_terraform_task(clean_data: dict) -> TaskExecutionResult:
    helper = _helper_module()
    access_key = clean_data["access_key"]
    secret_key = clean_data["secret_key"]
    region = clean_data["aws_region"]
    cluster_name = clean_data["cluster_name"]
    node_pools = _normalize_node_pools(clean_data)

    try:
        with _aws_env(access_key, secret_key, region):
            services_data = helper.collect_ecs_services(cluster_name, region)[1]
    except FileNotFoundError as exc:  # pragma: no cover - depends on host tooling
        raise TaskExecutionError("AWS CLI not found. Install the AWS CLI to use the ECS tooling.") from exc

    if not services_data:
        raise TaskExecutionError(f"No ECS services discovered in cluster '{cluster_name}'.")

    requested_services = clean_data.get("services") or []
    if isinstance(requested_services, str):
        try:
            requested_services = json.loads(requested_services)
        except json.JSONDecodeError:
            requested_services = [requested_services]
    requested_services = [svc.strip() for svc in requested_services if str(svc).strip()]
    if requested_services:
        service_lookup = {svc.name: svc for svc in services_data}
        missing = [svc for svc in requested_services if svc not in service_lookup]
        if missing:
            print(f"⚠️ Skipping unknown ECS services: {', '.join(missing)}")
        services_data = [service_lookup[name] for name in requested_services if name in service_lookup]
        if not services_data:
            raise TaskExecutionError("None of the requested ECS services were found in the cluster.")

    totals = helper.aggregate_service_resources(services_data)
    summary_text = helper.build_services_summary(services_data, totals[0], totals[1], totals[4])
    print(summary_text)

    args = _build_args(clean_data, node_pools)
    resolved_location, location_note = helper.resolve_gcp_location(region, args.gcp_location)
    plan, _ = helper.build_gke_plan(cluster_name, region, args, services_data, resolved_location, location_note)
    recommendations = helper.build_plan_recommendations(plan, services_data)
    bundle = helper.deterministic_terraform_bundle(plan, cluster_name)
    structure_messages = helper.validate_terraform_bundle_structure(bundle)

    temp_dir = Path(tempfile.mkdtemp(prefix="ecs_tf_"))
    try:
        helper.write_terraform_files(bundle, str(temp_dir), overwrite=True)
        validation_messages: List[str] = []
        if not bool(clean_data.get("skip_terraform_validate", False)):
            try:
                validation_messages = helper.terraform_cli_validate(str(temp_dir))
            except RuntimeError as exc:
                print(f"⚠️ Terraform validation failed: {exc}")
                validation_messages = [f"terraform validate failed: {exc}"]

        archive_bytes = _zip_directory(temp_dir)
        summary_bytes = _summarize_plan(plan, recommendations, structure_messages, validation_messages)
    finally:
        try:
            for item in temp_dir.iterdir():
                if item.is_file():
                    item.unlink(missing_ok=True)
                else:
                    shutil.rmtree(item, ignore_errors=True)
            temp_dir.rmdir()
        except OSError:
            pass

    artifacts = [
        GeneratedArtifact(
            filename=f"{cluster_name}_gke_terraform.zip",
            content=archive_bytes,
            content_type="application/zip",
        ),
        GeneratedArtifact(
            filename=f"{cluster_name}_plan.txt",
            content=summary_bytes,
            content_type="text/plain",
        ),
    ]
    return TaskExecutionResult(artifacts)


automation_registry.register(
    TaskDefinition(
        task_id="ecs_terraform",
        label="ECS → GKE Terraform",
        description="Size a GKE cluster from ECS services and emit Terraform configuration.",
        form_class=EcsTerraformForm,
        runner=run_ecs_terraform_task,
    )
)
