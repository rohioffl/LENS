#!/usr/bin/env python3
import argparse
import base64
import ipaddress
import json
import os
import re
import shutil
import subprocess
import sys
import textwrap
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Set, Tuple


def _author_signature() -> int:
    return sum(value << (idx * 8) for idx, value in enumerate((0x52, 0x6F, 0x68, 0x69, 0x74)))

GCP_CLIENT_KWARGS: Dict[str, Any] = {}


try:
    import boto3
except ImportError:  # pragma: no cover - dependency hint
    boto3 = None

try:
    from botocore.exceptions import ClientError
except ImportError:  # pragma: no cover - dependency hint
    ClientError = None

try:
    from google.cloud import compute_v1
except ImportError:  # pragma: no cover - dependency hint
    compute_v1 = None

try:
    from google.api_core import exceptions as gcp_exceptions
except ImportError:  # pragma: no cover - dependency hint
    gcp_exceptions = None

try:
    from google.oauth2 import service_account
except ImportError:  # pragma: no cover - dependency hint
    service_account = None


def ensure_boto3():
    if boto3 is None:
        raise SystemExit("boto3 is required for AWS API access. Install with `pip install boto3`." )


def ensure_botocore():
    if ClientError is None:
        raise SystemExit("botocore is required for AWS API access. Install with `pip install botocore`." )


def ensure_compute_client():
    if compute_v1 is None:
        raise SystemExit("google-cloud-compute is required for GCP API access. Install with `pip install google-cloud-compute`.")
    if gcp_exceptions is None:
        raise SystemExit("google-api-core is required for GCP API access. Install with `pip install google-api-core`.")
    if GCP_CLIENT_KWARGS.get("credentials") is not None and service_account is None:
        raise SystemExit("google-auth is required for credential overrides. Install with `pip install google-auth`." )


def configure_boto3_session(access_key: Optional[str] = None, secret_key: Optional[str] = None,
                            session_token: Optional[str] = None, profile_name: Optional[str] = None) -> None:
    """Configure boto3's default session so downstream helpers reuse the provided credentials."""
    ensure_boto3()
    session_kwargs: Dict[str, Any] = {}
    if profile_name:
        session_kwargs["profile_name"] = profile_name
    if access_key and secret_key:
        session_kwargs["aws_access_key_id"] = access_key
        session_kwargs["aws_secret_access_key"] = secret_key
        if session_token:
            session_kwargs["aws_session_token"] = session_token
    elif any((access_key, secret_key, session_token)):
        raise SystemExit("Both AWS access key and secret key are required when overriding credentials.")

    if not session_kwargs:
        # Nothing to override; keep existing environment/profile resolution.
        return

    boto3.setup_default_session(**session_kwargs)


def print_api(plan: str, **extra: Any) -> None:
    if extra:
        details = " ".join(f"{k}={v}" for k, v in extra.items())
        print(f"GCP API ⇒ {plan} {details}")
    else:
        print(f"GCP API ⇒ {plan}")


def build_compute_client(client_cls):
    ensure_compute_client()
    return client_cls(**GCP_CLIENT_KWARGS)


def wait_for_operation(project: str, operation: Any, region: Optional[str] = None) -> None:
    if not operation or not operation.name:
        return
    if region:
        op_client = build_compute_client(compute_v1.RegionOperationsClient)
        result = op_client.wait(project=project, region=region, operation=operation.name)
    else:
        op_client = build_compute_client(compute_v1.GlobalOperationsClient)
        result = op_client.wait(project=project, operation=operation.name)
    if result.error and result.error.errors:
        messages = ", ".join(err.message for err in result.error.errors if getattr(err, "message", None))
        raise RuntimeError(f"GCP operation failed: {messages}")


def configure_gcp_credentials(path: Optional[str]) -> None:
    global GCP_CLIENT_KWARGS
    if not path:
        return
    if service_account is None:
        raise SystemExit("google-auth is required to use --credential-file-override. Install with `pip install google-auth`.")
    if not os.path.exists(path):
        raise SystemExit(f"Credential file not found: {path}")
    scopes = ["https://www.googleapis.com/auth/cloud-platform"]
    creds = service_account.Credentials.from_service_account_file(path, scopes=scopes)
    GCP_CLIENT_KWARGS = {"credentials": creds}
    os.environ.setdefault("GOOGLE_APPLICATION_CREDENTIALS", path)


def resolve_workspace_path(path: str) -> str:
    """Force generated artifacts into the ./terraform/vpc workspace tree."""
    workspace_root = os.path.abspath(os.path.join(os.getcwd(), "terraform", "vpc"))
    os.makedirs(workspace_root, exist_ok=True)
    if os.path.isabs(path):
        relative = os.path.relpath(path, os.sep)
        target = os.path.join(workspace_root, relative)
    else:
        target = os.path.join(workspace_root, path)
    os.makedirs(target, exist_ok=True)
    return os.path.abspath(target)


# ---------- Data models

@dataclass
class AwsSubnet:
    id: str
    cidr: str
    az: str
    name: str = ""
    map_public_ip_on_launch: bool = False

@dataclass
class AwsNatGateway:
    id: str
    subnet_id: str
    state: str
    allocation_ids: List[str] = field(default_factory=list)

@dataclass
class AwsInternetGateway:
    id: str
    attachment_states: Dict[str, str] = field(default_factory=dict)

@dataclass
class AwsSgRule:
    direction: str  # ingress | egress
    protocol: str   # -1, tcp, udp, icmp
    port_from: Optional[int]
    port_to: Optional[int]
    cidrs: List[str] = field(default_factory=list)
    description: str = ""

@dataclass
class SubnetPlan:
    source: AwsSubnet
    target_name: str
    target_region: str
    target_cidr: str

@dataclass
class AwsVpc:
    id: str
    cidr: str
    name: str
    region: str
    subnets: List[AwsSubnet] = field(default_factory=list)
    routes: List[Dict[str, Any]] = field(default_factory=list)
    route_tables: Dict[str, List[Dict[str, Any]]] = field(default_factory=dict)
    subnet_route_tables: Dict[str, List[Dict[str, Any]]] = field(default_factory=dict)
    nat_gateways: List[AwsNatGateway] = field(default_factory=list)
    internet_gateways: List[AwsInternetGateway] = field(default_factory=list)
    sgs: Dict[str, List[AwsSgRule]] = field(default_factory=dict)

@dataclass
class GcpVpc:
    name: str
    project: str
    subnets: List[Dict[str, Any]] = field(default_factory=list)  # {name, region, ipCidrRange}
    routes: List[Dict[str, Any]] = field(default_factory=list)
    exists: bool = False

# ---------- Discovery (AWS)

def get_tag_name(tags: List[Dict[str, str]]) -> str:
    for t in tags or []:
        if t.get("Key") == "Name":
            return t.get("Value", "")
    return ""

def list_aws_vpcs(region: str) -> List[Dict[str, Any]]:
    ensure_boto3()
    ensure_botocore()
    ec2 = boto3.client("ec2", region_name=region)
    try:
        resp = ec2.describe_vpcs()
    except ClientError as exc:
        raise SystemExit(f"Failed to list VPCs in region {region}: {exc}")
    vpcs = resp.get("Vpcs", [])
    for vpc in vpcs:
        vpc["Name"] = get_tag_name(vpc.get("Tags", []))
    return vpcs


def discover_aws_vpc(vpc_id: str, region: str) -> AwsVpc:
    ensure_boto3()
    ensure_botocore()
    ec2 = boto3.client("ec2", region_name=region)

    try:
        vpcs = ec2.describe_vpcs(VpcIds=[vpc_id])["Vpcs"]
    except ClientError as exc:
        raise SystemExit(f"Could not describe AWS VPC {vpc_id}: {exc}")
    if not vpcs:
        raise SystemExit("No such VPC.")
    vpc = vpcs[0]
    vpc_name = get_tag_name(vpc.get("Tags", []))
    vpc_cidr = vpc["CidrBlock"]

    subnets: List[AwsSubnet] = []
    try:
        subs_resp = ec2.describe_subnets(Filters=[{"Name": "vpc-id", "Values": [vpc_id]}])
    except ClientError as exc:
        raise SystemExit(f"Failed to describe subnets: {exc}")
    for s in subs_resp.get("Subnets", []):
        subnets.append(AwsSubnet(
            id=s["SubnetId"],
            cidr=s["CidrBlock"],
            az=s["AvailabilityZone"],
            name=get_tag_name(s.get("Tags", [])),
            map_public_ip_on_launch=s.get("MapPublicIpOnLaunch", False),
        ))

    routes: List[Dict[str, Any]] = []
    route_tables: Dict[str, List[Dict[str, Any]]] = {}
    subnet_route_tables: Dict[str, List[Dict[str, Any]]] = {}
    try:
        rt_resp = ec2.describe_route_tables(Filters=[{"Name": "vpc-id", "Values": [vpc_id]}])
    except ClientError as exc:
        raise SystemExit(f"Failed to describe route tables: {exc}")
    for rt in rt_resp.get("RouteTables", []):
        rt_routes = rt.get("Routes", [])
        rt_id = rt.get("RouteTableId", "rt-unknown")
        route_tables[rt_id] = rt_routes
        is_main = any(a.get("Main") for a in rt.get("Associations", []))
        if is_main:
            routes.extend(rt_routes)
        for assoc in rt.get("Associations", []):
            subnet_id = assoc.get("SubnetId")
            if subnet_id:
                subnet_route_tables[subnet_id] = rt_routes

    sgs: Dict[str, List[AwsSgRule]] = {}
    try:
        sgs_resp = ec2.describe_security_groups(Filters=[{"Name": "vpc-id", "Values": [vpc_id]}])
    except ClientError as exc:
        raise SystemExit(f"Failed to describe security groups: {exc}")
    for sg in sgs_resp.get("SecurityGroups", []):
        rules: List[AwsSgRule] = []
        for perm in sg.get("IpPermissions", []):
            rules.extend(_aws_perm_to_rules(perm, "ingress"))
        for perm in sg.get("IpPermissionsEgress", []):
            rules.extend(_aws_perm_to_rules(perm, "egress"))
        sgs[get_tag_name(sg.get("Tags", [])) or sg["GroupId"]] = rules

    nat_gateways: List[AwsNatGateway] = []
    try:
        nat_resp = ec2.describe_nat_gateways(Filters=[{"Name": "vpc-id", "Values": [vpc_id]}])
    except (ClientError, AttributeError):
        nat_resp = {"NatGateways": []}
    for ng in nat_resp.get("NatGateways", []):
        allocations = [addr.get("AllocationId") for addr in ng.get("NatGatewayAddresses", []) if addr.get("AllocationId")]
        nat_gateways.append(AwsNatGateway(
            id=ng.get("NatGatewayId", "unknown"),
            subnet_id=ng.get("SubnetId", ""),
            state=ng.get("State", "unknown"),
            allocation_ids=allocations,
        ))

    internet_gateways: List[AwsInternetGateway] = []
    try:
        igw_resp = ec2.describe_internet_gateways(Filters=[{"Name": "attachment.vpc-id", "Values": [vpc_id]}])
    except (ClientError, AttributeError):
        igw_resp = {"InternetGateways": []}
    for igw in igw_resp.get("InternetGateways", []):
        attachments = {}
        for att in igw.get("Attachments", []):
            vpc_att = att.get("VpcId", "")
            state = att.get("State", "unknown")
            if vpc_att:
                attachments[vpc_att] = state
        internet_gateways.append(AwsInternetGateway(
            id=igw.get("InternetGatewayId", "unknown"),
            attachment_states=attachments,
        ))

    return AwsVpc(
        id=vpc_id,
        cidr=vpc_cidr,
        name=vpc_name or vpc_id,
        region=region,
        subnets=subnets,
        routes=routes,
        nat_gateways=nat_gateways,
        internet_gateways=internet_gateways,
        sgs=sgs,
        route_tables=route_tables,
        subnet_route_tables=subnet_route_tables,
    )

def _aws_perm_to_rules(perm: Dict[str, Any], direction: str) -> List[AwsSgRule]:
    ipranges = [r["CidrIp"] for r in perm.get("IpRanges", []) if "CidrIp" in r]
    proto = perm.get("IpProtocol", "-1")
    from_p = perm.get("FromPort", None)
    to_p = perm.get("ToPort", None)
    return [AwsSgRule(direction, proto, from_p, to_p, ipranges)]

# ---------- Discovery (GCP) — minimal (subnets & routes)

def discover_gcp_vpc(project: str, network: str) -> GcpVpc:
    networks_client = build_compute_client(compute_v1.NetworksClient)
    try:
        networks_client.get(project=project, network=network)
    except gcp_exceptions.NotFound:
        return GcpVpc(name=network, project=project, subnets=[], routes=[], exists=False)
    except gcp_exceptions.GoogleAPICallError as exc:
        raise SystemExit(f"Failed to describe GCP network {network}: {exc}")

    subnetworks_client = build_compute_client(compute_v1.SubnetworksClient)
    subnets: List[Dict[str, Any]] = []
    agg = subnetworks_client.aggregated_list(project=project)
    for region, scoped in agg:
        subnet_list = getattr(scoped, "subnetworks", None)
        if not subnet_list:
            continue
        for subnet in subnet_list:
            if subnet.network and subnet.network.endswith(f"/{network}"):
                subnets.append({
                    "name": subnet.name,
                    "region": subnet.region.split("/")[-1] if subnet.region else region,
                    "ipCidrRange": subnet.ip_cidr_range,
                })

    routes_client = build_compute_client(compute_v1.RoutesClient)
    routes: List[Dict[str, Any]] = []
    for route in routes_client.list(project=project):
        if route.network and route.network.endswith(f"/{network}"):
            routes.append({
                "name": route.name,
                "destRange": route.dest_range,
                "nextHop": route.next_hop_gateway or route.next_hop_ip or route.next_hop_ilb or route.next_hop_vpn_tunnel or "",
            })

    return GcpVpc(name=network, project=project, subnets=subnets, routes=routes, exists=True)

# ---------- Mapping & Validation

def cidr_overlap(c1: str, c2: str) -> bool:
    return ipaddress.ip_network(c1).overlaps(ipaddress.ip_network(c2))

def extract_json_candidate(text: str) -> Optional[Any]:
    if not text:
        return None
    snippet = text.strip()
    fenced = re.match(r"```(?:json)?\s*(.*?)\s*```$", snippet, re.DOTALL)
    if fenced:
        snippet = fenced.group(1).strip()
    if not snippet:
        return None
    candidates = [snippet]
    # Try to isolate JSON object or array inside surrounding commentary
    for opener, closer in (("{", "}"), ("[", "]")):
        start = snippet.find(opener)
        end = snippet.rfind(closer)
        if start != -1 and end != -1 and end > start:
            candidates.append(snippet[start:end + 1].strip())

    for candidate in candidates:
        if not candidate:
            continue
        try:
            return json.loads(candidate)
        except json.JSONDecodeError:
            continue
    return None


def build_terraform_generation_payload(aws_vpc: AwsVpc, gcp_project: str, gcp_network: str,
                                       gcp_region_fallback: str,
                                       subnet_cidr_overrides: Optional[Dict[str, str]] = None,
                                       subnet_name_overrides: Optional[Dict[str, str]] = None) -> Dict[str, Any]:
    created_subnet_names: Set[str] = set()
    subnet_mappings: List[Dict[str, Any]] = []
    for subnet in aws_vpc.subnets:
        gcp_region = az_to_gcp_region(subnet.az, gcp_region_fallback)
        override_name = (subnet_name_overrides or {}).get(subnet.id)
        target_name = sanitize_name(override_name or subnet.name or subnet.id)
        if target_name in created_subnet_names:
            suffix = len(created_subnet_names) + 1
            target_name = f"{target_name}-{suffix}"
        created_subnet_names.add(target_name)
        target_cidr = subnet_cidr_overrides.get(subnet.id, subnet.cidr) if subnet_cidr_overrides else subnet.cidr
        subnet_mappings.append({
            "aws_subnet_id": subnet.id,
            "aws_name": subnet.name,
            "aws_az": subnet.az,
            "source_cidr": subnet.cidr,
            "gcp_region": gcp_region,
            "gcp_subnet_name": target_name,
            "target_cidr": target_cidr,
        })

    sg_details: List[Dict[str, Any]] = []
    for sg_name, rules in aws_vpc.sgs.items():
        rule_specs = []
        for rule in rules:
            rule_specs.append({
                "direction": rule.direction,
                "protocol": rule.protocol,
                "from_port": rule.port_from,
                "to_port": rule.port_to,
                "cidrs": list(rule.cidrs or []),
            })
        sg_details.append({
            "name": sg_name or "unnamed",
            "rule_count": len(rules),
            "rules": rule_specs,
        })

    nat_info = []
    for nat in aws_vpc.nat_gateways:
        nat_info.append({
            "id": nat.id,
            "subnet_id": nat.subnet_id,
            "state": nat.state,
            "allocation_ids": list(nat.allocation_ids or []),
        })

    route_summaries = []
    for route in aws_vpc.routes:
        dest = route.get("DestinationCidrBlock") or route.get("DestinationPrefixListId")
        if not dest:
            continue
        next_hop = route.get("NatGatewayId") or route.get("GatewayId") or route.get("TransitGatewayId") or \
            route.get("VpcPeeringConnectionId") or route.get("NetworkInterfaceId") or route.get("InstanceId") or ""
        route_summaries.append({
            "destination": dest,
            "target": next_hop,
            "state": route.get("State", "unknown"),
        })

    internet_gateways = []
    for igw in aws_vpc.internet_gateways:
        internet_gateways.append({
            "id": igw.id,
            "attachment_states": igw.attachment_states,
        })

    payload = {
        "gcp": {
            "project": gcp_project,
            "network": gcp_network,
        },
        "aws": {
            "vpc_id": aws_vpc.id,
            "vpc_name": aws_vpc.name,
            "cidr": aws_vpc.cidr,
            "region": aws_vpc.region,
            "subnets": subnet_mappings,
            "security_groups": sg_details,
            "nat_gateways": nat_info,
            "routes": route_summaries,
            "internet_gateways": internet_gateways,
        },
    }
    return payload


def normalize_gemini_bundle_structure(raw: Any) -> Any:
    if isinstance(raw, dict):
        # Flatten common wrappers
        for key in ("files", "artifacts", "outputs", "data"):
            value = raw.get(key)
            if isinstance(value, dict):
                return normalize_gemini_bundle_structure(value)
        flattened: Dict[str, Any] = {}
        for k, v in raw.items():
            if isinstance(v, dict) and {"file", "content"}.issubset(set(v.keys())):
                flattened[str(v["file"])] = v.get("content")
            elif isinstance(v, dict) and "text" in v and len(v) == 1:
                flattened[str(k)] = v["text"]
            else:
                flattened[str(k)] = v
        return flattened

    if isinstance(raw, list):
        files: Dict[str, str] = {}
        for entry in raw:
            if not isinstance(entry, dict):
                continue
            name = entry.get("file") or entry.get("filename") or entry.get("name") or entry.get("path")
            content = entry.get("content") or entry.get("body") or entry.get("text") or entry.get("value")
            if name and isinstance(content, str):
                files[str(name)] = content
        if files:
            return files
    return raw


def format_hcl_value(value: Any, indent: int = 0) -> str:
    spacer = " " * indent
    if isinstance(value, str):
        return json.dumps(value)
    if isinstance(value, bool):
        return "true" if value else "false"
    if value is None:
        return "null"
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, list):
        if not value:
            return "[]"
        items = [" " * (indent + 2) + format_hcl_value(item, indent + 2) for item in value]
        return "[\n" + "\n".join(items) + "\n" + spacer + "]"
    if isinstance(value, dict):
        if not value:
            return "{}"
        lines = []
        for key in sorted(value.keys()):
            if re.match(r"^[A-Za-z_][A-Za-z0-9_]*$", key):
                key_repr = key
            else:
                key_repr = json.dumps(key)
            val_repr = format_hcl_value(value[key], indent + 2)
            lines.append(" " * (indent + 2) + f"{key_repr} = {val_repr}")
        return "{\n" + "\n".join(lines) + "\n" + spacer + "}"
    raise TypeError(f"Unsupported value type for HCL formatting: {type(value)!r}")


def generate_local_terraform_bundle(
    aws_vpc: AwsVpc,
    gcp_project: str,
    gcp_network: str,
    gcp_region_fallback: str,
    subnet_cidr_overrides: Optional[Dict[str, str]] = None,
    subnet_name_overrides: Optional[Dict[str, str]] = None,
    prepared_payload: Optional[Dict[str, Any]] = None,
    analysis: Optional[Dict[str, Any]] = None,
) -> Dict[str, str]:
    payload = prepared_payload or build_terraform_generation_payload(
        aws_vpc,
        gcp_project,
        gcp_network,
        gcp_region_fallback,
        subnet_cidr_overrides=subnet_cidr_overrides,
        subnet_name_overrides=subnet_name_overrides,
    )
    analysis = analysis or analyze_network_topology(aws_vpc, payload, gcp_network, gcp_region_fallback)

    network_exists = False
    if compute_v1 is not None and gcp_exceptions is not None:
        try:
            gcp_state = discover_gcp_vpc(gcp_project, gcp_network)
            network_exists = getattr(gcp_state, "exists", False)
        except SystemExit:
            network_exists = False
        except RuntimeError:
            network_exists = False
        except Exception:
            network_exists = False
    subnet_egress = analysis.get("subnet_egress", {})
    subnet_entries = []
    for item in analysis["subnet_entries"]:
        aws_subnet_id = item["aws_subnet_id"]
        egress_type = subnet_egress.get(aws_subnet_id, {}).get("type")
        is_public = egress_type == "igw"
        subnet_entries.append({
            "name": item["gcp_subnet_name"],
            "region": item["gcp_region"],
            "cidr": item["target_cidr"],
            "is_public": is_public,
        })

    nat_configs = analysis.get("nat_configs", [])

    variable_sections = [
        textwrap.dedent(
            """
            variable "project_id" {
              description = "Target GCP project ID"
              type        = string
            }

            variable "network_name" {
              description = "Name of the VPC network to create"
              type        = string
            }

            variable "default_region" {
              description = "Default GCP region for regional resources"
              type        = string
            }

            variable "create_network" {
              description = "Set to false when the VPC network already exists"
              type        = bool
              default     = true
            }

            variable "subnets" {
              description = "Subnets to create in the VPC"
              type = list(object({
                name      = string
                region    = string
                cidr      = string
                is_public = bool
              }))
            }
            """
        ).strip()
    ]

    variables_tf = "\n\n".join(variable_sections) + "\n"

    main_sections: List[str] = []

    main_sections.append(textwrap.dedent(
        """
        terraform {
          required_version = ">= 1.5.0"

          required_providers {
            google = {
              source  = "hashicorp/google"
              version = "~> 5.0"
            }
          }
        }
        """
    ).strip())

    main_sections.append(textwrap.dedent(
        """
        provider "google" {
          project = var.project_id
          region  = var.default_region
        }
        """
    ).strip())

    main_sections.append(textwrap.dedent(
        """
        resource "google_compute_network" "this" {
          count                   = var.create_network ? 1 : 0
          name                    = var.network_name
          auto_create_subnetworks = false
        }
        """
    ).strip())

    main_sections.append(textwrap.dedent(
        """
        data "google_compute_network" "existing" {
          count   = var.create_network ? 0 : 1
          name    = var.network_name
          project = var.project_id
        }
        """
    ).strip())
    main_sections.append(textwrap.dedent(
        """
        locals {
          network_self_link = var.create_network ? google_compute_network.this[0].self_link : data.google_compute_network.existing[0].self_link
          network_name      = var.create_network ? google_compute_network.this[0].name : data.google_compute_network.existing[0].name
        }
        """
    ).strip())

    main_sections.append(textwrap.dedent(
        """
        resource "google_compute_subnetwork" "this" {
          for_each      = { for subnet in var.subnets : subnet.name => subnet }
          name          = each.value.name
          ip_cidr_range = each.value.cidr
          region        = each.value.region
          network       = local.network_self_link
          stack_type    = "IPV4_ONLY"
        }
        """
    ).strip())

    subnet_egress = analysis.get("subnet_egress", {})

    for cfg in nat_configs:
        router_tf = cfg["router_tf_name"]
        router_name = cfg["router_name"]
        region = cfg["region"]
        main_sections.append(textwrap.dedent(
            f"""
            resource "google_compute_router" "{router_tf}" {{
              name    = "{router_name}"
              network = local.network_name
              region  = "{region}"
            }}
            """
        ).strip())
        for ip_name, ip_tf in zip(cfg["ip_names"], cfg["ip_tf_names"]):
            main_sections.append(textwrap.dedent(
                f"""
                resource "google_compute_address" "{ip_tf}" {{
                  name   = "{ip_name}"
                  region = "{region}"
                }}
                """
            ).strip())
        nat_tf = cfg["nat_tf_name"]
        nat_name = cfg["nat_name"]
        nat_ip_refs = ", ".join(f"google_compute_address.{ip_tf}.self_link" for ip_tf in cfg["ip_tf_names"])
        nat_block = textwrap.dedent(
            f"""
            resource "google_compute_router_nat" "{nat_tf}" {{
              name                               = "{nat_name}"
              router                             = google_compute_router.{router_tf}.name
              region                             = "{region}"
              nat_ip_allocate_option             = "MANUAL_ONLY"
              nat_ips                            = [{nat_ip_refs}]
              source_subnetwork_ip_ranges_to_nat = "LIST_OF_SUBNETWORKS"

              dynamic "subnetwork" {{
                for_each = [for s in var.subnets : s if !s.is_public && s.region == "{region}"]
                content {{
                  name                    = google_compute_subnetwork.this[subnetwork.value.name].self_link
                  source_ip_ranges_to_nat = ["ALL_IP_RANGES"]
                }}
              }}
            }}
            """
        ).strip()
        main_sections.append(nat_block)

    main_tf = "\n\n".join(main_sections) + "\n"

    subnet_lines = ["["]
    for entry in subnet_entries:
        subnet_lines.append("  {")
        subnet_lines.append(f"    name   = {json.dumps(entry['name'])}")
        subnet_lines.append(f"    region = {json.dumps(entry['region'])}")
        subnet_lines.append(f"    cidr   = {json.dumps(entry['cidr'])}")
        subnet_lines.append(f"    is_public = {json.dumps(entry['is_public']).lower()}")
        subnet_lines.append("  },")
    if len(subnet_lines) > 1:
        subnet_lines[-1] = subnet_lines[-1].rstrip(',')
    subnet_lines.append("]")
    subnets_formatted = "\n".join(subnet_lines)

    create_network_default = not network_exists
    create_network_literal = "true" if create_network_default else "false"

    tfvars_lines = [
        f"project_id     = {json.dumps(gcp_project)}",
        f"network_name   = {json.dumps(gcp_network)}",
        f"default_region = {json.dumps(gcp_region_fallback)}",
        f"create_network = {create_network_literal}",
        f"subnets        = {subnets_formatted}",
    ]
    terraform_tfvars = "\n".join(tfvars_lines) + "\n"

    return {
        "main.tf": main_tf,
        "variables.tf": variables_tf,
        "terraform.tfvars": terraform_tfvars,
    }

def validate_terraform_bundle_structure(bundle: Dict[str, str]) -> List[str]:
    errors: List[str] = []
    messages: List[str] = []
    main_body = bundle.get("main.tf", "")
    variables_body = bundle.get("variables.tf", "")
    tfvars_body = bundle.get("terraform.tfvars", "")

    for name, content in (("main.tf", main_body), ("variables.tf", variables_body), ("terraform.tfvars", tfvars_body)):
        if not content or not content.strip():
            errors.append(f"{name} is empty.")
        else:
            messages.append(f"{name} populated ({len(content.strip().splitlines())} lines)")

    if main_body and "provider \"google\"" not in main_body:
        errors.append("main.tf missing google provider block.")
    elif main_body:
        messages.append("main.tf includes google provider block")
    if main_body and "resource \"google_compute_network\"" not in main_body:
        errors.append("main.tf missing google_compute_network resource.")
    elif main_body:
        messages.append("main.tf defines google_compute_network resource")
    if main_body and "var.create_network" not in main_body:
        errors.append("main.tf missing create_network conditional handling.")
    elif main_body:
        messages.append("main.tf references var.create_network")

    for attr in ("source_ranges", "destination_ranges", "source_tags", "target_tags", "target_service_accounts"):
        dynamic_attr = f"dynamic \"{attr}\""
        if dynamic_attr in (main_body or ""):
            errors.append(f"main.tf uses unsupported Terraform dynamic block {dynamic_attr}.")

    if variables_body and "variable \"project_id\"" not in variables_body:
        errors.append("variables.tf missing variable 'project_id'.")
    elif variables_body:
        messages.append("variables.tf declares variable 'project_id'")
    if variables_body and "variable \"network_name\"" not in variables_body:
        errors.append("variables.tf missing variable 'network_name'.")
    elif variables_body:
        messages.append("variables.tf declares variable 'network_name'")
    if variables_body and "variable \"create_network\"" not in variables_body:
        errors.append("variables.tf missing variable 'create_network'.")
    elif variables_body:
        messages.append("variables.tf declares variable 'create_network'")

    if tfvars_body and "project_id" not in tfvars_body:
        errors.append("terraform.tfvars missing project_id assignment.")
    elif tfvars_body:
        messages.append("terraform.tfvars sets project_id")
    if tfvars_body and "network_name" not in tfvars_body:
        errors.append("terraform.tfvars missing network_name assignment.")
    elif tfvars_body:
        messages.append("terraform.tfvars sets network_name")
    if tfvars_body and "create_network" not in tfvars_body:
        errors.append("terraform.tfvars missing create_network assignment.")
    elif tfvars_body:
        messages.append("terraform.tfvars sets create_network")

    if main_body and re.search(r'resource\s+"google_compute_address"[\s\S]*?purpose\s*=', main_body):
        errors.append("google_compute_address resources must not set purpose for external addresses.")

    if errors:
        raise RuntimeError("Terraform content validation failed: " + " ".join(errors))

    return messages


def write_terraform_files(bundle: Dict[str, str], output_dir: str, overwrite: bool = False) -> None:
    os.makedirs(output_dir, exist_ok=True)
    for filename, content in bundle.items():
        path = os.path.join(output_dir, filename)
        if os.path.exists(path) and not overwrite:
            raise RuntimeError(f"File already exists (use --overwrite to replace): {path}")
        with open(path, "w", encoding="utf-8") as fh:
            if content.endswith("\n"):
                fh.write(content)
            else:
                fh.write(content + "\n")


def generate_terraform_from_aws_vpc(aws_vpc: AwsVpc, gcp_project: str, gcp_network: str,
                                     gcp_region_fallback: str, output_root: str, overwrite: bool = False,
                                     subnet_cidr_overrides: Optional[Dict[str, str]] = None,
                                     subnet_name_overrides: Optional[Dict[str, str]] = None,
                                     preview_architecture: bool = False) -> Tuple[str, List[str], List[str]]:
    folder_name = sanitize_name(aws_vpc.name or aws_vpc.id)
    normalized_root = resolve_workspace_path(output_root or "terraform")
    target_dir = os.path.join(normalized_root, folder_name)

    payload = build_terraform_generation_payload(
        aws_vpc,
        gcp_project,
        gcp_network,
        gcp_region_fallback,
        subnet_cidr_overrides=subnet_cidr_overrides,
        subnet_name_overrides=subnet_name_overrides,
    )
    analysis = analyze_network_topology(aws_vpc, payload, gcp_network, gcp_region_fallback)
    architecture_table = analysis.get("architecture_table", "")

    if architecture_table:
        print("\nPlanned architecture (AWS → GCP mapping):\n")
        print(architecture_table)
        print()
    if preview_architecture:
        if not prompt_yes_no("Proceed with Terraform generation?", default=True):
            raise RuntimeError("Terraform generation cancelled by user.")

    print("ℹ️ Generating Terraform bundle using deterministic logic.")
    bundle = generate_local_terraform_bundle(
        aws_vpc,
        gcp_project,
        gcp_network,
        gcp_region_fallback,
        subnet_cidr_overrides=subnet_cidr_overrides,
        subnet_name_overrides=subnet_name_overrides,
        prepared_payload=payload,
        analysis=analysis,
    )
    write_terraform_files(bundle, target_dir, overwrite=overwrite)
    graph_path = write_graphviz_artifacts(analysis.get("architecture_graphviz"), target_dir)
    if graph_path:
        print(f"Graphviz topology diagram written to {graph_path}")
    structure_messages = validate_terraform_bundle_structure(bundle)
    validation_messages = terraform_cli_validate(target_dir)
    return target_dir, structure_messages, validation_messages


def terraform_cli_validate(directory: str) -> List[str]:
    terraform_bin = shutil.which("terraform")
    if not terraform_bin:
        raise RuntimeError("Terraform CLI not found in PATH. Install Terraform or update PATH to enable validation.")

    directory = os.path.abspath(directory)
    env = os.environ.copy()
    env.setdefault("TF_IN_AUTOMATION", "1")

    commands = [
        ([terraform_bin, "init", "-backend=false", "-input=false", "-no-color"], "terraform init"),
        ([terraform_bin, "validate", "-no-color"], "terraform validate"),
    ]
    messages: List[str] = []
    try:
        for cmd, label in commands:
            try:
                proc = subprocess.run(cmd, check=True, capture_output=True, text=True, env=env, cwd=directory)
            except subprocess.CalledProcessError as exc:
                output = "\n".join(filter(None, [exc.stdout, exc.stderr]))
                raise RuntimeError(f"{label} failed:\n{output.strip() or '(no output)'}") from exc
            else:
                summary = proc.stdout.strip() or proc.stderr.strip()
                messages.append(f"{label} succeeded" + (f": {summary}" if summary else "."))
        return messages
    finally:
        cleanup_terraform_cache(directory)


def cleanup_terraform_cache(directory: str) -> None:
    cache_dir = os.path.join(directory, ".terraform")
    lock_file = os.path.join(directory, ".terraform.lock.hcl")
    if os.path.isdir(cache_dir):
        shutil.rmtree(cache_dir, ignore_errors=True)
    if os.path.exists(lock_file):
        try:
            os.remove(lock_file)
        except OSError:
            pass

# ---------- Generators

def generate_gcp_vpc(project: str, network: str, auto_subnet=False, apply=False):
    mode = "auto" if auto_subnet else "custom"
    if not apply:
        print_api("networks.create", project=project, network=network, subnet_mode=mode)
        return
    networks_client = build_compute_client(compute_v1.NetworksClient)
    network_resource = compute_v1.Network()
    network_resource.name = network
    network_resource.auto_create_subnetworks = auto_subnet
    operation = networks_client.insert(project=project, network_resource=network_resource)
    wait_for_operation(project, operation)

def generate_gcp_subnet(project: str, network: str, region: str, name: str, cidr: str, apply=False):
    if not apply:
        print_api("subnetworks.create", project=project, region=region, name=name, cidr=cidr)
        return
    subnet_client = build_compute_client(compute_v1.SubnetworksClient)
    subnet_resource = compute_v1.Subnetwork()
    subnet_resource.name = name
    subnet_resource.ip_cidr_range = cidr
    subnet_resource.network = f"projects/{project}/global/networks/{network}"
    operation = subnet_client.insert(project=project, region=region, subnetwork_resource=subnet_resource)
    wait_for_operation(project, operation, region=region)

def generate_gcp_route(project: str, network: str, name: str, dest_cidr: str, next_hop: str, apply=False):
    if not apply:
        print_api("routes.create", project=project, name=name, dest=dest_cidr, next_hop=next_hop)
        return
    routes_client = build_compute_client(compute_v1.RoutesClient)
    route = compute_v1.Route()
    route.name = name
    route.dest_range = dest_cidr
    route.network = f"projects/{project}/global/networks/{network}"
    route.priority = 1000
    if next_hop == "default-internet-gateway":
        route.next_hop_gateway = f"projects/{project}/global/gateways/default-internet-gateway"
    else:
        route.next_hop_gateway = next_hop
    operation = routes_client.insert(project=project, route_resource=route)
    wait_for_operation(project, operation)

def generate_gcp_address(project: str, region: str, name: str, apply: bool = False):
    if not apply:
        print_api("addresses.create", project=project, region=region, name=name)
        return
    addresses_client = build_compute_client(compute_v1.AddressesClient)
    address = compute_v1.Address()
    address.name = name
    address.address_type = "EXTERNAL"
    address.network_tier = "PREMIUM"
    operation = addresses_client.insert(project=project, region=region, address_resource=address)
    wait_for_operation(project, operation, region=region)


def generate_gcp_router(project: str, region: str, network: str, name: str, apply: bool = False):
    if not apply:
        print_api("routers.create", project=project, region=region, name=name)
        return
    routers_client = build_compute_client(compute_v1.RoutersClient)
    router = compute_v1.Router()
    router.name = name
    router.network = f"projects/{project}/global/networks/{network}"
    operation = routers_client.insert(project=project, region=region, router_resource=router)
    wait_for_operation(project, operation, region=region)


def generate_gcp_nat(project: str, region: str, router_name: str, nat_name: str, nat_ips: List[str],
                     subnet_self_links: Optional[List[str]] = None, apply: bool = False):
    subnet_desc = ",".join(subnet_self_links or [])
    if not apply:
        extra = {"subnets": subnet_desc} if subnet_self_links else {}
        print_api("routerNats.create", project=project, region=region, router=router_name,
                   name=nat_name, ips=",".join(nat_ips), **extra)
        return
    routers_client = build_compute_client(compute_v1.RoutersClient)
    try:
        existing_router = routers_client.get(project=project, region=region, router=router_name)
    except gcp_exceptions.NotFound as exc:
        raise RuntimeError(f"Router {router_name} not found for NAT creation: {exc}")

    if any(nat.name == nat_name for nat in existing_router.nats):
        print(f"   ℹ️ Cloud NAT {nat_name} already attached to router {router_name}.")
        return

    nat = compute_v1.RouterNat()
    nat.name = nat_name
    nat.nat_ips = nat_ips
    nat.nat_ip_allocate_option = compute_v1.RouterNat.NatIpAllocateOption.MANUAL_ONLY.name
    if subnet_self_links:
        nat.source_subnetwork_ip_ranges_to_nat = compute_v1.RouterNat.SourceSubnetworkIpRangesToNat.LIST_OF_SUBNETWORKS.name
        for subnet in subnet_self_links:
            sub_to_nat = compute_v1.RouterNatSubnetworkToNat()
            sub_to_nat.name = subnet
            sub_to_nat.source_ip_ranges_to_nat.append(
                compute_v1.RouterNatSubnetworkToNat.SourceIpRangesToNat.ALL_IP_RANGES.name
            )
            nat.subnetworks.append(sub_to_nat)
    else:
        nat.source_subnetwork_ip_ranges_to_nat = compute_v1.RouterNat.SourceSubnetworkIpRangesToNat.ALL_SUBNETWORKS_ALL_IP_RANGES.name
    nat.enable_endpoint_independent_mapping = True

    router_patch = compute_v1.Router()
    router_patch.name = router_name
    router_patch.network = existing_router.network
    router_patch.nats.extend(existing_router.nats)
    router_patch.nats.append(nat)

    operation = routers_client.patch(project=project, region=region, router=router_name, router_resource=router_patch)
    wait_for_operation(project, operation, region=region)


def generate_gcp_peering(project: str, network: str, peer_name: str, peer_project: str, peer_network: str, apply=False):
    if not apply:
        print_api("peerings.create", project=project, network=network, peer=peer_name, peer_project=peer_project, peer_network=peer_network)
        return
    peerings_client = build_compute_client(compute_v1.NetworksClient)
    peering = compute_v1.NetworkPeering()
    peering.name = peer_name
    peering.peer_project = peer_project
    peering.peer_network = peer_network
    peering.export_custom_routes = True
    peering.import_custom_routes = True
    peering.export_subnet_routes_with_public_ip = True
    peering.import_subnet_routes_with_public_ip = True
    request = compute_v1.AddPeeringNetworkRequest(
        project=project,
        network=network,
        networks_add_peering_request_resource=compute_v1.NetworksAddPeeringRequest(network_peering=peering),
    )
    operation = peerings_client.add_peering(request=request)
    wait_for_operation(project, operation)

# ---------- Plans

def plan_migrate_aws_to_gcp(aws_vpc: AwsVpc, gcp_project: str, gcp_network: str,
                            gcp_region_fallback: str, apply=False, interactive=False,
                            subnet_name_overrides: Optional[Dict[str, str]] = None):
    print(f"\n=== MIGRATION PLAN: AWS {aws_vpc.id} ({aws_vpc.name}) → GCP {gcp_project}/{gcp_network} ===")

    cidr_overrides = {s.id: s.cidr for s in aws_vpc.subnets}
    if interactive and aws_vpc.subnets:
        cidr_overrides = prompt_subnet_cidrs(aws_vpc.subnets)
    
    # Build subnet plan
    created_subnet_names = set()
    subnet_plans: List[SubnetPlan] = []
    subnet_region_map: Dict[str, str] = {}
    for s in aws_vpc.subnets:
        gcp_region = az_to_gcp_region(s.az, gcp_region_fallback)
        override = (subnet_name_overrides or {}).get(s.id)
        sub_name = sanitize_name(override or s.name or s.id)
        if sub_name in created_subnet_names:
            sub_name = f"{sub_name}-{len(created_subnet_names)}"
        created_subnet_names.add(sub_name)
        target_cidr = cidr_overrides.get(s.id, s.cidr)
        subnet_plans.append(SubnetPlan(source=s, target_name=sub_name, target_region=gcp_region, target_cidr=target_cidr))
        subnet_region_map[s.id] = gcp_region

    summarize_migration_plan(aws_vpc, subnet_plans)

    payload = build_terraform_generation_payload(
        aws_vpc,
        gcp_project,
        gcp_network,
        gcp_region_fallback,
        subnet_cidr_overrides=cidr_overrides,
        subnet_name_overrides=subnet_name_overrides,
    )
    analysis = analyze_network_topology(aws_vpc, payload, gcp_network, gcp_region_fallback)
    nat_configs = analysis.get("nat_configs", [])
    nat_overview: Dict[str, Dict[str, Any]] = {}
    for cfg in nat_configs:
        nat_overview[cfg["region"]] = {
            "router": cfg["router_name"],
            "nat": cfg["nat_name"],
            "ips": cfg["ip_names"],
        }

    print_connectivity_map(aws_vpc, subnet_plans, nat_overview)
    architecture_table = analysis.get("architecture_table", "")
    if architecture_table:
        print("\nPlanned AWS → GCP mapping (Markdown):\n")
        print(architecture_table)
        print()
    architecture_mermaid = analysis.get("architecture_mermaid")
    if architecture_mermaid:
        print("Proposed topology diagram (Mermaid):\n")
        print(architecture_mermaid)
        print()

    effective_apply = apply
    if interactive:
        if prompt_yes_no("Create resources now?", default=False):
            if not apply:
                effective_apply = True
                print("⚙️ Interactive approval granted; executing commands.")
        else:
            print("✋ Skipping resource creation; showing commands in dry-run mode.")
            effective_apply = False
    if effective_apply:
        print("\nExecuting creation commands...")

    creation_errors: List[str] = []
    expected_addresses: List[Tuple[str, str]] = []
    expected_routers: List[Tuple[str, str]] = []
    expected_nats: List[Tuple[str, str, str]] = []

    def handle_creation_exception(label: str, exc: Exception):
        if gcp_exceptions and isinstance(exc, gcp_exceptions.GoogleAPICallError) and getattr(exc, "code", None) == 409:
            print(f"   ℹ️ {label} already exists; continuing.")
        else:
            creation_errors.append(f"{label}: {exc}")

    # 1) Create VPC (custom)
    try:
        generate_gcp_vpc(gcp_project, gcp_network, auto_subnet=False, apply=effective_apply)
    except Exception as exc:  # pragma: no cover - runtime protection
        handle_creation_exception(f"VPC {gcp_network}", exc)

    # 2) Subnets
    for plan in subnet_plans:
        print(f"→ Subnet {plan.target_name}  {plan.target_cidr}  @ {plan.target_region}")
        try:
            generate_gcp_subnet(gcp_project, gcp_network, plan.target_region, plan.target_name, plan.target_cidr, apply=effective_apply)
        except Exception as exc:  # pragma: no cover
            handle_creation_exception(f"Subnet {plan.target_name}", exc)

    # 3) NAT gateways (Cloud NAT equivalent)
    for cfg in nat_configs:
        region = cfg["region"]
        for ip_name in cfg["ip_names"]:
            print(f"→ Reserve external IP {ip_name} @ {region}")
            try:
                generate_gcp_address(gcp_project, region, ip_name, apply=effective_apply)
                expected_addresses.append((region, ip_name))
            except Exception as exc:
                handle_creation_exception(f"Address {ip_name}", exc)
        print(f"→ Router {cfg['router_name']} @ {region}")
        try:
            generate_gcp_router(gcp_project, region, gcp_network, cfg["router_name"], apply=effective_apply)
            expected_routers.append((region, cfg["router_name"]))
        except Exception as exc:
            handle_creation_exception(f"Router {cfg['router_name']}", exc)
        nat_ips = [f"projects/{gcp_project}/regions/{region}/addresses/{ip}" for ip in cfg["ip_names"]]
        nat_subnets = [
            f"projects/{gcp_project}/regions/{region}/subnetworks/{subnet_name}"
            for subnet_name in cfg.get("subnets", [])
        ]
        print(f"→ Cloud NAT {cfg['nat_name']} on router {cfg['router_name']}")
        try:
            generate_gcp_nat(
                gcp_project,
                region,
                cfg["router_name"],
                cfg["nat_name"],
                nat_ips,
                subnet_self_links=nat_subnets,
                apply=effective_apply,
            )
            expected_nats.append((region, cfg["router_name"], cfg["nat_name"]))
        except Exception as exc:
            handle_creation_exception(f"Cloud NAT {cfg['nat_name']}", exc)

    # 4) Routes: Rely on GCP's default route. Explicit route creation is removed.
    print("ℹ️ Relying on GCP's automatically created default internet route.")

    print("\n⚠️ Review notes:")
    print("- NAT, VPNs, TGW/Hub, PrivateLink, VPC endpoints, custom route targets are NOT auto-migrated.")
    print("- Security group intents are listed for awareness but GCP firewall rules must be crafted manually.")
    print("- Consider hierarchical firewall policies / tags for least privilege once rules are defined.")

    if creation_errors:
        print("\n❗ Issues encountered during creation:")
        for err in creation_errors:
            print(f" - {err}")

    if effective_apply:
        verify_gcp_resources(
            gcp_project,
            gcp_network,
            subnet_plans,
            {
                "addresses": expected_addresses,
                "routers": expected_routers,
                "nats": expected_nats,
            }
        )



def verify_gcp_resources(project: str, network: str, subnet_plans: List[SubnetPlan], expected_nat_artifacts: Optional[Dict[str, List[Tuple]]] = None) -> None:
    print("\n=== GCP POST-CREATION VERIFICATION ===")
    try:
        gcp_state = discover_gcp_vpc(project, network)
    except SystemExit as exc:
        print(f"⚠️ Verification skipped: {exc}")
        return

    subnets_by_name = {s["name"]: s for s in gcp_state.subnets}
    for plan in subnet_plans:
        status = "✔" if plan.target_name in subnets_by_name else "✖"
        print(f" {status} subnet {plan.target_name} ({plan.target_cidr})")

    addr_expect = expected_nat_artifacts.get("addresses", []) if expected_nat_artifacts else []
    if addr_expect:
        addresses_client = build_compute_client(compute_v1.AddressesClient)
        for region, addr_name in addr_expect:
            status = "✖"
            try:
                addresses_client.get(project=project, region=region, address=addr_name)
            except gcp_exceptions.NotFound:
                status = "✖"
            except Exception as exc:
                print(f" ⚠️ address {addr_name} ({region}) verification error: {exc}")
                continue
            else:
                status = "✔"
            print(f" {status} address {addr_name} ({region})")

    router_expect = expected_nat_artifacts.get("routers", []) if expected_nat_artifacts else []
    if router_expect:
        routers_client = build_compute_client(compute_v1.RoutersClient)
        for region, router_name in router_expect:
            status = "✖"
            try:
                routers_client.get(project=project, region=region, router=router_name)
            except gcp_exceptions.NotFound:
                status = "✖"
            except Exception as exc:
                print(f" ⚠️ router {router_name} ({region}) verification error: {exc}")
                continue
            else:
                status = "✔"
            print(f" {status} router {router_name} ({region})")

    nat_expect = expected_nat_artifacts.get("nats", []) if expected_nat_artifacts else []
    if nat_expect:
        routers_client = build_compute_client(compute_v1.RoutersClient)
        for region, router_name, nat_name in nat_expect:
            status = "✖"
            try:
                router = routers_client.get(project=project, region=region, router=router_name)
                if any(n.name == nat_name for n in router.nats):
                    status = "✔"
            except gcp_exceptions.NotFound:
                status = "✖"
            except Exception as exc:
                print(f" ⚠️ Cloud NAT {nat_name} ({region}) verification error: {exc}")
                continue
            print(f" {status} cloud-nat {nat_name} ({region})")

# ---------- Utilities

def sanitize_name(s: str) -> str:
    import re
    s = (s or "vpc").lower()
    s = re.sub(r"[^a-z0-9-]", "-", s)
    s = s.strip("-")
    return s[:61] if s else "vpc"


def terraform_identifier(name: str) -> str:
    ident = re.sub(r"[^a-zA-Z0-9_]", "_", name)
    if not ident:
        ident = "resource"
    if ident[0].isdigit():
        ident = f"r_{ident}"
    return ident


def _infer_subnet_privacy(subnet: AwsSubnet) -> str:
    """Classify subnets using explicit names before falling back to AWS flags."""
    name = (subnet.name or "").lower()
    if "public" in name:
        return "public"
    if "private" in name:
        return "private"
    return "public" if subnet.map_public_ip_on_launch else "private"


def _default_route_target(routes: List[Dict[str, Any]]) -> Tuple[str, str]:
    for route in routes or []:
        dest = route.get("DestinationCidrBlock") or route.get("DestinationIpv6CidrBlock")
        if dest not in {"0.0.0.0/0", "::/0"}:
            continue
        if route.get("NatGatewayId"):
            return "nat", route["NatGatewayId"]
        gateway_id = route.get("GatewayId", "")
        if gateway_id.startswith("igw-"):
            return "igw", gateway_id
        if route.get("TransitGatewayId"):
            return "transit", route["TransitGatewayId"]
        if route.get("VpcPeeringConnectionId"):
            return "peering", route["VpcPeeringConnectionId"]
        if route.get("InstanceId"):
            return "instance", route["InstanceId"]
        if route.get("NetworkInterfaceId"):
            return "eni", route["NetworkInterfaceId"]
        return "other", gateway_id or "unknown"
    return "none", ""


def determine_subnet_egress(aws_vpc: AwsVpc) -> Dict[str, Dict[str, str]]:
    result: Dict[str, Dict[str, str]] = {}
    fallback_routes = aws_vpc.routes
    if not fallback_routes and aws_vpc.route_tables:
        fallback_routes = next(iter(aws_vpc.route_tables.values()))
    subnet_lookup = {subnet.id: subnet for subnet in aws_vpc.subnets}
    nat_ids: List[str] = []
    nat_ids_by_az: Dict[str, List[str]] = {}
    for nat in aws_vpc.nat_gateways:
        if nat.state and nat.state.lower() not in {"available", "pending"}:
            continue
        nat_ids.append(nat.id)
        host_subnet = subnet_lookup.get(nat.subnet_id)
        if host_subnet and host_subnet.az:
            nat_ids_by_az.setdefault(host_subnet.az, []).append(nat.id)
    for subnet in aws_vpc.subnets:
        routes = aws_vpc.subnet_route_tables.get(subnet.id) or fallback_routes
        r_type, target = _default_route_target(routes or [])
        privacy = _infer_subnet_privacy(subnet)
        force_nat = (privacy == "private") and (r_type in {"igw", "none"} or not target)
        if r_type != "nat" and force_nat and nat_ids:
            preferred = nat_ids_by_az.get(subnet.az)
            nat_choice = (preferred or nat_ids)[0]
            r_type = "nat"
            target = nat_choice
        result[subnet.id] = {"type": r_type, "target": target}
    return result


def build_nat_configs(aws_vpc: AwsVpc, subnet_mapping_by_id: Dict[str, Dict[str, Any]],
                      subnet_egress: Dict[str, Dict[str, str]], gcp_network: str,
                      gcp_region_fallback: str) -> List[Dict[str, Any]]:
    nat_configs: List[Dict[str, Any]] = []
    subnets_by_nat: Dict[str, List[str]] = {}
    for subnet_id, info in subnet_egress.items():
        if info.get("type") != "nat":
            continue
        target = info.get("target")
        if not target:
            continue
        subnets_by_nat.setdefault(target, []).append(subnet_id)

    gcp_region_by_subnet = {sid: mapping["gcp_region"] for sid, mapping in subnet_mapping_by_id.items()}
    gcp_subnet_name_by_subnet = {sid: mapping["gcp_subnet_name"] for sid, mapping in subnet_mapping_by_id.items()}

    router_counts: Dict[str, int] = {}
    nat_counts: Dict[str, int] = {}

    for nat in aws_vpc.nat_gateways:
        if nat.state and nat.state.lower() not in {"available", "pending"}:
            continue
        associated_subnets = subnets_by_nat.get(nat.id, [])
        if not associated_subnets:
            continue
        # Regional NATs may not have subnet_id; infer region from attached subnets first.
        region = None
        for subnet_id in associated_subnets:
            region = gcp_region_by_subnet.get(subnet_id)
            if region:
                break
        if not region and nat.subnet_id:
            region = gcp_region_by_subnet.get(nat.subnet_id)
        if not region:
            region = gcp_region_fallback
        router_counts[region] = router_counts.get(region, 0) + 1
        nat_counts[region] = nat_counts.get(region, 0) + 1
        router_name = sanitize_name(f"router-{gcp_network}-{region}-{router_counts[region]}")
        nat_name = sanitize_name(f"cloud-nat-{gcp_network}-{region}-{nat_counts[region]}")
        ip_names: List[str] = []
        allocations = nat.allocation_ids or []
        if allocations:
            for idx, _ in enumerate(allocations, start=1):
                ip_names.append(sanitize_name(f"nat-ip-{gcp_network}-{region}-{idx}"))
        if not ip_names:
            ip_names.append(sanitize_name(f"nat-ip-{gcp_network}-{region}-1"))
        nat_configs.append({
            "aws_nat_id": nat.id,
            "region": region,
            "router_name": router_name,
            "router_tf_name": terraform_identifier(router_name),
            "nat_name": nat_name,
            "nat_tf_name": terraform_identifier(nat_name),
            "ip_names": ip_names,
            "ip_tf_names": [terraform_identifier(name) for name in ip_names],
            "subnets": [gcp_subnet_name_by_subnet[sid] for sid in associated_subnets if sid in gcp_subnet_name_by_subnet],
        })

    return nat_configs


def render_architecture_table(aws_vpc: AwsVpc, subnet_mapping_by_id: Dict[str, Dict[str, Any]],
                              subnet_egress: Dict[str, Dict[str, str]], nat_configs: List[Dict[str, Any]]) -> str:
    header = [
        "| Subnet | AWS CIDR | GCP Subnet | Region | Egress | Target |",
        "| --- | --- | --- | --- | --- | --- |",
    ]
    nat_lookup = {cfg["aws_nat_id"]: cfg for cfg in nat_configs}
    rows: List[str] = []
    for subnet_id, mapping in sorted(subnet_mapping_by_id.items(), key=lambda item: item[1]["gcp_subnet_name"]):
        aws_label = mapping.get("aws_name") or subnet_id
        gcp_subnet = mapping.get("gcp_subnet_name")
        region = mapping.get("gcp_region")
        cidr = mapping.get("source_cidr")
        egress_info = subnet_egress.get(subnet_id, {"type": "unknown", "target": ""})
        egress_type = egress_info.get("type", "unknown")
        target = egress_info.get("target", "")
        if egress_type == "nat":
            cfg = nat_lookup.get(target)
            target_display = cfg["nat_name"] if cfg else target or "Cloud NAT"
            egress_label = "Cloud NAT"
        elif egress_type == "igw":
            target_display = target or ",".join(igw.id for igw in aws_vpc.internet_gateways) or "IGW"
            egress_label = "Internet Gateway"
        elif egress_type == "none":
            target_display = "—"
            egress_label = "No default route"
        else:
            target_display = target or "—"
            egress_label = egress_type.upper()
        rows.append(f"| {aws_label} | {cidr} | {gcp_subnet} | {region} | {egress_label} | {target_display} |")
    return "\n".join(header + rows)


def render_architecture_mermaid(aws_vpc: AwsVpc, analysis: Dict[str, Any], gcp_network: str) -> str:
    subnet_mapping = analysis.get("subnet_mapping_by_id", {})
    subnet_egress = analysis.get("subnet_egress", {})
    nat_configs = analysis.get("nat_configs", [])
    nat_lookup = {cfg["aws_nat_id"]: cfg for cfg in nat_configs}

    lines: List[str] = ["```mermaid", "flowchart LR"]
    lines.append(f"  subgraph aws_vpc [\"AWS VPC {aws_vpc.name or aws_vpc.id}\"]")
    for subnet in aws_vpc.subnets:
        label = subnet.name or subnet.id
        lines.append(f"    aws_{terraform_identifier(subnet.id)}[\"{label}\\n{subnet.cidr}\"]")
    lines.append("  end")

    lines.append(f"  subgraph gcp_vpc [\"GCP VPC {gcp_network}\"]")
    regions: Dict[str, List[str]] = {}
    for mapping in subnet_mapping.values():
        regions.setdefault(mapping["gcp_region"], []).append(mapping["gcp_subnet_name"])
    for region, subnets in regions.items():
        lines.append(f"    subgraph region_{terraform_identifier(region)} [\"Region {region}\"]")
        for sub_name in subnets:
            lines.append(f"      gcp_{terraform_identifier(sub_name)}[\"{sub_name}\"]")
        lines.append("    end")
    
    for cfg in nat_configs:
        router_node = f"router_{terraform_identifier(cfg['router_name'])}"
        nat_node = f"nat_{terraform_identifier(cfg['nat_name'])}"
        lines.append(f"    subgraph nat_cluster_{terraform_identifier(cfg['region'])} [\"NAT in {cfg['region']}\"]")
        lines.append(f"      {router_node}[\"Router {cfg['router_name']}\"]")
        lines.append(f"      {nat_node}[\"Cloud NAT {cfg['nat_name']}\"]")
        lines.append(f"      {router_node} -- NAT --> {nat_node}")
        lines.append("    end")
    lines.append("  end")

    for subnet in aws_vpc.subnets:
        mapping = subnet_mapping.get(subnet.id)
        if not mapping:
            continue
        aws_node = f"aws_{terraform_identifier(subnet.id)}"
        gcp_node = f"gcp_{terraform_identifier(mapping['gcp_subnet_name'])}"
        lines.append(f"  {aws_node} -->|migrate| {gcp_node}")
        egress = subnet_egress.get(subnet.id, {})
        if egress.get("type") == "nat":
            cfg = nat_lookup.get(egress.get("target"))
            if cfg:
                router_node = f"router_{terraform_identifier(cfg['router_name'])}"
                lines.append(f"  {gcp_node} -->|egress via| {router_node}")
        elif egress.get("type") == "igw":
            lines.append(f"  {gcp_node} -->|egress| igw_default")

    if any(info.get("type") == "igw" for info in subnet_egress.values()):
        lines.append("  igw_default[\"Default Internet Gateway\"]")
    
    for cfg in nat_configs:
        nat_node = f"nat_{terraform_identifier(cfg['nat_name'])}"
        lines.append(f"  {nat_node} -->|outbound| igw_default")

    lines.append("```")
    return "\n".join(lines)


def render_architecture_graphviz(analysis: Dict[str, Any], gcp_network: str) -> str:
    subnet_mapping = analysis.get("subnet_mapping_by_id", {})
    subnet_egress = analysis.get("subnet_egress", {})
    nat_configs = analysis.get("nat_configs", [])
    nat_lookup = {cfg["aws_nat_id"]: cfg for cfg in nat_configs}

    lines: List[str] = ["digraph gcp_vpc {", "  rankdir=LR;", "  node [shape=box, style=rounded];"]
    region_groups: Dict[str, List[str]] = {}
    for sid, mapping in subnet_mapping.items():
        region_groups.setdefault(mapping["gcp_region"], []).append(sid)

    for region, subnets in region_groups.items():
        cluster_name = f"cluster_{terraform_identifier(region)}"
        lines.append(f"  subgraph {cluster_name} {{")
        lines.append(f"    label=\"Region {region}\";")
        for sid in subnets:
            mapping = subnet_mapping[sid]
            node_name = f"sub_{terraform_identifier(mapping['gcp_subnet_name'])}"
            label = f"{mapping['gcp_subnet_name']}\\n{mapping['target_cidr']}"
            lines.append(f"    {node_name} [label=\"{label}\"]; ")
        lines.append("  }")

    for cfg in nat_configs:
        router_node = f"router_{terraform_identifier(cfg['router_name'])}"
        node_name = f"nat_{terraform_identifier(cfg['nat_name'])}"
        lines.append(f"  {router_node} [label=\"Router {cfg['router_name']}\", shape=box, style=filled, fillcolor=\"#DAE8FC\"];")
        lines.append(f"  {node_name} [label=\"Cloud NAT {cfg['nat_name']}\", shape=ellipse, style=filled, fillcolor=\"#E8F0FE\"]; ")
        lines.append(f"  {router_node} -> {node_name} [label=\"NAT\"];")

    if any(info.get("type") == "igw" for info in subnet_egress.values()):
        lines.append("  igw_default [label=\"Default Internet Gateway\", shape=ellipse, style=filled, fillcolor=\"#FFF4E5\"]; ")

    for sid, mapping in subnet_mapping.items():
        subnet_node = f"sub_{terraform_identifier(mapping['gcp_subnet_name'])}"
        egress = subnet_egress.get(sid, {})
        if egress.get("type") == "nat":
            cfg = nat_lookup.get(egress.get("target"))
            if cfg:
                router_node = f"router_{terraform_identifier(cfg['router_name'])}"
                lines.append(f"  {subnet_node} -> {router_node};")
        elif egress.get("type") == "igw":
            lines.append(f"  {subnet_node} -> igw_default;")
    
    for cfg in nat_configs:
        nat_node = f"nat_{terraform_identifier(cfg['nat_name'])}"
        lines.append(f"  {nat_node} -> igw_default [style=dashed, label=\"outbound\"];")

    lines.append("}")
    return "\n".join(lines)


def write_graphviz_artifacts(dot_str: Optional[str], target_dir: str, prefix: str = "architecture") -> Optional[str]:
    if not dot_str:
        return None
    os.makedirs(target_dir, exist_ok=True)
    dot_path = os.path.join(target_dir, f"{prefix}.dot")
    with open(dot_path, "w", encoding="utf-8") as fh:
        fh.write(dot_str)
        if not dot_str.endswith("\n"):
            fh.write("\n")
    image_path = None
    dot_bin = shutil.which("dot")
    if dot_bin:
        image_path = os.path.join(target_dir, f"{prefix}.png")
        try:
            subprocess.run([dot_bin, "-Tpng", dot_path, "-o", image_path], check=True, capture_output=True)
        except subprocess.CalledProcessError as exc:
            print(f"⚠️ Failed to render Graphviz image: {exc.stderr.decode().strip() if exc.stderr else exc}")
            image_path = None
    else:
        print("⚠️ Graphviz 'dot' binary not found; only the .dot file was written.")
    return image_path or dot_path


def analyze_network_topology(aws_vpc: AwsVpc, payload: Dict[str, Any], gcp_network: str,
                              gcp_region_fallback: str) -> Dict[str, Any]:
    subnet_entries = payload["aws"]["subnets"]
    subnet_mapping_by_id = {entry["aws_subnet_id"]: entry for entry in subnet_entries}
    subnet_egress = determine_subnet_egress(aws_vpc)
    nat_configs = build_nat_configs(aws_vpc, subnet_mapping_by_id, subnet_egress, gcp_network, gcp_region_fallback)
    architecture_table = render_architecture_table(aws_vpc, subnet_mapping_by_id, subnet_egress, nat_configs)
    return {
        "subnet_entries": subnet_entries,
        "subnet_mapping_by_id": subnet_mapping_by_id,
        "subnet_egress": subnet_egress,
        "nat_configs": nat_configs,
        "architecture_table": architecture_table,
        "architecture_mermaid": render_architecture_mermaid(aws_vpc, {
            "subnet_mapping_by_id": subnet_mapping_by_id,
            "subnet_egress": subnet_egress,
            "nat_configs": nat_configs,
        }, gcp_network),
        "architecture_graphviz": render_architecture_graphviz({
            "subnet_mapping_by_id": subnet_mapping_by_id,
            "subnet_egress": subnet_egress,
            "nat_configs": nat_configs,
        }, gcp_network),
    }
AWS_TO_GCP_REGION = {
    "us-east-1": "us-east1",
    "us-east-2": "us-east4",
    "us-west-1": "us-west2",
    "us-west-2": "us-west1",
    "ca-central-1": "northamerica-northeast1",
    "eu-west-1": "europe-west1",
    "eu-west-2": "europe-west2",
    "eu-west-3": "europe-west9",
    "eu-central-1": "europe-west3",
    "eu-north-1": "europe-north1",
    "eu-south-1": "europe-southwest1",
    "ap-south-1": "asia-south1",
    "ap-south-2": "asia-south2",
    "ap-southeast-1": "asia-southeast1",
    "ap-southeast-2": "australia-southeast1",
    "ap-southeast-3": "asia-southeast2",
    "ap-northeast-1": "asia-northeast1",
    "ap-northeast-2": "asia-northeast3",
    "ap-northeast-3": "asia-northeast2",
    "ap-east-1": "asia-east2",
    "sa-east-1": "southamerica-east1",
    "me-south-1": "me-central1",
}


def az_to_gcp_region(aws_az: str, fallback: str) -> str:
    if not aws_az:
        return fallback
    region = aws_az[:-1] if aws_az[-1].isalpha() else aws_az
    mapped = AWS_TO_GCP_REGION.get(region)
    return mapped or fallback


def prompt_subnet_cidrs(subnets: List[AwsSubnet]) -> Dict[str, str]:
    overrides: Dict[str, str] = {}
    if not subnets:
        return overrides
    print("\nAdjust target subnet CIDRs (press Enter to keep the AWS CIDR).")
    for s in subnets:
        label = s.name or s.id
        default = s.cidr
        while True:
            try:
                raw = input(f"CIDR for subnet {label} [{default}]: ").strip()
            except EOFError:
                raw = ""
            if not raw:
                overrides[s.id] = default
                break
            try:
                net = ipaddress.ip_network(raw, strict=False)
            except ValueError:
                print("  ⚠️  Invalid CIDR, please retry (example: 10.10.0.0/24).")
                continue
            overrides[s.id] = str(net)
            break
    return overrides


def prompt_subnet_names(subnets: List[AwsSubnet]) -> Dict[str, str]:
    overrides: Dict[str, str] = {}
    if not subnets:
        return overrides
    print("\nChoose GCP subnet names (press Enter to reuse the AWS name).")
    for s in subnets:
        label = s.name or s.id
        default = sanitize_name(label)
        while True:
            try:
                raw = input(f"Name for subnet {label} [{default}]: ").strip()
            except EOFError:
                raw = ""
            candidate = raw or default
            if not candidate:
                print("  ⚠️  Please provide a name or accept the default.")
                continue
            overrides[s.id] = candidate
            break
    return overrides


def prompt_yes_no(msg: str, default: bool = False) -> bool:
    suffix = "[Y/n]" if default else "[y/N]"
    while True:
        try:
            ans = input(f"{msg} {suffix} ").strip().lower()
        except EOFError:
            ans = ""
        if not ans:
            return default
        if ans in {"y", "yes", "ok", "okay"}:
            return True
        if ans in {"n", "no"}:
            return False
        print("Please answer 'y' or 'n'.")


def prompt_text(msg: str, default: Optional[str] = None) -> str:
    while True:
        prompt = f"{msg}"
        if default:
            prompt += f" [{default}]"
        prompt += ": "
        try:
            ans = input(prompt).strip()
        except EOFError:
            ans = ""
        if not ans and default:
            return default
        if ans:
            return ans
        print("Please provide a value.")


def prompt_select(prompt: str, options: List[str]) -> int:
    if not options:
        raise ValueError("No options to select from.")
    for idx, opt in enumerate(options, start=1):
        print(f" {idx}) {opt}")
    while True:
        try:
            ans = input(f"{prompt} [1-{len(options)}]: ").strip()
        except EOFError:
            ans = ""
        if not ans:
            continue
        if ans.lower() in {"q", "quit", "exit"}:
            raise KeyboardInterrupt
        if ans.isdigit():
            choice = int(ans)
            if 1 <= choice <= len(options):
                return choice - 1
        print("Please enter a valid number from the list (or 'q' to cancel).")


def interactive_choose_aws_vpc(region: str, provided_vpc_id: Optional[str]) -> str:
    vpcs = list_aws_vpcs(region)
    if not vpcs:
        raise SystemExit(f"❌ No VPCs found in AWS region {region}.")

    matched_index = None
    if provided_vpc_id:
        for idx, v in enumerate(vpcs):
            if v.get("VpcId") == provided_vpc_id:
                matched_index = idx
                break
        if matched_index is None:
            print(f"⚠️ Provided AWS VPC id {provided_vpc_id} not found. Pick from the discovered list.")

    if matched_index is not None:
        selection = vpcs[matched_index]
        label = f"{selection.get('Name') or selection['VpcId']} · {selection['VpcId']} · {selection.get('CidrBlock', 'unknown')}"
        if prompt_yes_no(f"Use AWS VPC {label}?", default=True):
            return selection["VpcId"]

    options = [f"{v.get('Name') or v['VpcId']} · {v['VpcId']} · {v.get('CidrBlock', 'unknown')}" for v in vpcs]
    choice = prompt_select("Select AWS VPC", options)
    return vpcs[choice]["VpcId"]


def build_interactive_cli_args() -> List[str]:
    print("\n=== Terraform VPC Toolkit ===")
    print("No arguments supplied; entering interactive mode. Press Ctrl+C at any time to abort.\n")

    modes = [
        ("migrate-aws-to-gcp", "Replicate an AWS VPC layout into GCP"),
        ("generate-terraform", "Generate Terraform bundle from an AWS VPC"),
        ("exit", "Exit the toolkit"),
    ]
    mode_choice = prompt_select(
        "Select operation",
        [f"{title} — {desc}" for title, desc in modes],
    )
    mode = modes[mode_choice][0]
    args: List[str] = [mode]

    def prompt_region(default: str = "us-east-1") -> str:
        return prompt_text("AWS region", default=default)

    if mode == "exit":
        print("\n👋 Exiting without taking any action.")
        sys.exit(0)

    aws_region = prompt_region()
    args.extend(["--aws-region", aws_region])

    if mode == "migrate-aws-to-gcp":
        if prompt_yes_no("Use step-by-step interactive planning?", default=True):
            args.append("--interactive")
        if prompt_yes_no("Apply changes after planning?", default=False):
            args.append("--apply")

    elif mode == "generate-terraform":
        args.append("--interactive")

    return args


def summarize_migration_plan(aws_vpc: AwsVpc, subnet_plans: List[SubnetPlan]) -> None:
    print("\n--- AWS VPC INVENTORY ---")
    print(f"VPC {aws_vpc.id} ({aws_vpc.name}) · CIDR {aws_vpc.cidr} · region {aws_vpc.region}")

    print("\nSubnets")
    if subnet_plans:
        for plan in subnet_plans:
            src = plan.source
            name = plan.source.name or src.id
            delta = "(unchanged)" if plan.target_cidr == src.cidr else "(override)"
            print(f" - {name} {src.cidr} → {plan.target_region}/{plan.target_name} {plan.target_cidr} {delta}")
    else:
        print(" - none")


def print_connectivity_map(aws_vpc: AwsVpc, subnet_plans: List[SubnetPlan], nat_plan: Dict[str, Dict[str, Any]]) -> None:
    print("\n--- CONNECTIVITY PLAN ---")
    if not subnet_plans:
        print(" (no subnets discovered; nothing to map)")
        return

    region_to_subnets: Dict[str, List[SubnetPlan]] = {}
    for plan in subnet_plans:
        region_to_subnets.setdefault(plan.target_region, []).append(plan)

    for region in sorted(region_to_subnets.keys()):
        print(f"Region {region}:")
        for plan in region_to_subnets[region]:
            label = plan.source.name or plan.source.id
            print(f"   • Subnet {plan.target_name} ({plan.target_cidr}) ← AWS {label} {plan.source.cidr}")
        if region in nat_plan:
            cfg = nat_plan[region]
            ips = ", ".join(cfg.get("ips", [])) or "(auto)"
            print(f"     ↳ Cloud Router {cfg['router']} with NAT {cfg['nat']} (IPs: {ips}) → default-internet-gateway")
        else:
            print("     ↳ Direct route via default-internet-gateway (no Cloud NAT)")

    if aws_vpc.internet_gateways:
        igw_list = ", ".join(igw.id for igw in aws_vpc.internet_gateways)
        print(f"Linked AWS IGW(s): {igw_list} → mapped to GCP default internet gateway")
    else:
        print("AWS VPC has no internet gateway attachments; only private connectivity will be recreated.")


def summarize_aws_vpc(aws_vpc: AwsVpc) -> None:
    print("\n--- AWS VPC OVERVIEW ---")
    print(f"VPC {aws_vpc.id} ({aws_vpc.name}) · CIDR {aws_vpc.cidr} · region {aws_vpc.region}")

    print("\nSubnets")
    if aws_vpc.subnets:
        for s in aws_vpc.subnets:
            label = s.name or s.id
            print(f" - {label} {s.cidr} @ {s.az} ({s.id})")
    else:
        print(" - none")

    print("\nRoute table (main associations)")
    if aws_vpc.routes:
        for r in aws_vpc.routes:
            dest = r.get("DestinationCidrBlock") or r.get("DestinationPrefixListId", "unknown")
            target = r.get("NatGatewayId") or r.get("GatewayId") or r.get("TransitGatewayId") or \
                     r.get("VpcPeeringConnectionId") or r.get("NetworkInterfaceId") or r.get("InstanceId") or "—"
            state = r.get("State", "unknown")
            print(f" - {dest} → {target} ({state})")
    else:
        print(" - none")

    print("\nNAT gateways")
    if aws_vpc.nat_gateways:
        for nat in aws_vpc.nat_gateways:
            allocations = ",".join(nat.allocation_ids) if nat.allocation_ids else "—"
            print(f" - {nat.id} subnet:{nat.subnet_id} state:{nat.state} eips:{allocations}")
    else:
        print(" - none")

    print("\nInternet gateways")
    if aws_vpc.internet_gateways:
        for igw in aws_vpc.internet_gateways:
            attachments = ",".join(f"{vpc}:{state}" for vpc, state in igw.attachment_states.items()) or "—"
            print(f" - {igw.id} attachments:{attachments}")
    else:
        print(" - none")

    print("\nSecurity groups")
    if aws_vpc.sgs:
        for sg_name, rules in aws_vpc.sgs.items():
            label = sg_name or "(untitled-sg)"
            print(f" - {label} ({len(rules)} rules)")
    else:
        print(" - none")


    print("\nRoute table (main associations)")
    if aws_vpc.routes:
        for r in aws_vpc.routes:
            dest = r.get("DestinationCidrBlock") or r.get("DestinationPrefixListId", "unknown")
            target = r.get("NatGatewayId") or r.get("GatewayId") or r.get("TransitGatewayId") or \
                     r.get("VpcPeeringConnectionId") or r.get("NetworkInterfaceId") or r.get("InstanceId") or "—"
            state = r.get("State", "unknown")
            print(f" - {dest} → {target} ({state})")
    else:
        print(" - none")

    print("\nNAT gateways")
    if aws_vpc.nat_gateways:
        for nat in aws_vpc.nat_gateways:
            allocations = ",".join(nat.allocation_ids) if nat.allocation_ids else "—"
            print(f" - {nat.id} subnet:{nat.subnet_id} state:{nat.state} eips:{allocations}")
    else:
        print(" - none")

    print("\nInternet gateways")
    if aws_vpc.internet_gateways:
        for igw in aws_vpc.internet_gateways:
            attachments = ",".join(f"{vpc}:{state}" for vpc, state in igw.attachment_states.items()) or "—"
            print(f" - {igw.id} attachments:{attachments}")
    else:
        print(" - none")

    print("\nSecurity groups / firewall intents")
    if aws_vpc.sgs:
        for sg_name, rules in aws_vpc.sgs.items():
            label = sg_name or "(untitled-sg)"
            print(f" - {label} ({len(rules)} rules)")
            for rule in rules:
                ports = "all" if rule.port_from is None else (
                    f"{rule.port_from}" if rule.port_from == rule.port_to else f"{rule.port_from}-{rule.port_to}"
                )
                cidrs = ",".join(rule.cidrs) if rule.cidrs else "0.0.0.0/0"
                print(f"     · {rule.direction.upper()} {rule.protocol} {ports} → {cidrs}")
    else:
        print(" - none")

# ---------- CLI

def main():
    p = argparse.ArgumentParser(description="VPC ⇄ VPC tooling (separate from ECS→GKE). Safe dry-run by default.")
    p.add_argument("--gcp-credential-file", "--credential-file-override", dest="gcp_credential_file",
                   help="Path to a Google Cloud service-account JSON for API calls.", metavar="PATH")
    sub = p.add_subparsers(dest="mode", required=True)

    # Common AWS args
    def add_aws_args(sp, require_vpc: bool = True):
        sp.add_argument("--aws-region", required=True)
        sp.add_argument("--aws-vpc-id", required=require_vpc)

    # migrate-aws-to-gcp
    m = sub.add_parser("migrate-aws-to-gcp", help="Replicate an AWS VPC layout into GCP.")
    add_aws_args(m, require_vpc=False)
    m.add_argument("--gcp-project")
    m.add_argument("--gcp-network", help="Target GCP VPC network name (will be created if missing).")
    m.add_argument("--gcp-region-fallback", help="GCP region to use when AZ→region mapping is unclear.")
    m.add_argument("--interactive", action="store_true", help="Prompt for subnet CIDR overrides and confirmation before creation.")
    m.add_argument("--apply", action="store_true", help="Execute commands instead of printing them.")

    tf_gen = sub.add_parser("generate-terraform", help="Generate Terraform configuration for a target AWS VPC.")
    add_aws_args(tf_gen, require_vpc=False)
    tf_gen.add_argument("--gcp-project")
    tf_gen.add_argument("--gcp-network")
    tf_gen.add_argument("--gcp-region-fallback")
    tf_gen.add_argument("--output-root", default="terraform", help="Root directory for generated Terraform bundles (default: terraform).")
    tf_gen.add_argument("--overwrite", action="store_true", help="Overwrite existing Terraform files if present.")
    tf_gen.add_argument("--interactive", action="store_true", help="Prompt for details and confirmation before writing files.")

    if len(sys.argv) == 1:
        try:
            interactive_args = build_interactive_cli_args()
        except KeyboardInterrupt:
            print("\n✋ Aborted before choosing a mode.")
            return
        args = p.parse_args(interactive_args)
    else:
        args = p.parse_args()

    configure_gcp_credentials(getattr(args, "gcp_credential_file", None))

    if args.mode == "migrate-aws-to-gcp":
        if args.interactive:
            try:
                args.aws_vpc_id = interactive_choose_aws_vpc(args.aws_region, args.aws_vpc_id)
            except KeyboardInterrupt:
                print("✋ Aborted before selecting a VPC.")
                return
        elif not args.aws_vpc_id:
            p.error("--aws-vpc-id is required (use --interactive to choose from discovered VPCs).")

        aws_vpc = discover_aws_vpc(args.aws_vpc_id, args.aws_region)
        subnet_name_overrides: Optional[Dict[str, str]] = None

        if args.interactive:
            summarize_aws_vpc(aws_vpc)
            if not prompt_yes_no("Continue with migration planning for this VPC?", default=True):
                print("✋ Aborted before planning.")
                return
            if not args.gcp_project:
                args.gcp_project = prompt_text("GCP project ID")
            default_network = sanitize_name(aws_vpc.name or aws_vpc.id)
            if not args.gcp_network:
                args.gcp_network = prompt_text("GCP network name", default=default_network)
            if not args.gcp_region_fallback:
                args.gcp_region_fallback = prompt_text("GCP region fallback (e.g. europe-west3)")
            subnet_name_overrides = prompt_subnet_names(aws_vpc.subnets)
        else:
            missing = []
            if not args.gcp_project:
                missing.append("--gcp-project")
            if not args.gcp_network:
                missing.append("--gcp-network")
            if not args.gcp_region_fallback:
                missing.append("--gcp-region-fallback")
            if missing:
                p.error("Missing required arguments: " + ", ".join(missing))
            subnet_name_overrides = {}

        network_exists = True
        try:
            networks_client = build_compute_client(compute_v1.NetworksClient)
            networks_client.get(project=args.gcp_project, network=args.gcp_network)
        except gcp_exceptions.NotFound:
            network_exists = False
        except gcp_exceptions.GoogleAPICallError as exc:
            raise SystemExit(f"Failed to query GCP network {args.gcp_network}: {exc}")
        if not network_exists:
            print(f"ℹ️ Target GCP VPC '{args.gcp_network}' not found; will create.")
        plan_migrate_aws_to_gcp(aws_vpc, args.gcp_project, args.gcp_network, args.gcp_region_fallback,
                                apply=args.apply, interactive=args.interactive, subnet_name_overrides=subnet_name_overrides)

    elif args.mode == "generate-terraform":
        subnet_overrides: Optional[Dict[str, str]] = None
        subnet_name_overrides: Optional[Dict[str, str]] = None
        if args.interactive:
            try:
                args.aws_vpc_id = interactive_choose_aws_vpc(args.aws_region, args.aws_vpc_id)
            except KeyboardInterrupt:
                print("✋ Aborted before selecting a VPC.")
                return
        elif not args.aws_vpc_id:
            p.error("--aws-vpc-id is required (use --interactive to choose from discovered VPCs).")

        aws_vpc = discover_aws_vpc(args.aws_vpc_id, args.aws_region)

        if args.interactive:
            summarize_aws_vpc(aws_vpc)
            if not prompt_yes_no("Generate Terraform bundle for this VPC?", default=True):
                print("✋ Aborted before Terraform generation.")
                return

            if not args.gcp_project or args.gcp_project == "your-gcp-project-id":
                args.gcp_project = prompt_text("GCP project ID", default="entropik-internal-poc")
            if not args.gcp_network:
                default_network = sanitize_name(aws_vpc.name or aws_vpc.id)
                args.gcp_network = prompt_text("GCP network name", default=default_network)
            if not args.gcp_region_fallback:
                mapped = AWS_TO_GCP_REGION.get(aws_vpc.region)
                default_region = mapped or "us-central1"
                args.gcp_region_fallback = prompt_text("GCP region fallback (e.g. europe-west3)", default=default_region)

            subnet_overrides = prompt_subnet_cidrs(aws_vpc.subnets)
            subnet_name_overrides = prompt_subnet_names(aws_vpc.subnets)

            folder_name = sanitize_name(aws_vpc.name or aws_vpc.id)
            normalized_root = resolve_workspace_path(args.output_root or "terraform")
            target_dir = os.path.join(normalized_root, folder_name)
            if os.path.isdir(target_dir) and not args.overwrite:
                if prompt_yes_no(f"Directory {target_dir} exists. Overwrite Terraform files?", default=False):
                    args.overwrite = True
                else:
                    print("✋ Aborted to avoid overwriting existing Terraform bundle.")
                    return
        else:
            missing = []
            if not args.gcp_project:
                missing.append("--gcp-project")
            if not args.gcp_network:
                missing.append("--gcp-network")
            if not args.gcp_region_fallback:
                missing.append("--gcp-region-fallback")
            if missing:
                p.error("Missing required arguments: " + ", ".join(missing))

            subnet_overrides = None
            subnet_name_overrides = {}

        try:
            target_dir, structure_checks, validation_messages = generate_terraform_from_aws_vpc(
                aws_vpc,
                args.gcp_project,
                args.gcp_network,
                args.gcp_region_fallback,
                args.output_root,
                overwrite=args.overwrite,
                subnet_cidr_overrides=subnet_overrides,
                subnet_name_overrides=subnet_name_overrides,
                preview_architecture=args.interactive,
            )
        except RuntimeError as exc:
            raise SystemExit(f"Terraform generation failed: {exc}") from exc
        print(f"\n🗂️ Terraform configuration written to {target_dir}")

        if structure_checks:
            print("\nInline content checks:")
            for msg in structure_checks:
                print(f"✅ {msg}")

        if validation_messages:
            print("\nTerraform CLI validation:")
            for msg in validation_messages:
                print(f"✅ {msg}")

if __name__ == "__main__":
    main()
