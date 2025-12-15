from __future__ import annotations

import importlib.util
import os
import shutil
import tempfile
from contextlib import contextmanager
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, Sequence
from zipfile import ZipFile

from inventory.forms import EksTerraformForm
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
    helper_path = Path(__file__).resolve().parents[4] / "terraform-eks2gke.py"
    if not helper_path.exists():
        raise TaskExecutionError("Helper script terraform-eks2gke.py not found in the repository root.")
    spec = importlib.util.spec_from_file_location("feature.terraform_eks2gke", helper_path)
    if spec is None or spec.loader is None:
        raise TaskExecutionError("Unable to load terraform-eks2gke helper module.")
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


def _build_args(clean_data: dict):
    helper = _helper_module()
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
        node_locations=clean_data.get("node_locations"),
        network=clean_data.get("network"),
        subnetwork=clean_data.get("subnetwork"),
        service_account=clean_data.get("service_account") or "",
        release_channel=clean_data.get("release_channel") or helper.DEFAULT_RELEASE_CHANNEL,
        private_nodes=bool(clean_data.get("private_nodes", True)),
        private_endpoint=bool(clean_data.get("private_endpoint")),
        master_ipv4_cidr=clean_data.get("master_ipv4_cidr"),
        node_pools=[],
        output_root=".",
        skip_terraform_validate=False,
    )


def _summarize_plan(
    cluster_name: str,
    plan: Dict[str, Any],
    nodegroup_summary: str,
    location_note: str | None,
    recommendations: Sequence[str],
    structure_msgs: Sequence[str],
    validation_msgs: Sequence[str],
) -> bytes:
    lines = [
        f"EKS cluster: {cluster_name}",
        f"GCP project: {plan.get('project')}",
        f"GKE location: {plan.get('location')}",
        f"GKE cluster name: {plan.get('cluster_name')}",
        f"Machine type: {plan.get('machine_type')} ({plan.get('node_cpu_vcpu')} vCPU / {plan.get('node_memory_mb')} MB)",
        f"Node counts (initial/min/max): {plan.get('total_initial_nodes')}/{plan.get('total_min_nodes')}/{plan.get('total_max_nodes')}",
        f"Workload footprint: {plan.get('workloads_cpu_vcpu'):.2f} vCPU / {plan.get('workloads_memory_mb')} MB",
    ]
    if plan.get("network"):
        lines.append(f"Network override: {plan.get('network')}")
    if plan.get("subnetwork"):
        lines.append(f"Subnetwork override: {plan.get('subnetwork')}")
    if plan.get("enable_private_nodes"):
        endpoint_desc = "private endpoint only" if plan.get("enable_private_endpoint") else "public endpoint exposed"
        cidr = plan.get("master_ipv4_cidr_block")
        cidr_note = f" (control plane CIDR {cidr})" if cidr else ""
        lines.append(f"Private nodes enabled: {endpoint_desc}{cidr_note}")
    else:
        lines.append("Private nodes disabled: nodes receive public IPs.")
    if plan.get("node_locations"):
        lines.append(f"Node locations: {', '.join(plan.get('node_locations'))}")
    if location_note:
        lines.append("")
        lines.append(location_note)
    lines.append("")
    lines.append("Nodegroup summary:")
    lines.append(nodegroup_summary.strip())
    if recommendations:
        lines.append("")
        lines.append("Recommendations:")
        lines.extend(f"- {rec}" for rec in recommendations)
    if structure_msgs:
        lines.append("")
        lines.append("Bundle checks:")
        lines.extend(f"- {msg}" for msg in structure_msgs)
    if validation_msgs:
        lines.append("")
        lines.append("Terraform validation:")
        lines.extend(f"- {msg}" for msg in validation_msgs)
    return "\n".join(lines).strip().encode("utf-8")


def run_eks_terraform_task(clean_data: dict) -> TaskExecutionResult:
    helper = _helper_module()
    access_key = clean_data["access_key"]
    secret_key = clean_data["secret_key"]
    region = clean_data["aws_region"]
    cluster_name = clean_data["cluster_name"]

    try:
        with _aws_env(access_key, secret_key, region):
            cluster_details, nodegroups = helper.collect_eks_nodegroups(cluster_name, region)
    except FileNotFoundError as exc:  # pragma: no cover - depends on host tooling
        raise TaskExecutionError("AWS CLI not found. Install the AWS CLI to use the EKS tooling.") from exc

    if not cluster_details:
        raise TaskExecutionError(f"Unable to describe EKS cluster '{cluster_name}'. Verify the name and region.")
    if not nodegroups:
        raise TaskExecutionError(f"No nodegroups discovered for EKS cluster '{cluster_name}'.")

    totals = helper.aggregate_nodegroup_resources(nodegroups)
    summary_text = helper.build_nodegroup_summary(nodegroups, totals[0], totals[1], totals[4])
    print(summary_text)

    args = _build_args(clean_data)
    resolved_location, location_note = helper.resolve_gcp_location(region, args.gcp_location)
    plan, _ = helper.build_gke_plan(cluster_name, region, args, nodegroups, resolved_location, location_note)
    recommendations = helper.build_plan_recommendations(plan, nodegroups)
    bundle = helper.deterministic_terraform_bundle(plan, cluster_name)
    structure_messages = helper.validate_terraform_bundle_structure(bundle)

    temp_dir = Path(tempfile.mkdtemp(prefix="eks_tf_"))
    try:
        helper.write_terraform_files(bundle, str(temp_dir), overwrite=True)
        validation_messages: list[str] = []
        try:
            validation_messages = helper.terraform_cli_validate(str(temp_dir))
        except RuntimeError as exc:
            print(f"Terraform validation failed: {exc}")
            validation_messages = [f"terraform validate failed: {exc}"]

        archive_bytes = _zip_directory(temp_dir)
        summary_bytes = _summarize_plan(
            cluster_name,
            plan,
            summary_text,
            location_note,
            recommendations,
            structure_messages,
            validation_messages,
        )
    finally:
        try:
            shutil.rmtree(temp_dir)
        except OSError:
            pass

    artifacts = [
        GeneratedArtifact(
            filename=f"{cluster_name}_eks_gke_terraform.zip",
            content=archive_bytes,
            content_type="application/zip",
        ),
        GeneratedArtifact(
            filename=f"{cluster_name}_eks_plan.txt",
            content=summary_bytes,
            content_type="text/plain",
        ),
    ]
    return TaskExecutionResult(artifacts)


automation_registry.register(
    TaskDefinition(
        task_id="eks_terraform",
        label="EKS -> GKE Terraform",
        description="Size a GKE cluster from an EKS footprint and emit Terraform configuration.",
        form_class=EksTerraformForm,
        runner=run_eks_terraform_task,
    )
)
