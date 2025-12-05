from __future__ import annotations

from typing import List

from feature import classic_vpn
from inventory.forms import ClassicVpnForm
from inventory.services.task_registry import (
    GeneratedArtifact,
    TaskDefinition,
    TaskExecutionResult,
    automation_registry,
)


def _artifact_specs_to_objects(specs: List[dict]) -> List[GeneratedArtifact]:
    artifacts = []
    for spec in specs:
        artifacts.append(
            GeneratedArtifact(
                filename=spec["filename"],
                content=spec["content"],
                content_type=spec["content_type"],
            )
        )
    return artifacts


def run_classic_vpn_task(clean_data: dict) -> TaskExecutionResult:
    artifacts = classic_vpn.generate_classic_vpn_artifacts(
        access_key=clean_data["access_key"],
        secret_key=clean_data["secret_key"],
        aws_region=clean_data["aws_region"],
        aws_vpc_id=clean_data["aws_vpc_id"],
        gcp_service_key=clean_data["gcp_service_key"],
        gcp_project=clean_data["gcp_project"],
        gcp_region=clean_data["gcp_region"],
        gcp_network_name=clean_data["gcp_network"],
        aws_subnet_ids=clean_data.get("aws_subnets"),
        gcp_subnet_names=clean_data.get("gcp_subnets"),
    )
    generated = _artifact_specs_to_objects(artifacts)
    return TaskExecutionResult(generated)


automation_registry.register(
    TaskDefinition(
        task_id="classic_vpn",
        label="Classic AWS↔GCP VPN",
        description="Plan a site-to-site tunnel between an AWS VPC and a GCP VPC network.",
        form_class=ClassicVpnForm,
        runner=run_classic_vpn_task,
    )
)
