from __future__ import annotations

import json
import logging
import sys
import time
from typing import List

import boto3

from feature import ha_vpn
from feature.vpn_report import build_vpn_report_workbook
from inventory.forms import HaVpnForm
from inventory.services.task_registry import (
    GeneratedArtifact,
    TaskExecutionError,
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


def run_ha_vpn_task(clean_data: dict) -> TaskExecutionResult:
    access_key = clean_data["access_key"]
    secret_key = clean_data["secret_key"]
    if not access_key or not secret_key:
        raise TaskExecutionError("AWS access key and secret key are required.")

    ha_vpn.configure_boto3_session(
        access_key=access_key,
        secret_key=secret_key,
        session_token=None,
        profile_name=None,
    )

    aws_region = clean_data["aws_region"]
    aws_vpc_id = clean_data["aws_vpc_id"]
    aws_vpc = ha_vpn.discover_aws_vpc(aws_vpc_id, aws_region)
    all_subnet_ids = [subnet.id for subnet in aws_vpc.subnets]

    raw_subnet_selection = clean_data.get("aws_subnets")
    if raw_subnet_selection is None:
        propagate_targets = None
    else:
        valid_ids = set(all_subnet_ids)
        selected_aws_subnets = [sid for sid in raw_subnet_selection if sid in valid_ids]
        if not selected_aws_subnets:
            propagate_targets = []
        elif set(selected_aws_subnets) == valid_ids:
            propagate_targets = None
        else:
            propagate_targets = selected_aws_subnets

    prefix = clean_data.get("name_prefix") or f"ha-{aws_vpc_id}"
    prefix = ha_vpn.sanitize_name(prefix) or f"ha-{aws_vpc_id}"

    config = ha_vpn.HAVPNConfig()
    config.aws_region = aws_region
    config.aws_vpc_id = aws_vpc_id
    config.aws_asn = clean_data.get("aws_asn") or 64513
    config.gcp_project = clean_data["gcp_project"]
    config.gcp_region = clean_data["gcp_region"]
    config.gcp_network = clean_data["gcp_network"]
    config.gcp_asn = clean_data.get("gcp_asn") or 64512

    service_key = clean_data["gcp_service_key"]
    logger = ha_vpn.logger
    handler = logging.StreamHandler(sys.stdout)
    handler.setLevel(logging.INFO)
    formatter = logging.Formatter("%(asctime)s - %(levelname)s - %(message)s")
    handler.setFormatter(formatter)
    logger.addHandler(handler)
    original_propagate = logger.propagate
    logger.propagate = False

    try:
        metadata = ha_vpn.setup_ha_vpn(config, prefix, service_key=service_key)

        logger.info("Enabling route propagation on AWS route tables...")
        ec2 = boto3.client("ec2", region_name=config.aws_region)
        vgw_id = metadata["resources"]["aws_vgw_id"]
        if propagate_targets is None:
            ha_vpn.enable_route_propagation(ec2, config.aws_vpc_id, vgw_id)
        else:
            logger.info(
                "Targeting route tables for subnets: %s",
                ", ".join(propagate_targets),
            )
            ha_vpn.enable_route_propagation_for_subnets(
                ec2, config.aws_vpc_id, vgw_id, propagate_targets
            )

        logger.info("Waiting 30 seconds before checking tunnel status...")
        time.sleep(30)
        compute = ha_vpn.get_gcp_compute_service(service_key)
        tunnel_names = [t["name"] for t in metadata["resources"].get("gcp_tunnels", [])]
        if tunnel_names:
            ha_vpn.check_tunnel_status(compute, config, tunnel_names)
            ha_vpn.check_bgp_status(compute, config, ha_vpn.build_resource_names(prefix)["gcp_router"])

        context = {}
        plan_artifacts = ha_vpn.generate_ha_vpn_artifacts(
            access_key=access_key,
            secret_key=secret_key,
            aws_region=aws_region,
            aws_vpc_id=aws_vpc_id,
            gcp_service_key=service_key,
            gcp_project=clean_data["gcp_project"],
            gcp_region=clean_data["gcp_region"],
            gcp_network_name=clean_data["gcp_network"],
            aws_asn=config.aws_asn,
            gcp_asn=config.gcp_asn,
            name_prefix=prefix,
        )
        for artifact in plan_artifacts:
            if artifact["filename"] == "ha_vpn_context.json":
                try:
                    context = json.loads(artifact["content"].decode("utf-8"))
                except (json.JSONDecodeError, UnicodeDecodeError):
                    context = {}
                break
        report_bytes = build_vpn_report_workbook(context, metadata)
        report = GeneratedArtifact(
            filename=f"{prefix}_vpn_report.xlsx",
            content=report_bytes,
            content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        )
        return TaskExecutionResult([report])
    except Exception as exc:  # pragma: no cover - runtime safety
        raise TaskExecutionError(f"HA VPN setup failed: {exc}") from exc
    finally:
        logger.removeHandler(handler)
        logger.propagate = original_propagate


automation_registry.register(
    TaskDefinition(
        task_id="ha_vpn",
        label="AWS<->GCP HA VPN",
        description="Provision a high-availability AWS/GCP VPN with BGP routing.",
        form_class=HaVpnForm,
        runner=run_ha_vpn_task,
    )
)
