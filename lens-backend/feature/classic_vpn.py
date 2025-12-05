"""
Classic VPN helper — plans an AWS ⇄ GCP site-to-site VPN and returns shareable artifacts.

The logic mirrors the structure used by ``terraform_vpc`` but focuses on collating the
information required to stitch a traditional IPSec tunnel (AWS Virtual Private Gateway +
GCP Cloud VPN/Router).  The module also exposes helpers for listing GCP networks so both
the CLI and the Django API can drive pickers in the UI.
"""

from __future__ import annotations

import base64
import json
import textwrap
from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Optional, Tuple

from . import terraform_vpc as vpc_mod

try:  # pragma: no cover - optional dependency
    from google.cloud import resourcemanager_v3
except ImportError:  # pragma: no cover - dependency hint
    resourcemanager_v3 = None

configure_boto3_session = vpc_mod.configure_boto3_session
discover_aws_vpc = vpc_mod.discover_aws_vpc
sanitize_name = vpc_mod.sanitize_name
AWS_TO_GCP_REGION = vpc_mod.AWS_TO_GCP_REGION
ensure_compute_client = vpc_mod.ensure_compute_client
compute_v1 = vpc_mod.compute_v1
gcp_exceptions = vpc_mod.gcp_exceptions
service_account = vpc_mod.service_account

SCOPES = ["https://www.googleapis.com/auth/cloud-platform"]


class ClassicVpnError(RuntimeError):
    """Raised when VPN planning or discovery fails."""


def _decode_service_key(raw_value: str) -> Dict[str, Any]:
    text = (raw_value or "").strip()
    if not text:
        raise ClassicVpnError("GCP service key is required.")
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        try:
            decoded = base64.b64decode(text).decode("utf-8")
        except Exception as exc:  # pragma: no cover - guardrail
            raise ClassicVpnError("GCP service key must be valid JSON or base64 encoded JSON.") from exc
        try:
            return json.loads(decoded)
        except json.JSONDecodeError as exc:  # pragma: no cover - guardrail
            raise ClassicVpnError("Decoded GCP service key is not valid JSON.") from exc


def _build_gcp_credentials(service_key: str) -> Tuple[Any, str]:
    ensure_compute_client()
    info = _decode_service_key(service_key)
    if service_account is None:  # pragma: no cover - dependency hint
        raise ClassicVpnError("google-auth is required to work with service-account keys.")
    credentials = service_account.Credentials.from_service_account_info(info, scopes=SCOPES)
    project_id = info.get("project_id")
    if not project_id:
        raise ClassicVpnError("Service account JSON is missing 'project_id'.")
    return credentials, project_id


def _ensure_resource_manager_client() -> None:
    if resourcemanager_v3 is None:
        raise ClassicVpnError(
            "google-cloud-resource-manager is required to list GCP projects. Install with `pip install google-cloud-resource-manager`."
        )
    if gcp_exceptions is None:
        raise ClassicVpnError("google-api-core is required to talk to the Resource Manager API. Install with `pip install google-api-core`.")


def _resolve_subnetwork_details(credentials, project: str, subnetwork_url: str) -> Dict[str, Any]:
    parts = subnetwork_url.split("/")
    try:
        region_idx = parts.index("regions")
        region = parts[region_idx + 1]
        name = parts[-1]
    except (ValueError, IndexError):
        region = "unknown"
        name = parts[-1]
    subnet_client = compute_v1.SubnetworksClient(credentials=credentials)
    cidr = None
    try:
        subnet = subnet_client.get(project=project, region=region, subnetwork=name)
        cidr = getattr(subnet, "ip_cidr_range", None)
    except gcp_exceptions.GoogleAPICallError:
        cidr = None
    return {"name": name, "region": region, "cidr": cidr}


def list_gcp_networks(service_key: str, project_id: Optional[str] = None) -> Tuple[str, List[Dict[str, Any]]]:
    """Return all GCP VPC networks visible to the provided service account."""
    if compute_v1 is None:  # pragma: no cover - dependency hint
        raise ClassicVpnError("google-cloud-compute is required to list GCP VPCs.")
    credentials, inferred_project = _build_gcp_credentials(service_key)
    project = project_id or inferred_project
    client = compute_v1.NetworksClient(credentials=credentials)
    networks: List[Dict[str, Any]] = []
    try:
        for network in client.list(project=project):
            networks.append(
                {
                    "name": network.name,
                    "auto_create_subnetworks": getattr(network, "auto_create_subnetworks", False),
                    "routing_mode": getattr(getattr(network, "routing_config", None), "routing_mode", "REGIONAL"),
                    "subnet_count": len(getattr(network, "subnetworks", []) or []),
                }
            )
    except gcp_exceptions.GoogleAPICallError as exc:  # pragma: no cover - API guard
        raise ClassicVpnError(f"Failed to list GCP networks: {exc}") from exc
    return project, networks


def list_gcp_projects(service_key: str) -> Tuple[str, List[Dict[str, Any]]]:
    """Return all active GCP projects visible to the provided service account."""

    _ensure_resource_manager_client()
    credentials, inferred_project = _build_gcp_credentials(service_key)
    client = resourcemanager_v3.ProjectsClient(credentials=credentials)
    projects: List[Dict[str, Any]] = []

    def _append(project_obj) -> None:
        if not project_obj:
            return
        state = getattr(project_obj, "state", None)
        if state and state != resourcemanager_v3.Project.State.ACTIVE:
            return
        project_id = getattr(project_obj, "project_id", None) or ""
        projects.append(
            {
                "project_id": project_id,
                "display_name": getattr(project_obj, "display_name", None) or project_id or "Unnamed project",
                "name": getattr(project_obj, "name", ""),
                "project_number": str(getattr(project_obj, "project_number", "") or ""),
            }
        )

    request = resourcemanager_v3.SearchProjectsRequest()
    try:
        for project in client.search_projects(request=request):
            _append(project)
    except gcp_exceptions.GoogleAPICallError as exc:  # pragma: no cover - API guard
        code = getattr(exc, "code", None)
        if code in (401, 403) or exc.__class__.__name__ in {"PermissionDenied", "Unauthenticated"}:
            projects.clear()
        else:
            raise ClassicVpnError(f"Failed to list GCP projects: {exc}") from exc

    if not projects:
        projects.append(
            {
                "project_id": inferred_project,
                "display_name": inferred_project,
                "name": f"projects/{inferred_project}",
                "project_number": "",
            }
        )

    projects.sort(key=lambda item: (item.get("display_name") or item.get("project_id") or "").lower())
    return inferred_project, projects


def get_gcp_network(service_key: str, project_id: str, network_name: str) -> Dict[str, Any]:
    if not network_name:
        raise ClassicVpnError("A GCP VPC network must be selected.")
    credentials, inferred_project = _build_gcp_credentials(service_key)
    project = project_id or inferred_project
    client = compute_v1.NetworksClient(credentials=credentials)
    try:
        network = client.get(project=project, network=network_name)
    except gcp_exceptions.NotFound as exc:
        raise ClassicVpnError(f"GCP network '{network_name}' not found in project '{project}'.") from exc
    subnetworks: List[Dict[str, Any]] = []
    for url in getattr(network, "subnetworks", []) or []:
        subnetworks.append(_resolve_subnetwork_details(credentials, project, url))
    return {
        "name": network.name,
        "project": project,
        "auto_create_subnetworks": getattr(network, "auto_create_subnetworks", False),
        "routing_mode": getattr(getattr(network, "routing_config", None), "routing_mode", "REGIONAL"),
        "subnetworks": subnetworks,
        "self_link": getattr(network, "self_link", ""),
    }


def _render_plan_markdown(context: Dict[str, Any]) -> str:
    aws = context["aws"]
    gcp = context["gcp"]
    vpn = context["vpn"]
    text = f"""
    # AWS ↔ GCP Classic VPN Plan

    ## AWS Inputs
    - Region: **{aws['region']}**
    - VPC: **{aws['id']}** `{aws['cidr']}`
    - Subnets:
{''.join([f"      - {entry['name'] or entry['id']} ({entry['cidr']}) @ {entry['az']}\n" for entry in aws['subnets']] ) or '      - (all)\\n'}

    ## GCP Inputs
    - Project: **{gcp['project']}**
    - Network: **{gcp['name']}**
    - Region: **{gcp['region']}**
    - Subnets:
{''.join([f"      - {entry['name']} ({entry.get('cidr') or 'unknown'}) @ {entry['region']}\n" for entry in gcp['selected_subnets']] ) or '      - (all)\\n'}

    ## Suggested Resource Names
    - Customer Gateway: `{vpn['customer_gateway']}`
    - Virtual Private Gateway: `{vpn['vgw_name']}`
    - Cloud Router: `{vpn['router_name']}`
    - Cloud VPN Gateway: `{vpn['vpn_gateway']}`
    - Tunnel (AWS side): `{vpn['tunnel_name']}`

    ## High-level Steps
    ### AWS
    1. Attach a Virtual Private Gateway (`{vpn['vgw_name']}`) to VPC **{aws['id']}**.
    2. Allocate an Elastic IP and associate it with the gateway.
    3. Create a Customer Gateway referencing the Cloud VPN public IP (after the GCP side is provisioned).
    4. Create a Site-to-Site VPN connection targeting the Customer Gateway and Virtual Private Gateway.
    5. Update route tables to point `{aws['cidr']}` towards the VPN connection.

    ### GCP
    1. Create a Cloud Router `{vpn['router_name']}` in region **{gcp['region']}** attached to network **{gcp['name']}**.
    2. Reserve two external IPs for the Cloud VPN gateway and create gateway `{vpn['vpn_gateway']}`.
    3. Create a Cloud VPN tunnel `{vpn['tunnel_name']}` that targets the AWS Elastic IP from step 2.
    4. Configure BGP (if desired) or static routes that cover `{aws['cidr']}` ↔ `{gcp['placeholder_cidr']}`.

    ## Notes
    - Replace placeholder CIDRs and public IPs once the tunnel endpoints are created.
    - Consider HA-VPN if you need resiliency across multiple availability zones.
    """
    return textwrap.dedent(text).strip() + "\n"


def _render_sample_config(context: Dict[str, Any]) -> str:
    aws = context["aws"]
    gcp = context["gcp"]
    vpn = context["vpn"]
    sample = f"""
    aws_customer_gateway \"{vpn['customer_gateway']}\" {{
      bgp_asn    = 65010
      ip_address = \"<GCP_PUBLIC_IP>\"
      type       = \"ipsec.1\"
      tags = {{
        Name = \"{vpn['customer_gateway']}\"
      }}
    }}

    google_compute_router \"{vpn['router_tf']}\" {{
      name    = \"{vpn['router_name']}\"
      region  = \"{gcp['region']}\"
      network = \"{gcp['name']}\"
      bgp {{
        asn = 64514
      }}
    }}

    # Adapt the above snippet for Terraform or translate to console/API calls.
    """
    return textwrap.dedent(sample).strip() + "\n"


def _build_context(
    aws_vpc,
    gcp_network: Dict[str, Any],
    gcp_region: str,
) -> Dict[str, Any]:
    vpn_names = {
        "customer_gateway": sanitize_name(f"cg-{aws_vpc.id}"),
        "vgw_name": sanitize_name(f"vgw-{aws_vpc.id}"),
        "router_name": sanitize_name(f"router-{gcp_network['name']}-{gcp_region}"),
        "vpn_gateway": sanitize_name(f"vpn-{gcp_network['name']}-{gcp_region}"),
        "tunnel_name": sanitize_name(f"tunnel-{aws_vpc.id}-{gcp_region}"),
        "router_tf": sanitize_name(f"router-{gcp_region}"),
    }
    placeholder_cidr = (
        gcp_network["subnetworks"][0].get("cidr", "10.0.0.0/24") if gcp_network["subnetworks"] else "10.0.0.0/24"
    )
    return {
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
            "routing_mode": gcp_network["routing_mode"],
            "subnetworks": gcp_network["subnetworks"],
            "selected_subnets": gcp_network["subnetworks"],
        },
        "vpn": {**vpn_names, "placeholder_cidr": placeholder_cidr},
    }


def generate_classic_vpn_artifacts(
    access_key: str,
    secret_key: str,
    aws_region: str,
    aws_vpc_id: str,
    gcp_service_key: str,
    gcp_project: str,
    gcp_region: str,
    gcp_network_name: str,
    aws_subnet_ids: Optional[List[str]] = None,
    gcp_subnet_names: Optional[List[str]] = None,
) -> List[Dict[str, Any]]:
    if not access_key or not secret_key:
        raise ClassicVpnError("AWS access key and secret key are required.")
    configure_boto3_session(access_key=access_key, secret_key=secret_key, session_token=None, profile_name=None)
    aws_vpc = discover_aws_vpc(aws_vpc_id, aws_region)
    if aws_subnet_ids:
        aws_vpc.subnets = [subnet for subnet in aws_vpc.subnets if subnet.id in set(aws_subnet_ids)] or aws_vpc.subnets
    gcp_network = get_gcp_network(gcp_service_key, gcp_project, gcp_network_name)
    if gcp_subnet_names:
        gcp_network["subnetworks"] = [
            subnet for subnet in gcp_network["subnetworks"] if subnet["name"] in set(gcp_subnet_names)
        ] or gcp_network["subnetworks"]
    context = _build_context(aws_vpc, gcp_network, gcp_region)
    plan_md = _render_plan_markdown(context)
    sample_tf = _render_sample_config(context)
    metadata = json.dumps(context, indent=2)
    return [
        {"filename": "classic_vpn_plan.md", "content": plan_md.encode("utf-8"), "content_type": "text/markdown"},
        {"filename": "classic_vpn_samples.tf", "content": sample_tf.encode("utf-8"), "content_type": "text/plain"},
        {"filename": "vpn_context.json", "content": metadata.encode("utf-8"), "content_type": "application/json"},
    ]


def main(argv: Optional[Iterable[str]] = None) -> None:  # pragma: no cover - convenience CLI
    import argparse
    import sys
    from pathlib import Path

    parser = argparse.ArgumentParser(description="Plan an AWS ↔ GCP classic VPN.")
    parser.add_argument("--aws-region", required=True)
    parser.add_argument("--aws-vpc-id", required=True)
    parser.add_argument("--access-key", required=True)
    parser.add_argument("--secret-key", required=True)
    parser.add_argument("--gcp-service-key", required=True, help="Path to a service-account JSON file.")
    parser.add_argument("--gcp-project", required=True)
    parser.add_argument("--gcp-region", required=True)
    parser.add_argument("--gcp-network", required=True)
    parser.add_argument("--aws-subnets", nargs="*", help="Optional list of AWS subnet IDs to include.")
    parser.add_argument("--gcp-subnets", nargs="*", help="Optional list of GCP subnet names to include.")
    parser.add_argument("--output", default="classic_vpn_artifacts", help="Directory for generated files.")
    args = parser.parse_args(argv)

    service_key_path = Path(args.gcp_service_key)
    if not service_key_path.exists():
        parser.error(f"GCP service key not found: {service_key_path}")
    service_key_data = service_key_path.read_text(encoding="utf-8")
    artifacts = generate_classic_vpn_artifacts(
        access_key=args.access_key,
        secret_key=args.secret_key,
        aws_region=args.aws_region,
        aws_vpc_id=args.aws_vpc_id,
        gcp_service_key=service_key_data,
        gcp_project=args.gcp_project,
        gcp_region=args.gcp_region,
        gcp_network_name=args.gcp_network,
        aws_subnet_ids=args.aws_subnets,
        gcp_subnet_names=args.gcp_subnets,
    )
    output_dir = Path(args.output).resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    for artifact in artifacts:
        path = output_dir / artifact["filename"]
        path.write_bytes(artifact["content"])
        print(f"Wrote {path}")


if __name__ == "__main__":  # pragma: no cover
    main()
