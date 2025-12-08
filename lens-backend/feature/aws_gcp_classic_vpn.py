#!/usr/bin/env python3
from __future__ import annotations

"""
Automate AWS <--> GCP Classic VPN (static routing) setup.

This script mirrors the HA VPN tool’s UX but follows the Classic pattern:
1. Reserve/ensure a regional GCP static IP.
2. Create/attach AWS VGW (ipsec.1) to the target VPC.
3. Create/ensure AWS Customer Gateway pointing to the GCP IP (static ASN).
4. Create an AWS Site-to-Site VPN connection with static routes (GCP CIDRs).
5. Wait for AWS to emit tunnel outside IPs + PSKs.
6. Create GCP Target VPN Gateway and forwarding rules (ESP/UDP500/UDP4500).
7. Create 2 Classic VPN tunnels (IKEv1/2) using the AWS outside IPs.
8. Create GCP route(s) pointing AWS CIDR through the tunnels.
9. Optionally enable VGW route propagation on AWS route tables.

Requirements:
- AWS credentials configured (env/profile)
- GCP service account JSON in GOOGLE_APPLICATION_CREDENTIALS
- boto3, google-api-python-client, google-auth
"""

import argparse
import ipaddress
import json
import logging
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import boto3
from botocore.exceptions import ClientError
from google.oauth2 import service_account
from googleapiclient import discovery
from googleapiclient.errors import HttpError

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)

METADATA_DIR = Path(__file__).resolve().parent / "vpn_runs"


def build_resource_names(prefix: str) -> Dict[str, str]:
    return {
        "aws_vgw": f"{prefix}-vgw",
        "aws_cgw": f"{prefix}-cgw",
        "aws_vpn": f"{prefix}-vpn",
        "gcp_address": f"{prefix}-classic-ip",
        "gcp_gateway": f"{prefix}-classic-gateway",
        "gcp_tunnel_prefix": f"{prefix}-tunnel",
        "gcp_forwarding_esp": f"{prefix}-esp",
        "gcp_forwarding_udp500": f"{prefix}-udp500",
        "gcp_forwarding_udp4500": f"{prefix}-udp4500",
        "gcp_route": f"{prefix}-aws-route",
    }


class CleanupManager:
    """Track created resources and remove them if something fails."""

    def __init__(self):
        self._actions: List[Tuple[str, callable, tuple, dict]] = []

    def add(self, description: str, func, *args, **kwargs):
        self._actions.append((description, func, args, kwargs))

    def clear(self):
        self._actions.clear()

    def run(self):
        while self._actions:
            description, func, args, kwargs = self._actions.pop()
            try:
                logger.info(f"Cleaning up: {description}")
                func(*args, **kwargs)
            except Exception as exc:
                logger.warning(f"Cleanup failed for {description}: {exc}")


def write_metadata(prefix: str, data: dict):
    METADATA_DIR.mkdir(parents=True, exist_ok=True)
    path = METADATA_DIR / f"{prefix}.json"
    tmp = path.with_suffix(".tmp")
    with tmp.open("w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2, sort_keys=True)
    tmp.replace(path)
    logger.info(f"Metadata written to {path}")


def get_compute_service():
    creds = service_account.Credentials.from_service_account_file(
        os.environ.get("GOOGLE_APPLICATION_CREDENTIALS")
    )
    return discovery.build("compute", "v1", credentials=creds, cache_discovery=False)


def wait_for_gcp_operation(
    compute, project: str, operation: dict, region: Optional[str] = None, is_global: bool = False, poll: int = 3
):
    op_name = operation.get("name")
    while True:
        if region:
            result = compute.regionOperations().get(project=project, region=region, operation=op_name).execute()
        elif is_global:
            result = compute.globalOperations().get(project=project, operation=op_name).execute()
        else:
            zone_ref = operation.get("zone")
            if not zone_ref:
                raise RuntimeError("Zone must be provided for zonal operations")
            zone = zone_ref.split("/")[-1]
            result = compute.zoneOperations().get(project=project, zone=zone, operation=op_name).execute()
        if result.get("status") == "DONE":
            if "error" in result:
                raise RuntimeError(f"GCP operation {op_name} failed: {result['error']}")
            return result
        time.sleep(poll)
# -----------------------------------------------------------------------------
# AWS helpers
# -----------------------------------------------------------------------------

def ensure_vgw_attached(ec2, vpc_id: str, vgw_name: str, amazon_side_asn: Optional[int] = None) -> Tuple[str, bool]:
    """Create/attach VGW using the same logic as the HA script, reusing any existing attachment."""
    resp = ec2.describe_vpn_gateways(Filters=[{"Name": "tag:Name", "Values": [vgw_name]}])
    active = [gw for gw in resp["VpnGateways"] if gw.get("State") != "deleted"]
    vgw = None
    created = False
    if active:
        vgw = active[0]
        vgw_id = vgw["VpnGatewayId"]
        logger.info(f"Found existing VGW {vgw_id}")
    else:
        # See if the VPC already has a VGW attached (maybe with a different name).
        attached_resp = ec2.describe_vpn_gateways(Filters=[{"Name": "attachment.vpc-id", "Values": [vpc_id]}])
        attached_candidates = [
            gw for gw in attached_resp.get("VpnGateways", [])
            if any(att.get("VpcId") == vpc_id and att.get("State") == "attached" for att in gw.get("VpcAttachments", []))
        ]
        if attached_candidates:
            vgw = attached_candidates[0]
            vgw_id = vgw["VpnGatewayId"]
            logger.info(f"Reusing VGW {vgw_id} already attached to {vpc_id}")
        else:
            create_args = {"Type": "ipsec.1"}
            if amazon_side_asn:
                create_args["AmazonSideAsn"] = amazon_side_asn
            logger.info("Creating VGW...")
            resp = ec2.create_vpn_gateway(**create_args)
            vgw = resp["VpnGateway"]
            vgw_id = vgw["VpnGatewayId"]
            ec2.create_tags(Resources=[vgw_id], Tags=[{"Key": "Name", "Value": vgw_name}])
            created = True

    attachments = vgw.get("VpcAttachments", [])
    attached = any(att.get("VpcId") == vpc_id and att.get("State") == "attached" for att in attachments)
    if attached:
        logger.info(f"VGW {vgw_id} already attached to {vpc_id}")
        return vgw_id, created

    logger.info(f"Attaching VGW {vgw_id} to VPC {vpc_id}")
    try:
        ec2.attach_vpn_gateway(VpnGatewayId=vgw_id, VpcId=vpc_id)
    except ClientError as exc:
        code = exc.response["Error"]["Code"]
        if code not in ("Resource.AlreadyAssociated", "IncorrectState"):
            raise

    while True:
        gw = ec2.describe_vpn_gateways(VpnGatewayIds=[vgw_id])["VpnGateways"][0]
        for att in gw.get("VpcAttachments", []):
            if att.get("VpcId") == vpc_id and att.get("State") == "attached":
                logger.info(f"VGW {vgw_id} attached successfully")
                return vgw_id, created
        time.sleep(3)


def ensure_customer_gateway(ec2, ip_address: str, name: str, bgp_asn: int) -> Tuple[str, bool]:
    gateways = ec2.describe_customer_gateways()["CustomerGateways"]
    for cgw in gateways:
        if (
            cgw.get("State") == "available"
            and cgw.get("IpAddress") == ip_address
            and str(cgw.get("BgpAsn")) == str(bgp_asn)
        ):
            tags = {t["Key"]: t["Value"] for t in cgw.get("Tags", [])}
            logger.info(
                f"Reusing existing CGW {cgw['CustomerGatewayId']} (Name={tags.get('Name')}) "
                f"for IP {ip_address} / ASN {bgp_asn}"
            )
            return cgw["CustomerGatewayId"], False
    resp = ec2.create_customer_gateway(Type="ipsec.1", PublicIp=ip_address, BgpAsn=bgp_asn)
    cgw_id = resp["CustomerGateway"]["CustomerGatewayId"]
    ec2.create_tags(Resources=[cgw_id], Tags=[{"Key": "Name", "Value": name}])
    logger.info(f"Created CGW {cgw_id}")
    deadline = time.time() + 300
    while True:
        info = ec2.describe_customer_gateways(CustomerGatewayIds=[cgw_id])
        state = info["CustomerGateways"][0].get("State")
        if state == "available":
            break
        if time.time() > deadline:
            raise TimeoutError(f"Timed out waiting for CGW {cgw_id} to become available")
        logger.info(f"  Waiting for CGW state=available (current={state})")
        time.sleep(5)
    return cgw_id, True


def create_vpn_connection_static(
    ec2, cgw_id: str, vgw_id: str, name: str, static_prefixes: List[str]
) -> Tuple[str, bool]:
    for vpn in ec2.describe_vpn_connections()["VpnConnections"]:
        tags = {t["Key"]: t["Value"] for t in vpn.get("Tags", [])}
        if tags.get("Name") == name and vpn.get("CustomerGatewayId") == cgw_id and vpn.get("VpnGatewayId") == vgw_id:
            logger.info(f"Reusing VPN connection {vpn['VpnConnectionId']}")
            return vpn["VpnConnectionId"], False
    resp = ec2.create_vpn_connection(
        CustomerGatewayId=cgw_id,
        VpnGatewayId=vgw_id,
        Type="ipsec.1",
        Options={"StaticRoutesOnly": True},
        TagSpecifications=[
            {"ResourceType": "vpn-connection", "Tags": [{"Key": "Name", "Value": name}]}
        ],
    )
    vpn_id = resp["VpnConnection"]["VpnConnectionId"]
    logger.info(f"Created VPN connection {vpn_id}; configuring static routes...")
    for cidr in static_prefixes:
        try:
            ec2.create_vpn_connection_route(VpnConnectionId=vpn_id, DestinationCidrBlock=cidr)
        except ClientError as exc:
            if exc.response["Error"]["Code"] != "RouteAlreadyExists":
                raise
    return vpn_id, True


def wait_for_aws_tunnel_details(ec2, vpn_id: str, timeout: int = 900) -> Tuple[List[str], List[str], dict]:
    start = time.time()
    next_log = 0
    while True:
        vpn = ec2.describe_vpn_connections(VpnConnectionIds=[vpn_id])["VpnConnections"][0]
        opts = vpn.get("Options", {}) or {}
        tunnels = opts.get("TunnelOptions") or []
        outs = [item.get("OutsideIpAddress") for item in tunnels if item.get("OutsideIpAddress")]
        psks = [item.get("PreSharedKey") for item in tunnels if item.get("PreSharedKey")]
        if len(outs) >= 2 and len(psks) >= 2:
            return outs[:2], psks[:2], vpn
        xml = vpn.get("CustomerGatewayConfiguration") or ""
        xml_outs, xml_psks = [], []
        for line in xml.splitlines():
            line = line.strip()
            if "<outside-ip-address>" in line:
                xml_outs.append(line.split(">")[1].split("<")[0])
            if "<pre-shared-key>" in line:
                xml_psks.append(line.split(">")[1].split("<")[0])
        if len(xml_outs) >= 2 and len(xml_psks) >= 2:
            return xml_outs[:2], xml_psks[:2], vpn
        elapsed = time.time() - start
        if elapsed >= next_log:
            logger.info(f"Waiting for AWS tunnel info (state={vpn.get('State')}, elapsed={int(elapsed)}s)")
            next_log = elapsed + 30
        if elapsed > timeout:
            raise TimeoutError("Timed out waiting for AWS tunnel outside IPs/PSKs")
        time.sleep(5)


def delete_vpn_connection(ec2, vpn_id: str):
    try:
        ec2.delete_vpn_connection(VpnConnectionId=vpn_id)
        logger.info(f"Deleted VPN connection {vpn_id}")
    except ClientError as exc:
        if exc.response["Error"]["Code"] != "InvalidVpnConnectionID.NotFound":
            logger.warning(f"Failed to delete VPN connection {vpn_id}: {exc}")


def delete_customer_gateway(ec2, cgw_id: str):
    try:
        ec2.delete_customer_gateway(CustomerGatewayId=cgw_id)
        logger.info(f"Deleted CGW {cgw_id}")
    except ClientError as exc:
        if exc.response["Error"]["Code"] != "InvalidCustomerGatewayID.NotFound":
            logger.warning(f"Failed to delete CGW {cgw_id}: {exc}")


def delete_vgw(ec2, vgw_id: str, vpc_id: str):
    try:
        try:
            ec2.detach_vpn_gateway(VpnGatewayId=vgw_id, VpcId=vpc_id)
        except ClientError:
            pass
        ec2.delete_vpn_gateway(VpnGatewayId=vgw_id)
        logger.info(f"Deleted VGW {vgw_id}")
    except ClientError as exc:
        if exc.response["Error"]["Code"] != "InvalidVpnGatewayID.NotFound":
            logger.warning(f"Failed to delete VGW {vgw_id}: {exc}")
# -----------------------------------------------------------------------------
# GCP helpers
# -----------------------------------------------------------------------------

def reserve_address(compute, project: str, region: str, name: str) -> Tuple[str, bool]:
    try:
        result = compute.addresses().get(project=project, region=region, address=name).execute()
        return result["address"], False
    except HttpError as exc:
        if exc.resp.status != 404:
            raise
    op = compute.addresses().insert(project=project, region=region, body={"name": name}).execute()
    wait_for_gcp_operation(compute, project, op, region=region)
    result = compute.addresses().get(project=project, region=region, address=name).execute()
    return result["address"], True


def delete_gcp_address(compute, project: str, region: str, name: str):
    try:
        op = compute.addresses().delete(project=project, region=region, address=name).execute()
        wait_for_gcp_operation(compute, project, op, region=region)
    except HttpError as exc:
        if exc.resp.status != 404:
            logger.warning(f"Failed to delete address {name}: {exc}")


def ensure_target_vpn_gateway(compute, project: str, region: str, network: str, name: str) -> Tuple[str, bool]:
    try:
        gateway = compute.targetVpnGateways().get(project=project, region=region, targetVpnGateway=name).execute()
        return gateway["selfLink"], False
    except HttpError as exc:
        if exc.resp.status != 404:
            raise
    body = {
        "name": name,
        "network": f"projects/{project}/global/networks/{network}",
    }
    op = compute.targetVpnGateways().insert(project=project, region=region, body=body).execute()
    wait_for_gcp_operation(compute, project, op, region=region)
    gateway = compute.targetVpnGateways().get(project=project, region=region, targetVpnGateway=name).execute()
    return gateway["selfLink"], True


def delete_gcp_vpn_gateway(compute, project: str, region: str, name: str):
    try:
        op = compute.targetVpnGateways().delete(project=project, region=region, targetVpnGateway=name).execute()
        wait_for_gcp_operation(compute, project, op, region=region)
    except HttpError as exc:
        if exc.resp.status != 404:
            logger.warning(f"Failed to delete target VPN gateway {name}: {exc}")


def ensure_forwarding_rule(
    compute,
    project: str,
    region: str,
    name: str,
    address_link: str,
    target_link: str,
    protocol: str,
    port_range: Optional[str] = None,
) -> bool:
    try:
        compute.forwardingRules().get(project=project, region=region, forwardingRule=name).execute()
        return False
    except HttpError as exc:
        if exc.resp.status != 404:
            raise
    body = {
        "name": name,
        "IPAddress": address_link,
        "IPProtocol": protocol,
        "target": target_link,
    }
    if port_range:
        body["portRange"] = port_range
    op = compute.forwardingRules().insert(project=project, region=region, body=body).execute()
    wait_for_gcp_operation(compute, project, op, region=region)
    return True


def delete_forwarding_rule(compute, project: str, region: str, name: str):
    try:
        op = compute.forwardingRules().delete(project=project, region=region, forwardingRule=name).execute()
        wait_for_gcp_operation(compute, project, op, region=region)
    except HttpError as exc:
        if exc.resp.status != 404:
            logger.warning(f"Failed to delete forwarding rule {name}: {exc}")


def compute_covering_cidr(cidrs: List[str]) -> str:
    networks = [ipaddress.ip_network(c.strip(), strict=False) for c in cidrs if c.strip()]
    if not networks:
        raise ValueError("No CIDRs provided")
    min_ip = min(net.network_address for net in networks)
    max_ip = max(net.broadcast_address for net in networks)
    summary = list(ipaddress.summarize_address_range(min_ip, max_ip))
    result = summary[0]
    for net in summary[1:]:
        while not result.supernet_of(net):
            result = result.supernet()
    return str(result)


def ensure_classic_vpn_tunnel(
    compute,
    project: str,
    region: str,
    gateway_name: str,
    tunnel_name: str,
    peer_ip: str,
    shared_secret: str,
    remote_ranges: List[str],
    local_ranges: List[str],
    ike_version: int = 1,
) -> bool:
    try:
        compute.vpnTunnels().get(project=project, region=region, vpnTunnel=tunnel_name).execute()
        logger.info(f"Tunnel {tunnel_name} already exists")
        return False
    except HttpError as exc:
        if exc.resp.status != 404:
            raise
    body = {
        "name": tunnel_name,
        "peerIp": peer_ip,
        "ikeVersion": ike_version,
        "sharedSecret": shared_secret,
        "targetVpnGateway": f"projects/{project}/regions/{region}/targetVpnGateways/{gateway_name}",
    }
    if remote_ranges:
        body["remoteTrafficSelector"] = remote_ranges
    if local_ranges:
        selectors = local_ranges
        if ike_version == 1 and len(local_ranges) > 1:
            selectors = [compute_covering_cidr(local_ranges)]
        body["localTrafficSelector"] = selectors
    op = compute.vpnTunnels().insert(project=project, region=region, body=body).execute()
    wait_for_gcp_operation(compute, project, op, region=region)
    logger.info(f"Created tunnel {tunnel_name}")
    return True


def delete_gcp_vpn_tunnel(compute, project: str, region: str, name: str):
    try:
        op = compute.vpnTunnels().delete(project=project, region=region, vpnTunnel=name).execute()
        wait_for_gcp_operation(compute, project, op, region=region)
    except HttpError as exc:
        if exc.resp.status != 404:
            logger.warning(f"Failed to delete tunnel {name}: {exc}")


def ensure_gcp_route(
    compute,
    project: str,
    name: str,
    dest_range: str,
    tunnel_name: str,
    region: str,
    network: str,
) -> bool:
    try:
        compute.routes().get(project=project, route=name).execute()
        logger.info(f"Route {name} already exists")
        return False
    except HttpError as exc:
        if exc.resp.status != 404:
            raise
    body = {
        "name": name,
        "destRange": dest_range,
        "network": f"projects/{project}/global/networks/{network}",
        "nextHopVpnTunnel": f"projects/{project}/regions/{region}/vpnTunnels/{tunnel_name}",
        "priority": 1000,
    }
    op = compute.routes().insert(project=project, body=body).execute()
    wait_for_gcp_operation(compute, project, op, is_global=True)
    return True


def delete_gcp_route(compute, project: str, name: str):
    try:
        op = compute.routes().delete(project=project, route=name).execute()
        wait_for_gcp_operation(compute, project, op, is_global=True)
    except HttpError as exc:
        if exc.resp.status != 404:
            logger.warning(f"Failed to delete route {name}: {exc}")


# -----------------------------------------------------------------------------
# Route propagation helpers
# -----------------------------------------------------------------------------

def determine_route_tables(route_tables: List[dict], subnet_ids: Optional[List[str]] = None) -> List[str]:
    subnet_ids = subnet_ids or []
    subnet_to_rt = {}
    main_rt = None
    for rt in route_tables:
        rt_id = rt["RouteTableId"]
        for assoc in rt.get("Associations", []):
            if assoc.get("Main"):
                main_rt = rt_id
            subnet = assoc.get("SubnetId")
            if subnet:
                subnet_to_rt[subnet] = rt_id
    if not subnet_ids:
        return sorted({rt["RouteTableId"] for rt in route_tables})
    targets = []
    for subnet_id in subnet_ids:
        rt_id = subnet_to_rt.get(subnet_id)
        if rt_id:
            targets.append(rt_id)
        elif main_rt:
            targets.append(main_rt)
    return sorted(set(targets))


def enable_route_propagation(
    ec2, vpc_id: str, vgw_id: str, subnet_ids: Optional[List[str]]
) -> List[str]:
    resp = ec2.describe_route_tables(Filters=[{"Name": "vpc-id", "Values": [vpc_id]}])
    tables = resp.get("RouteTables", [])
    if not tables:
        logger.warning("No route tables found; skipping propagation")
        return []
    targets = determine_route_tables(tables, subnet_ids)
    enabled = []
    for rt_id in targets:
        try:
            ec2.enable_vgw_route_propagation(RouteTableId=rt_id, GatewayId=vgw_id)
            logger.info(f"Enabled propagation on {rt_id}")
            enabled.append(rt_id)
        except ClientError as exc:
            if exc.response["Error"]["Code"] == "RouteAlreadyExists":
                enabled.append(rt_id)
                logger.info(f"Propagation already enabled on {rt_id}")
            else:
                logger.warning(f"Failed to enable propagation on {rt_id}: {exc}")
    return enabled
# -----------------------------------------------------------------------------
# Config + main workflow
# -----------------------------------------------------------------------------


class ClassicVPNConfig:
    def __init__(self):
        self.aws_region: Optional[str] = None
        self.aws_vpc_id: Optional[str] = None
        self.aws_vpc_cidr: Optional[str] = None
        self.aws_vgw_asn: Optional[int] = None

        self.gcp_project: Optional[str] = None
        self.gcp_region: Optional[str] = None
        self.gcp_network: Optional[str] = None
        self.gcp_subnet_cidrs: List[str] = []
        self.gcp_asn: Optional[int] = None

        self.tunnel_ike_version: int = 1


def setup_classic_vpn(
    config: ClassicVPNConfig,
    prefix: str,
    propagate_subnets: Optional[List[str]],
    enable_propagation: bool,
):
    names = build_resource_names(prefix)
    compute = get_compute_service()
    ec2 = boto3.client("ec2", region_name=config.aws_region)
    cleanup = CleanupManager()

    resources = {}
    metadata = {
        "timestamp_utc": datetime.now(timezone.utc).isoformat(),
        "name_prefix": prefix,
        "aws_region": config.aws_region,
        "aws_vpc_id": config.aws_vpc_id,
        "aws_vpc_cidr": config.aws_vpc_cidr,
        "aws_vgw_asn": config.aws_vgw_asn,
        "gcp_project": config.gcp_project,
        "gcp_region": config.gcp_region,
        "gcp_network": config.gcp_network,
        "gcp_asn": config.gcp_asn,
        "gcp_subnets": config.gcp_subnet_cidrs,
        "resources": resources,
    }

    try:
        gcp_ip, ip_created = reserve_address(compute, config.gcp_project, config.gcp_region, names["gcp_address"])
        resources["gcp_ip"] = gcp_ip
        if ip_created:
            cleanup.add(
                "GCP address",
                delete_gcp_address,
                compute,
                config.gcp_project,
                config.gcp_region,
                names["gcp_address"],
            )

        vgw_id, vgw_created = ensure_vgw_attached(ec2, config.aws_vpc_id, names["aws_vgw"], config.aws_vgw_asn)
        resources["aws_vgw_id"] = vgw_id
        if vgw_created:
            cleanup.add("AWS VGW", delete_vgw, ec2, vgw_id, config.aws_vpc_id)

        cgw_id, cgw_created = ensure_customer_gateway(ec2, gcp_ip, names["aws_cgw"], config.gcp_asn)
        resources["aws_cgw_id"] = cgw_id
        if cgw_created:
            cleanup.add("AWS CGW", delete_customer_gateway, ec2, cgw_id)

        vpn_id, vpn_created = create_vpn_connection_static(
            ec2, cgw_id, vgw_id, names["aws_vpn"], config.gcp_subnet_cidrs
        )
        resources["aws_vpn_id"] = vpn_id
        if vpn_created:
            cleanup.add("AWS VPN connection", delete_vpn_connection, ec2, vpn_id)

        outside_ips, psks, _ = wait_for_aws_tunnel_details(ec2, vpn_id)

        gateway_link, gateway_created = ensure_target_vpn_gateway(
            compute, config.gcp_project, config.gcp_region, config.gcp_network, names["gcp_gateway"]
        )
        resources["gcp_target_gateway"] = gateway_link
        if gateway_created:
            cleanup.add(
                "GCP target VPN gateway",
                delete_gcp_vpn_gateway,
                compute,
                config.gcp_project,
                config.gcp_region,
                names["gcp_gateway"],
            )

        address_link = f"projects/{config.gcp_project}/regions/{config.gcp_region}/addresses/{names['gcp_address']}"
        target_link = f"projects/{config.gcp_project}/regions/{config.gcp_region}/targetVpnGateways/{names['gcp_gateway']}"
        forwarding_specs = [
            (names["gcp_forwarding_esp"], "ESP", None),
            (names["gcp_forwarding_udp500"], "UDP", "500-500"),
            (names["gcp_forwarding_udp4500"], "UDP", "4500-4500"),
        ]
        created_forwarding = []
        for fr_name, proto, port in forwarding_specs:
            created = ensure_forwarding_rule(
                compute,
                config.gcp_project,
                config.gcp_region,
                fr_name,
                address_link,
                target_link,
                proto,
                port,
            )
            if created:
                cleanup.add(
                    f"Forwarding rule {fr_name}",
                    delete_forwarding_rule,
                    compute,
                    config.gcp_project,
                    config.gcp_region,
                    fr_name,
                )
                created_forwarding.append(fr_name)
        resources["gcp_forwarding_rules"] = created_forwarding or [spec[0] for spec in forwarding_specs]

        tunnels = []
        for idx, peer_ip in enumerate(outside_ips[:2]):
            tunnel_name = f"{names['gcp_tunnel_prefix']}-{idx+1}"
            created = ensure_classic_vpn_tunnel(
                compute,
                config.gcp_project,
                config.gcp_region,
                names["gcp_gateway"],
                tunnel_name,
                peer_ip,
                psks[idx],
                remote_ranges=[config.aws_vpc_cidr],
                local_ranges=config.gcp_subnet_cidrs,
                ike_version=config.tunnel_ike_version,
            )
            tunnels.append(tunnel_name)
            if created:
                cleanup.add(
                    f"GCP tunnel {tunnel_name}",
                    delete_gcp_vpn_tunnel,
                    compute,
                    config.gcp_project,
                    config.gcp_region,
                    tunnel_name,
                )
        resources["gcp_tunnels"] = tunnels

        route_created = ensure_gcp_route(
            compute,
            config.gcp_project,
            names["gcp_route"],
            config.aws_vpc_cidr,
            tunnels[0],
            config.gcp_region,
            config.gcp_network,
        )
        resources["gcp_route"] = names["gcp_route"]
        if route_created:
            cleanup.add(
                "GCP route",
                delete_gcp_route,
                compute,
                config.gcp_project,
                names["gcp_route"],
            )

        if enable_propagation:
            enabled = enable_route_propagation(ec2, config.aws_vpc_id, vgw_id, propagate_subnets)
            resources["aws_route_tables"] = enabled
        else:
            resources["aws_route_tables"] = []

        cleanup.clear()
        return metadata
    except BaseException:
        cleanup.run()
        raise
# -----------------------------------------------------------------------------
# CLI helpers
# -----------------------------------------------------------------------------

def prompt_with_default(message: str, default: Optional[str] = None) -> str:
    while True:
        suffix = f" [{default}]" if default else ""
        resp = input(f"{message}{suffix}: ").strip()
        if resp:
            return resp
        if default:
            return default
        print("A value is required.")


def prompt_cidr_list(message: str, default: Optional[List[str]] = None) -> List[str]:
    default_text = ",".join(default) if default else None
    while True:
        raw = prompt_with_default(message, default_text)
        cidrs = [segment.strip() for segment in raw.split(",") if segment.strip()]
        if cidrs:
            return cidrs
        print("Please enter at least one CIDR.")


def prompt_int(message: str, default: Optional[int] = None) -> int:
    while True:
        suffix = f" [{default}]" if default is not None else ""
        raw = input(f"{message}{suffix}: ").strip()
        if not raw:
            if default is not None:
                return default
            print("A value is required.")
            continue
        try:
            return int(raw)
        except ValueError:
            print("Please enter a numeric ASN value.")


def prompt_multi_select(title: str, items: List[Dict[str, object]], allow_all: bool = True):
    if not items:
        raise ValueError("No items available for selection.")
    while True:
        print(f"\n{title}")
        for idx, option in enumerate(items, start=1):
            print(f"  [{idx}] {option['label']}")
        suffix = " or 'all'" if allow_all else ""
        raw = input(f"Enter comma-separated numbers{suffix}: ").strip().lower()
        if allow_all and raw in ("all", "a", "*"):
            return [item["value"] for item in items], True
        if not raw:
            print("Selection required.")
            continue
        tokens = [token.strip() for token in raw.split(",") if token.strip()]
        indices = []
        valid = True
        for token in tokens:
            if not token.isdigit():
                valid = False
                break
            idx = int(token)
            if idx < 1 or idx > len(items):
                valid = False
                break
            indices.append(idx - 1)
        if not valid or not indices:
            print("Invalid selection. Try again.")
            continue
        unique_indices = []
        for idx in indices:
            if idx not in unique_indices:
                unique_indices.append(idx)
        return [items[idx]["value"] for idx in unique_indices], False


def prompt_select_option(
    message: str,
    options: List[Dict[str, str]],
    allow_manual: bool = True,
    default_value: Optional[str] = None,
) -> str:
    if not options:
        if allow_manual:
            return prompt_with_default(message, default_value)
        raise ValueError("No options available for selection.")

    print(f"\n{message}")
    for idx, opt in enumerate(options, start=1):
        print(f"  {idx}. {opt['label']}")
    prompt = "Enter choice number"
    if allow_manual:
        prompt += " (or type a value)"
    if default_value:
        prompt += f" [default: {default_value}]"
    prompt += ": "

    while True:
        choice = input(prompt).strip()
        if not choice:
            if default_value:
                return default_value
            continue
        if choice.isdigit():
            idx = int(choice)
            if 1 <= idx <= len(options):
                return options[idx - 1]["value"]
        if allow_manual:
            return choice
        print("Invalid selection, try again.")


def list_gcp_regions(compute, project: str) -> List[str]:
    regions = []
    request = compute.regions().list(project=project)
    while request is not None:
        response = request.execute()
        for region in response.get("items", []):
            name = region.get("name")
            if not name:
                continue
            if region.get("status") == "UP":
                regions.append(name)
        request = compute.regions().list_next(previous_request=request, previous_response=response)
    return sorted(set(regions))


def list_gcp_networks(compute, project: str) -> List[Dict[str, str]]:
    networks = []
    request = compute.networks().list(project=project)
    while request is not None:
        response = request.execute()
        networks.extend(response.get("items", []))
        request = compute.networks().list_next(previous_request=request, previous_response=response)
    return networks


def get_tag_value(resource: dict, key: str = "Name") -> str:
    for tag in resource.get("Tags", []) or []:
        if tag.get("Key") == key:
            return tag.get("Value") or ""
    return ""


def list_aws_subnets(ec2, vpc_id: str) -> List[Dict[str, object]]:
    resp = ec2.describe_subnets(Filters=[{"Name": "vpc-id", "Values": [vpc_id]}])
    items = []
    for subnet in resp.get("Subnets", []):
        name = get_tag_value(subnet)
        label = f"{subnet['SubnetId']} ({subnet.get('CidrBlock', '?')} | {subnet.get('AvailabilityZone', '?')})"
        if name:
            label += f" - {name}"
        items.append({"label": label, "value": subnet})
    return items


def list_gcp_subnets(compute, project: str, network_self_link: str, region: Optional[str] = None) -> List[dict]:
    subnets = []
    if not compute or not network_self_link:
        return subnets
    request = compute.subnetworks().aggregatedList(project=project)
    while request is not None:
        response = request.execute()
        for region_entry in response.get("items", {}).values():
            for subnet in region_entry.get("subnetworks", []):
                if subnet.get("network") != network_self_link:
                    continue
                if region:
                    subnet_region = subnet.get("region", "").split("/")[-1]
                    if subnet_region != region:
                        continue
                subnets.append(subnet)
        request = compute.subnetworks().aggregatedList_next(previous_request=request, previous_response=response)
    return subnets


def parse_subnet_ids(value: Optional[str]) -> Optional[List[str]]:
    if value is None:
        return None
    raw = value.strip()
    if raw == "":
        return None
    if raw.lower() in ("none", "skip"):
        return []
    if raw.lower() == "all":
        return None
    return [segment.strip() for segment in raw.split(",") if segment.strip()]


def interactive_setup(args):
    required = [
        "aws_region",
        "aws_vpc_id",
        "aws_vpc_cidr",
        "gcp_project",
        "gcp_region",
        "gcp_network",
        "gcp_subnets",
    ]
    if all(getattr(args, field) for field in required):
        return

    print("\n=== Interactive Classic VPN configuration ===")
    propagate_cli_supplied = any(arg.startswith("--propagate-subnets") for arg in sys.argv)
    gcp_subnets_cli_supplied = any(arg.startswith("--gcp-subnets") for arg in sys.argv)

    if not args.aws_region:
        regions = sorted(set(boto3.Session().get_available_regions("ec2")))
        for idx, region in enumerate(regions, start=1):
            print(f"  {idx}. {region}")
        choice = input("Select AWS region (number or name): ").strip()
        if choice.isdigit() and 1 <= int(choice) <= len(regions):
            args.aws_region = regions[int(choice) - 1]
        elif choice:
            args.aws_region = choice
        else:
            args.aws_region = regions[0]
        print(f"AWS region selected: {args.aws_region}")

    ec2 = boto3.Session(region_name=args.aws_region).client("ec2")

    if not args.aws_vpc_id:
        vpcs = ec2.describe_vpcs().get("Vpcs", [])
        if not vpcs:
            raise RuntimeError("No VPCs found in selected AWS region")
        for idx, vpc in enumerate(vpcs, start=1):
            cidr = vpc.get("CidrBlock")
            name = next((t.get("Value") for t in vpc.get("Tags", []) if t.get("Key") == "Name"), "")
            label = f"{vpc['VpcId']} ({cidr})"
            if name:
                label += f" - {name}"
            print(f"  {idx}. {label}")
        choice = input("Select AWS VPC (number or ID): ").strip()
        if choice.isdigit() and 1 <= int(choice) <= len(vpcs):
            args.aws_vpc_id = vpcs[int(choice) - 1]["VpcId"]
            args.aws_vpc_cidr = vpcs[int(choice) - 1].get("CidrBlock")
        else:
            args.aws_vpc_id = choice or vpcs[0]["VpcId"]
        print(f"AWS VPC selected: {args.aws_vpc_id}")

    if not args.aws_vpc_cidr:
        resp = ec2.describe_vpcs(VpcIds=[args.aws_vpc_id])
        if not resp.get("Vpcs"):
            raise RuntimeError(f"Unable to find VPC {args.aws_vpc_id}")
        args.aws_vpc_cidr = resp["Vpcs"][0].get("CidrBlock")
        print(f"Detected AWS CIDR: {args.aws_vpc_cidr}")

    if not args.skip_route_propagation and not propagate_cli_supplied:
        aws_subnet_items = list_aws_subnets(ec2, args.aws_vpc_id)
        if aws_subnet_items:
            selected_subnets, used_all = prompt_multi_select(
                "Select AWS subnets whose route tables should receive VGW propagation",
                aws_subnet_items,
                allow_all=True,
            )
            if used_all or len(selected_subnets) == len(aws_subnet_items):
                args.propagate_subnets = None
                print("Selected all AWS subnets for propagation.")
            else:
                subnet_ids = [subnet["SubnetId"] for subnet in selected_subnets]
                args.propagate_subnets = ",".join(subnet_ids)
                print("AWS subnets chosen:", ", ".join(subnet_ids))
        else:
            print("No subnets found in the VPC; will target all route tables.")
            args.propagate_subnets = None

    if not args.gcp_project:
        default_project = os.environ.get("GCP_PROJECT") or os.environ.get("GOOGLE_CLOUD_PROJECT")
        args.gcp_project = prompt_with_default("Enter GCP project ID", default_project)

    compute = None
    creds_path = os.environ.get("GOOGLE_APPLICATION_CREDENTIALS")
    if not creds_path:
        logger.info("GOOGLE_APPLICATION_CREDENTIALS not set; skipping automatic GCP discovery.")
    else:
        try:
            compute = get_compute_service()
        except Exception as exc:
            logger.warning(f"Could not initialize GCP client automatically: {exc}")

    if not args.gcp_region:
        region_value = None
        if compute and args.gcp_project:
            try:
                region_names = list_gcp_regions(compute, args.gcp_project)
                if region_names:
                    options = [{"label": name, "value": name} for name in region_names]
                    region_value = prompt_select_option(
                        "Select GCP region", options, allow_manual=True, default_value="asia-south1"
                    )
            except Exception as exc:
                logger.info(f"Could not list GCP regions automatically: {exc}")
        if region_value:
            args.gcp_region = region_value
        else:
            args.gcp_region = prompt_with_default("Enter GCP region", "asia-south1")

    selected_network_details = None
    if not args.gcp_network:
        if compute and args.gcp_project:
            try:
                networks = list_gcp_networks(compute, args.gcp_project)
            except Exception as exc:
                logger.info(f"Could not list GCP networks automatically: {exc}")
                networks = []
        else:
            networks = []

        if networks:
            options = []
            network_map = {}
            for net in networks:
                name = net.get("name")
                mode = "auto" if net.get("autoCreateSubnetworks") else "custom"
                options.append({"label": f"{name} ({mode})", "value": name})
                if name:
                    network_map[name] = net
            args.gcp_network = prompt_select_option(
                "Select GCP VPC network", options, allow_manual=True, default_value="default"
            )
            selected_network_details = network_map.get(args.gcp_network)
        else:
            args.gcp_network = prompt_with_default("Enter GCP VPC network name", "default")
    else:
        selected_network_details = None

    if not args.gcp_subnets:
        network_link = None
        if selected_network_details:
            network_link = selected_network_details.get("selfLink")
        elif compute and args.gcp_project and args.gcp_network:
            try:
                selected_network_details = compute.networks().get(
                    project=args.gcp_project, network=args.gcp_network
                ).execute()
                network_link = selected_network_details.get("selfLink")
            except Exception as exc:
                logger.info(f"Could not fetch network details: {exc}")
        if not gcp_subnets_cli_supplied and compute and network_link:
            gcp_subnet_objs = list_gcp_subnets(compute, args.gcp_project, network_link, args.gcp_region)
            if gcp_subnet_objs:
                items = []
                for subnet in gcp_subnet_objs:
                    region_name = subnet.get("region", "").split("/")[-1]
                    label = f"{subnet.get('name')} ({subnet.get('ipCidrRange')} | {region_name})"
                    items.append({"label": label, "value": subnet})
                selected_subnets, used_all = prompt_multi_select(
                    "Select GCP subnetworks to advertise to AWS", items, allow_all=True
                )
                if used_all or len(selected_subnets) == len(items):
                    cidrs = [
                        subnet.get("ipCidrRange")
                        for subnet in gcp_subnet_objs
                        if subnet.get("ipCidrRange")
                    ]
                else:
                    cidrs = [
                        subnet.get("ipCidrRange")
                        for subnet in selected_subnets
                        if subnet.get("ipCidrRange")
                    ]
                cidrs = [cidr for cidr in cidrs if cidr]
                if cidrs:
                    args.gcp_subnets = ",".join(cidrs)
                    print("Selected GCP CIDRs:", ", ".join(cidrs))
        if not args.gcp_subnets:
            args.gcp_subnets = ",".join(prompt_cidr_list("Enter GCP subnet CIDRs (comma separated)"))
    if args.aws_cgw_asn is None:
        args.aws_cgw_asn = prompt_int("Enter AWS VGW ASN", default=64513)
    if args.gcp_asn is None:
        args.gcp_asn = prompt_int("Enter GCP ASN (used for the AWS Customer Gateway)", default=64512)
    args.prefix = prompt_with_default(
        "Enter VPN resource name/prefix (used for AWS/GCP resources)",
        args.prefix or "classic-vpn",
    )


def parse_arguments():
    parser = argparse.ArgumentParser(
        description="Automate AWS-GCP Classic VPN setup",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("--aws-region", help="AWS region (e.g. ap-south-1)")
    parser.add_argument("--aws-vpc-id", help="AWS VPC ID")
    parser.add_argument("--aws-vpc-cidr", help="AWS VPC CIDR block")
    parser.add_argument("--aws-cgw-asn", type=int, help="AWS VGW ASN (required)")

    parser.add_argument("--gcp-project", help="GCP project ID")
    parser.add_argument("--gcp-network", help="GCP VPC network name")
    parser.add_argument("--gcp-region", help="GCP region")
    parser.add_argument("--gcp-subnets", help="Comma-separated GCP subnet CIDRs")
    parser.add_argument("--gcp-asn", type=int, help="GCP ASN to record for this deployment")

    parser.add_argument("--ike-version", type=int, default=1, choices=[1, 2], help="Classic tunnel IKE version")
    parser.add_argument("--prefix", default="classic-vpn", help="Resource name prefix")
    parser.add_argument("--propagate-subnets", help="Comma-separated AWS subnet IDs (or 'all'/'none')")
    parser.add_argument("--skip-route-propagation", action="store_true", help="Skip enabling VGW propagation")
    parser.add_argument("--check-status", action="store_true", help="Check GCP tunnel status only")
    return parser.parse_args()


def check_tunnel_status(compute, project: str, region: str, tunnel_names: List[str]):
    for tunnel_name in tunnel_names:
        try:
            tunnel = compute.vpnTunnels().get(project=project, region=region, vpnTunnel=tunnel_name).execute()
            status = tunnel.get("status", "UNKNOWN")
            details = tunnel.get("detailedStatus", "No details")
            logger.info(f"Tunnel {tunnel_name}: status={status} details={details}")
        except HttpError as exc:
            logger.error(f"Unable to fetch tunnel {tunnel_name}: {exc}")


def main():
    args = parse_arguments()
    interactive_setup(args)
    missing = [
        field
        for field in [
            "aws_region",
            "aws_vpc_id",
            "aws_vpc_cidr",
            "aws_cgw_asn",
            "gcp_project",
            "gcp_region",
            "gcp_network",
            "gcp_subnets",
            "gcp_asn",
        ]
        if not getattr(args, field)
    ]
    if missing:
        raise RuntimeError(f"Missing required inputs: {', '.join(missing)}")

    config = ClassicVPNConfig()
    config.aws_region = args.aws_region
    config.aws_vpc_id = args.aws_vpc_id
    config.aws_vpc_cidr = args.aws_vpc_cidr
    config.aws_vgw_asn = args.aws_cgw_asn
    config.gcp_project = args.gcp_project
    config.gcp_region = args.gcp_region
    config.gcp_network = args.gcp_network
    config.gcp_subnet_cidrs = [segment.strip() for segment in args.gcp_subnets.split(",") if segment.strip()]
    config.gcp_asn = args.gcp_asn
    config.tunnel_ike_version = args.ike_version

    prefix = args.prefix

    if args.check_status:
        compute = get_compute_service()
        tunnel_names = [f"{build_resource_names(prefix)['gcp_tunnel_prefix']}-{i}" for i in (1, 2)]
        check_tunnel_status(compute, config.gcp_project, config.gcp_region, tunnel_names)
        return

    enable_propagation = not args.skip_route_propagation
    propagate_ids = None
    if enable_propagation:
        propagate_ids = parse_subnet_ids(args.propagate_subnets)
    metadata = setup_classic_vpn(config, prefix, propagate_ids, enable_propagation)
    write_metadata(prefix, metadata)
    logger.info("Classic VPN setup complete. Verify AWS and GCP consoles for tunnel status.")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        logger.error("Interrupted by user")
        sys.exit(1)
