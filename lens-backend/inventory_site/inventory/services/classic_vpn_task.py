from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import List

from feature import aws_gcp_classic_vpn, gcp_vpn, terraform_vpc
from feature.vpn_report import build_vpn_report_workbook
from inventory.forms import ClassicVpnForm
from inventory.services.task_registry import (
    GeneratedArtifact,
    TaskDefinition,
    TaskExecutionError,
    TaskExecutionResult,
    automation_registry,
)

REPO_ROOT = Path(__file__).resolve().parents[4]
CLASSIC_VPN_SCRIPT = REPO_ROOT / "lens-backend" / "feature" / "aws_gcp_classic_vpn.py"
VPN_RUNS_DIR = REPO_ROOT / "lens-backend" / "feature" / "vpn_runs"


def _run_classic_vpn_script(cmd: List[str], env: dict) -> None:
    if not CLASSIC_VPN_SCRIPT.exists():
        raise TaskExecutionError("aws_gcp_classic_vpn.py script was not found.")
    process = subprocess.Popen(
        cmd,
        cwd=str(REPO_ROOT),
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
    )
    assert process.stdout is not None
    for line in process.stdout:
        print(line, end="")
        # ensure streaming clients receive output promptly
        sys.stdout.flush()
    process.wait()
    if process.returncode != 0:
        raise TaskExecutionError("Classic VPN provisioning failed. Review the logs for details.")


def _selected_gcp_cidrs(network: dict, selected_names: List[str] | None) -> List[str]:
    subnets = network.get("subnetworks") or []
    names = set(selected_names or [s["name"] for s in subnets])
    cidrs = []
    for subnet in subnets:
        if subnet["name"] not in names:
            continue
        cidr = subnet.get("cidr") or subnet.get("ip_cidr_range") or subnet.get("ipCidrRange")
        if not cidr:
            raise TaskExecutionError(f"CIDR not found for GCP subnet '{subnet['name']}'.")
        cidrs.append(cidr)
    if not cidrs:
        raise TaskExecutionError("No GCP subnet CIDRs selected.")
    return cidrs


def _discover_attached_vgw_asn(region: str, vpc_id: str) -> int | None:
    """Return the AmazonSideAsn of an already-attached VGW, if present."""
    try:
        import boto3  # local import to avoid hard dependency if unused
    except ImportError:
        return None
    ec2 = boto3.client("ec2", region_name=region)
    resp = ec2.describe_vpn_gateways(Filters=[{"Name": "attachment.vpc-id", "Values": [vpc_id]}])
    for gw in resp.get("VpnGateways", []):
        for att in gw.get("VpcAttachments", []):
            if att.get("VpcId") == vpc_id and att.get("State") == "attached":
                asn = gw.get("AmazonSideAsn")
                return int(asn) if asn else None
    return None


def run_classic_vpn_task(clean_data: dict) -> TaskExecutionResult:
    access_key = (clean_data["access_key"] or "").strip()
    secret_key = (clean_data["secret_key"] or "").strip()
    if not access_key or not secret_key:
        raise TaskExecutionError("AWS access key and secret key are required.")

    aws_region = clean_data["aws_region"]
    aws_vpc_id = clean_data["aws_vpc_id"]
    detected_asn = _discover_attached_vgw_asn(aws_region, aws_vpc_id)
    aws_asn = int(clean_data.get("aws_asn") or detected_asn or 64513)
    gcp_asn = int(clean_data.get("gcp_asn") or 64512)
    gcp_project = clean_data["gcp_project"]
    gcp_region = clean_data["gcp_region"]
    gcp_network_name = clean_data["gcp_network"]
    service_key = clean_data["gcp_service_key"]
    ike_version = int(clean_data.get("ike_version") or 1)

    terraform_vpc.configure_boto3_session(
        access_key=access_key,
        secret_key=secret_key,
        session_token=None,
        profile_name=None,
    )

    aws_vpc = terraform_vpc.discover_aws_vpc(aws_vpc_id, aws_region)
    gcp_network = gcp_vpn.get_gcp_network(service_key, gcp_project, gcp_network_name, region_filter=gcp_region)
    selected_gcp_names = clean_data.get("gcp_subnets") or [s["name"] for s in gcp_network["subnetworks"]]
    gcp_cidrs = _selected_gcp_cidrs(gcp_network, selected_gcp_names)

    selected_aws_subnets = clean_data.get("aws_subnets") or []
    all_aws_subnets = [subnet.id for subnet in aws_vpc.subnets]
    propagate_arg = None
    skip_propagation = False
    if selected_aws_subnets:
        if set(selected_aws_subnets) == set(all_aws_subnets):
            propagate_arg = "all"
        else:
            propagate_arg = ",".join(selected_aws_subnets)
    else:
        skip_propagation = True

    prefix = terraform_vpc.sanitize_name(clean_data.get("name_prefix") or f"classic-{aws_vpc_id}") or f"classic-{aws_vpc_id}"
    metadata_file = VPN_RUNS_DIR / f"{prefix}.json"
    try:
        metadata_file.unlink()
    except FileNotFoundError:
        pass

    temp_key = tempfile.NamedTemporaryFile("w", delete=False, suffix=".json")
    try:
        temp_key.write(service_key)
        temp_key.flush()
        temp_key.close()

        env = os.environ.copy()
        env.update(
            {
                "AWS_ACCESS_KEY_ID": access_key,
                "AWS_SECRET_ACCESS_KEY": secret_key,
                "AWS_DEFAULT_REGION": aws_region,
                "GOOGLE_APPLICATION_CREDENTIALS": temp_key.name,
            }
        )

        cmd = [
            sys.executable,
            "-u",
            str(CLASSIC_VPN_SCRIPT),
            "--aws-region",
            aws_region,
            "--aws-vpc-id",
            aws_vpc_id,
            "--aws-vpc-cidr",
            aws_vpc.cidr,
            "--aws-cgw-asn",
            str(aws_asn),
            "--gcp-project",
            gcp_project,
            "--gcp-network",
            gcp_network_name,
            "--gcp-region",
            gcp_region,
            "--gcp-subnets",
            ",".join(gcp_cidrs),
            "--gcp-asn",
            str(gcp_asn),
            "--prefix",
            prefix,
            "--ike-version",
            str(ike_version),
        ]
        if propagate_arg:
            cmd.extend(["--propagate-subnets", propagate_arg])
        if skip_propagation:
            cmd.append("--skip-route-propagation")

        _run_classic_vpn_script(cmd, env)
    finally:
        try:
            os.unlink(temp_key.name)
        except OSError:
            pass

    metadata_data = None
    if metadata_file.exists():
        try:
            metadata_data = json.loads(metadata_file.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            metadata_data = None
    if not isinstance(metadata_data, dict):
        # Fall back to a minimal metadata payload so downstream artifacts are always generated.
        metadata_data = {
            "timestamp": clean_data.get("timestamp"),
            "prefix": prefix,
            "config": {
                "aws_region": aws_region,
                "aws_vpc_id": aws_vpc_id,
                "aws_asn": aws_asn,
                "gcp_project": gcp_project,
                "gcp_region": gcp_region,
                "gcp_network": gcp_network_name,
                "gcp_asn": gcp_asn,
                "aws_subnets": selected_aws_subnets or all_aws_subnets,
                "gcp_subnets": selected_gcp_names,
            },
            "resources": {},
        }
    else:
        # Normalize resources for downstream reporting.
        resources = metadata_data.get("resources")
        if isinstance(resources, dict) and "gcp_tunnels" in resources:
            tunnels = resources.get("gcp_tunnels") or []
            if isinstance(tunnels, list):
                normalized = []
                for entry in tunnels:
                    if isinstance(entry, str):
                        normalized.append({"name": entry})
                    elif isinstance(entry, dict):
                        normalized.append(entry)
                resources["gcp_tunnels"] = normalized

    context = {
        "aws": {
            "id": aws_vpc.id,
            "cidr": aws_vpc.cidr,
            "name": aws_vpc.name,
            "region": aws_vpc.region,
            "subnets": [
                {"id": s.id, "name": s.name, "cidr": s.cidr, "az": s.az}
                for s in aws_vpc.subnets
            ],
        },
        "gcp": {
            "name": gcp_network["name"],
            "project": gcp_network["project"],
            "region": gcp_region,
            "subnetworks": gcp_network["subnetworks"],
            "selected_subnets": [s for s in gcp_network["subnetworks"] if s["name"] in set(selected_gcp_names)],
        },
        "vpn": {
            "name_prefix": prefix,
        },
    }

    artifacts: list[GeneratedArtifact] = []
    report_bytes = build_vpn_report_workbook(context, metadata_data)
    artifacts.append(
        GeneratedArtifact(
            filename=f"{prefix}_vpn_report.xlsx",
            content=report_bytes,
            content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        )
    )
    artifacts.append(
        GeneratedArtifact(
            filename=f"{prefix}_metadata.json",
            content=json.dumps(metadata_data, indent=2).encode("utf-8"),
            content_type="application/json",
        )
    )
    return TaskExecutionResult(artifacts)


automation_registry.register(
    TaskDefinition(
        task_id="classic_vpn",
        label="Classic AWS↔GCP VPN",
        description="Provision a static (Classic) VPN between AWS and GCP.",
        form_class=ClassicVpnForm,
        runner=run_classic_vpn_task,
    )
)
