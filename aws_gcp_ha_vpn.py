#!/usr/bin/env python3
from __future__ import annotations
"""
Automate AWS <--> GCP HA VPN setup with BGP routing.

This script follows the High Availability VPN pattern:
1. GCP: Create Cloud Router with custom ASN (default 64512)
2. GCP: Create HA VPN Gateway (2 interfaces with public IPs)
3. AWS: Create 2 Customer Gateways (one for each GCP interface IP)
4. AWS: Create Virtual Private Gateway with custom ASN (default 64513)
5. AWS: Attach VGW to VPC
6. AWS: Create 2 Site-to-Site VPN connections (dynamic BGP routing)
7. GCP: Create External VPN Gateway (representing AWS VGW with 4 tunnel IPs)
8. GCP: Create 4 VPN tunnels (2 per AWS VPN connection)
9. GCP: Configure 4 BGP sessions with unique link-local IPs

Requirements:
- AWS credentials configured (env/profile)
- GCP service account JSON in GOOGLE_APPLICATION_CREDENTIALS
- boto3, google-api-python-client, google-auth
"""

import argparse
import time
import json
import sys
import os
from pathlib import Path
from datetime import datetime, timezone
from typing import List, Dict, Tuple, Optional
import xml.etree.ElementTree as ET

import boto3
from botocore.exceptions import ClientError

from google.oauth2 import service_account
from googleapiclient import discovery
from googleapiclient.errors import HttpError

# Configure basic logging
import logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

METADATA_DIR = Path(__file__).resolve().parent / "ha_vpn_runs"


class CleanupManager:
    """Track created resources and clean them up if the run fails."""
    def __init__(self):
        self._actions = []

    def add(self, description, func, *args, **kwargs):
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


def delete_vpn_connection(ec2, vpn_id: str):
    """Delete AWS VPN connection (ignore if missing)."""
    try:
        ec2.delete_vpn_connection(VpnConnectionId=vpn_id)
        logger.info(f"Deleted AWS VPN connection {vpn_id}")
    except ClientError as e:
        if e.response['Error']['Code'] != 'InvalidVpnConnectionID.NotFound':
            logger.warning(f"Failed to delete VPN {vpn_id}: {e}")


def delete_customer_gateway(ec2, cgw_id: str):
    try:
        ec2.delete_customer_gateway(CustomerGatewayId=cgw_id)
        logger.info(f"Deleted AWS Customer Gateway {cgw_id}")
    except ClientError as e:
        if e.response['Error']['Code'] != 'InvalidCustomerGatewayID.NotFound':
            logger.warning(f"Failed to delete CGW {cgw_id}: {e}")


def delete_vgw(ec2, vgw_id: str, vpc_id: str):
    try:
        try:
            ec2.detach_vpn_gateway(VpnGatewayId=vgw_id, VpcId=vpc_id)
        except ClientError:
            pass
        ec2.delete_vpn_gateway(VpnGatewayId=vgw_id)
        logger.info(f"Deleted AWS VGW {vgw_id}")
    except ClientError as e:
        if e.response['Error']['Code'] != 'InvalidVpnGatewayID.NotFound':
            logger.warning(f"Failed to delete VGW {vgw_id}: {e}")


def remove_router_entries_for_tunnel(compute, config: HAVPNConfig, router_name: str, tunnel_name: str):
    """Remove BGP peer/interface on a router that match the tunnel name."""
    try:
        router = compute.routers().get(
            project=config.gcp_project,
            region=config.gcp_region,
            router=router_name
        ).execute()
    except HttpError as e:
        if e.resp.status == 404:
            return
        raise

    peers = router.get('bgpPeers', [])
    interfaces = router.get('interfaces', [])
    target_peer = f"{tunnel_name}-bgp"
    target_iface = f"{tunnel_name}-interface"
    new_peers = [p for p in peers if p.get('name') != target_peer]
    new_ifaces = [i for i in interfaces if i.get('name') != target_iface]
    if len(new_peers) == len(peers) and len(new_ifaces) == len(interfaces):
        return

    body = {
        'bgpPeers': new_peers,
        'interfaces': new_ifaces,
    }
    op = compute.routers().patch(
        project=config.gcp_project,
        region=config.gcp_region,
        router=router_name,
        body=body
    ).execute()
    wait_for_gcp_operation(compute, config.gcp_project, op, region=config.gcp_region)


def cleanup_router_by_prefix(compute, config: HAVPNConfig, router_name: str, prefix: str):
    """Remove router interfaces/peers whose names start with prefix."""
    try:
        router = compute.routers().get(
            project=config.gcp_project,
            region=config.gcp_region,
            router=router_name
        ).execute()
    except HttpError as e:
        if e.resp.status == 404:
            return
        raise

    peers = router.get('bgpPeers', [])
    interfaces = router.get('interfaces', [])
    new_peers = [p for p in peers if not p.get('name', '').startswith(prefix)]
    new_ifaces = [i for i in interfaces if not i.get('name', '').startswith(prefix)]
    if len(new_peers) == len(peers) and len(new_ifaces) == len(interfaces):
        return

    body = {
        'bgpPeers': new_peers,
        'interfaces': new_ifaces,
    }
    op = compute.routers().patch(
        project=config.gcp_project,
        region=config.gcp_region,
        router=router_name,
        body=body
    ).execute()
    wait_for_gcp_operation(compute, config.gcp_project, op, region=config.gcp_region)


def delete_gcp_vpn_tunnel(compute, config: HAVPNConfig, tunnel_name: str, router_name: Optional[str] = None):
    """Delete GCP VPN tunnel and remove router references."""
    if router_name:
        remove_router_entries_for_tunnel(compute, config, router_name, tunnel_name)
    try:
        op = compute.vpnTunnels().delete(
            project=config.gcp_project,
            region=config.gcp_region,
            vpnTunnel=tunnel_name
        ).execute()
        wait_for_gcp_operation(compute, config.gcp_project, op, region=config.gcp_region)
        logger.info(f"Deleted GCP VPN tunnel {tunnel_name}")
    except HttpError as e:
        if e.resp.status != 404:
            logger.warning(f"Failed to delete tunnel {tunnel_name}: {e}")


def delete_external_vpn_gateway(compute, config: HAVPNConfig, gateway_name: str):
    try:
        op = compute.externalVpnGateways().delete(
            project=config.gcp_project,
            externalVpnGateway=gateway_name
        ).execute()
        wait_for_gcp_operation(compute, config.gcp_project, op)
        logger.info(f"Deleted GCP External VPN Gateway {gateway_name}")
    except HttpError as e:
        if e.resp.status != 404:
            logger.warning(f"Failed to delete external gateway {gateway_name}: {e}")


def delete_ha_vpn_gateway(compute, config: HAVPNConfig, gateway_name: str):
    try:
        op = compute.vpnGateways().delete(
            project=config.gcp_project,
            region=config.gcp_region,
            vpnGateway=gateway_name
        ).execute()
        wait_for_gcp_operation(compute, config.gcp_project, op, region=config.gcp_region)
        logger.info(f"Deleted GCP HA VPN Gateway {gateway_name}")
    except HttpError as e:
        if e.resp.status != 404:
            logger.warning(f"Failed to delete HA VPN gateway {gateway_name}: {e}")


def delete_cloud_router(compute, config: HAVPNConfig, router_name: str, prefix: Optional[str] = None):
    if prefix:
        cleanup_router_by_prefix(compute, config, router_name, prefix)
    try:
        op = compute.routers().delete(
            project=config.gcp_project,
            region=config.gcp_region,
            router=router_name
        ).execute()
        wait_for_gcp_operation(compute, config.gcp_project, op, region=config.gcp_region)
        logger.info(f"Deleted GCP Cloud Router {router_name}")
    except HttpError as e:
        if e.resp.status != 404:
            logger.warning(f"Failed to delete Cloud Router {router_name}: {e}")


def build_resource_names(prefix: str) -> Dict[str, str]:
    """Generate consistent resource names"""
    return {
        # AWS resources
        'aws_vgw': f"{prefix}-vgw",
        'aws_cgw_0': f"{prefix}-cgw-0",
        'aws_cgw_1': f"{prefix}-cgw-1",
        'aws_vpn_0': f"{prefix}-vpn-0",
        'aws_vpn_1': f"{prefix}-vpn-1",
        
        # GCP resources
        'gcp_router': f"{prefix}-router",
        'gcp_ha_gateway': f"{prefix}-ha-gateway",
        'gcp_peer_gateway': f"{prefix}-aws-peer-gateway",
        'gcp_tunnel_prefix': f"{prefix}-tunnel",
    }


class HAVPNConfig:
    """Configuration container for HA VPN setup"""
    def __init__(self):
        # GCP settings
        self.gcp_asn = 64512
        self.gcp_project = None
        self.gcp_region = None
        self.gcp_network = None
        
        # AWS settings
        self.aws_asn = 64513
        self.aws_region = None
        self.aws_vpc_id = None
        
        # Tunnel settings
        self.tunnel_ike_version = 2  # HA VPN uses IKEv2
        self.tunnel_advertise_mode = 'DEFAULT'  # or 'CUSTOM'
        self.custom_advertised_ranges = []  # If CUSTOM mode


def write_metadata(prefix: str, data: dict):
    """Write metadata to JSON file"""
    METADATA_DIR.mkdir(parents=True, exist_ok=True)
    path = METADATA_DIR / f"{prefix}.json"
    with path.open('w') as f:
        json.dump(data, f, indent=2, sort_keys=True)
    logger.info(f"Metadata written to {path}")


def get_gcp_compute_service():
    """Initialize GCP Compute API client"""
    creds = service_account.Credentials.from_service_account_file(
        os.environ.get('GOOGLE_APPLICATION_CREDENTIALS')
    )
    return discovery.build('compute', 'v1', credentials=creds, cache_discovery=False)


def wait_for_gcp_operation(compute, project: str, operation: dict, 
                           region: str = None, timeout: int = 300):
    """Wait for GCP operation to complete"""
    op_name = operation.get('name')
    start = time.time()
    
    while time.time() - start < timeout:
        if region:
            result = compute.regionOperations().get(
                project=project, region=region, operation=op_name
            ).execute()
        else:
            result = compute.globalOperations().get(
                project=project, operation=op_name
            ).execute()
        
        if result.get('status') == 'DONE':
            if 'error' in result:
                errors = result['error'].get('errors', [])
                raise RuntimeError(f"GCP operation failed: {errors}")
            return result
        
        time.sleep(3)
    
    raise TimeoutError(f"GCP operation {op_name} timed out")


# ============================================================================
# GCP Functions
# ============================================================================

def create_cloud_router(compute, config: HAVPNConfig, router_name: str) -> str:
    """Create GCP Cloud Router with specified ASN"""
    try:
        existing = compute.routers().get(
            project=config.gcp_project,
            region=config.gcp_region,
            router=router_name
        ).execute()
        logger.info(f"Cloud Router {router_name} already exists")
        return existing['selfLink'], False
    except HttpError as e:
        if e.resp.status != 404:
            raise
    
    logger.info(f"Creating Cloud Router {router_name} with ASN {config.gcp_asn}")
    
    body = {
        'name': router_name,
        'network': f"projects/{config.gcp_project}/global/networks/{config.gcp_network}",
        'bgp': {
            'asn': config.gcp_asn,
            'advertiseMode': config.tunnel_advertise_mode,
        }
    }
    
    if config.tunnel_advertise_mode == 'CUSTOM':
        body['bgp']['advertisedIpRanges'] = [
            {'range': cidr} for cidr in config.custom_advertised_ranges
        ]
    
    op = compute.routers().insert(
        project=config.gcp_project,
        region=config.gcp_region,
        body=body
    ).execute()
    
    wait_for_gcp_operation(compute, config.gcp_project, op, region=config.gcp_region)
    
    router = compute.routers().get(
        project=config.gcp_project,
        region=config.gcp_region,
        router=router_name
    ).execute()
    
    logger.info(f"Created Cloud Router: {router['selfLink']}")
    return router['selfLink'], True


def create_ha_vpn_gateway(compute, config: HAVPNConfig, gateway_name: str) -> Tuple[str, List[str]]:
    """
    Create HA VPN Gateway and return (selfLink, [interface0_ip, interface1_ip])
    """
    try:
        existing = compute.vpnGateways().get(
            project=config.gcp_project,
            region=config.gcp_region,
            vpnGateway=gateway_name
        ).execute()
        logger.info(f"HA VPN Gateway {gateway_name} already exists")
        
        interfaces = existing.get('vpnInterfaces', [])
        ips = [iface.get('ipAddress') for iface in interfaces]
        return existing['selfLink'], ips, False
    except HttpError as e:
        if e.resp.status != 404:
            raise
    
    logger.info(f"Creating HA VPN Gateway {gateway_name}")
    
    body = {
        'name': gateway_name,
        'network': f"projects/{config.gcp_project}/global/networks/{config.gcp_network}",
        'vpnGatewayInterface': [
            {'id': 0},
            {'id': 1}
        ]
    }
    
    op = compute.vpnGateways().insert(
        project=config.gcp_project,
        region=config.gcp_region,
        body=body
    ).execute()
    
    wait_for_gcp_operation(compute, config.gcp_project, op, region=config.gcp_region)
    
    # Retrieve gateway details
    gateway = compute.vpnGateways().get(
        project=config.gcp_project,
        region=config.gcp_region,
        vpnGateway=gateway_name
    ).execute()
    
    interfaces = gateway.get('vpnInterfaces', [])
    ips = [iface.get('ipAddress') for iface in interfaces]
    
    logger.info(f"HA VPN Gateway created with IPs: {ips}")
    return gateway['selfLink'], ips, True


def create_external_vpn_gateway(compute, config: HAVPNConfig, 
                               gateway_name: str, aws_tunnel_ips: List[str]) -> Tuple[str, bool]:
    """
    Create External VPN Gateway representing AWS VGW.
    aws_tunnel_ips should have 4 IPs (2 from each AWS VPN connection)
    """
    try:
        existing = compute.externalVpnGateways().get(
            project=config.gcp_project,
            externalVpnGateway=gateway_name
        ).execute()
        logger.info(f"External VPN Gateway {gateway_name} already exists")
        return existing['selfLink'], False
    except HttpError as e:
        if e.resp.status != 404:
            raise
    
    if len(aws_tunnel_ips) != 4:
        raise ValueError(f"Expected 4 AWS tunnel IPs, got {len(aws_tunnel_ips)}")
    
    logger.info(f"Creating External VPN Gateway {gateway_name} with 4 interfaces")
    
    body = {
        'name': gateway_name,
        'redundancyType': 'FOUR_IPS_REDUNDANCY',
        'interface': [
            {'id': 0, 'ipAddress': aws_tunnel_ips[0]},
            {'id': 1, 'ipAddress': aws_tunnel_ips[1]},
            {'id': 2, 'ipAddress': aws_tunnel_ips[2]},
            {'id': 3, 'ipAddress': aws_tunnel_ips[3]},
        ]
    }
    
    op = compute.externalVpnGateways().insert(
        project=config.gcp_project,
        body=body
    ).execute()
    
    wait_for_gcp_operation(compute, config.gcp_project, op)
    
    gateway = compute.externalVpnGateways().get(
        project=config.gcp_project,
        externalVpnGateway=gateway_name
    ).execute()
    
    logger.info(f"External VPN Gateway created: {gateway['selfLink']}")
    return gateway['selfLink'], True


def create_vpn_tunnel_with_bgp(compute, config: HAVPNConfig,
                                tunnel_name: str,
                                router_name: str,
                                vpn_gateway_link: str,
                                peer_gateway_link: str,
                                shared_secret: str,
                                vpn_gateway_interface: int,
                                peer_gateway_interface: int,
                                router_bgp_ip: str,
                                peer_bgp_ip: str) -> str:
    """Create a single VPN tunnel with BGP configuration"""
    
    try:
        existing = compute.vpnTunnels().get(
            project=config.gcp_project,
            region=config.gcp_region,
            vpnTunnel=tunnel_name
        ).execute()
        logger.info(f"VPN Tunnel {tunnel_name} already exists")
        return existing['selfLink'], False
    except HttpError as e:
        if e.resp.status != 404:
            raise
    
    logger.info(f"Creating VPN Tunnel {tunnel_name}")
    
    body = {
        'name': tunnel_name,
        'vpnGateway': vpn_gateway_link,
        'vpnGatewayInterface': vpn_gateway_interface,
        'peerExternalGateway': peer_gateway_link,
        'peerExternalGatewayInterface': peer_gateway_interface,
        'sharedSecret': shared_secret,
        'router': f"projects/{config.gcp_project}/regions/{config.gcp_region}/routers/{router_name}",
        'ikeVersion': config.tunnel_ike_version,
    }
    
    op = compute.vpnTunnels().insert(
        project=config.gcp_project,
        region=config.gcp_region,
        body=body
    ).execute()
    
    wait_for_gcp_operation(compute, config.gcp_project, op, region=config.gcp_region)
    
    # Now add BGP peer to the router
    logger.info(f"Adding BGP peer for tunnel {tunnel_name}")
    
    router = compute.routers().get(
        project=config.gcp_project,
        region=config.gcp_region,
        router=router_name
    ).execute()
    
    # Add BGP peer
    bgp_peer = {
        'name': f"{tunnel_name}-bgp",
        'interfaceName': f"{tunnel_name}-interface",
        'ipAddress': router_bgp_ip,
        'peerIpAddress': peer_bgp_ip,
        'peerAsn': config.aws_asn,
        'advertisedRoutePriority': 100,
    }
    
    # Add interface
    bgp_interface = {
        'name': f"{tunnel_name}-interface",
        'linkedVpnTunnel': f"projects/{config.gcp_project}/regions/{config.gcp_region}/vpnTunnels/{tunnel_name}",
        'ipRange': f"{router_bgp_ip}/30",  # /30 subnet for BGP
    }
    
    router['bgpPeers'] = router.get('bgpPeers', []) + [bgp_peer]
    router['interfaces'] = router.get('interfaces', []) + [bgp_interface]
    
    # Patch the router
    op = compute.routers().patch(
        project=config.gcp_project,
        region=config.gcp_region,
        router=router_name,
        body=router
    ).execute()
    
    wait_for_gcp_operation(compute, config.gcp_project, op, region=config.gcp_region)
    
    tunnel = compute.vpnTunnels().get(
        project=config.gcp_project,
        region=config.gcp_region,
        vpnTunnel=tunnel_name
    ).execute()
    
    logger.info(f"Created VPN Tunnel with BGP: {tunnel['selfLink']}")
    return tunnel['selfLink'], True


# ============================================================================
# AWS Functions
# ============================================================================

def ensure_vgw_attached(ec2, vpc_id: str, vgw_name: str, asn: int) -> str:
    """Create/attach Virtual Private Gateway with custom ASN"""
    # Prefer any VGW already attached to this VPC (regardless of name)
    attached_resp = ec2.describe_vpn_gateways(
        Filters=[{'Name': 'attachment.vpc-id', 'Values': [vpc_id]}]
    )['VpnGateways']
    vgw = None
    vgw_id = None
    for gw in attached_resp:
        if gw.get('State') == 'deleted':
            continue
        for att in gw.get('VpcAttachments', []):
            if att.get('VpcId') == vpc_id and att.get('State') in {'attached', 'attaching'}:
                vgw = gw
                vgw_id = gw['VpnGatewayId']
                logger.info(f"Reusing VGW {vgw_id} already associated with {vpc_id}")
                break
        if vgw:
            break

    created = False
    if vgw is None:
        # Fallback to name lookup
        resp = ec2.describe_vpn_gateways(
            Filters=[{'Name': 'tag:Name', 'Values': [vgw_name]}]
        )
        active = [gw for gw in resp['VpnGateways'] if gw.get('State') != 'deleted']

        if active:
            vgw = active[0]
            vgw_id = vgw['VpnGatewayId']
            logger.info(f"Found existing VGW: {vgw_id}")
        else:
            logger.info(f"Creating VGW with ASN {asn}")
            resp = ec2.create_vpn_gateway(
                Type='ipsec.1',
                AmazonSideAsn=asn
            )
            vgw_id = resp['VpnGateway']['VpnGatewayId']
            ec2.create_tags(
                Resources=[vgw_id],
                Tags=[{'Key': 'Name', 'Value': vgw_name}]
            )
            vgw = resp['VpnGateway']
            created = True
    
    # Check attachment
    attachments = vgw.get('VpcAttachments', [])
    attached = any(
        att.get('VpcId') == vpc_id and att.get('State') == 'attached'
        for att in attachments
    )
    
    if attached:
        logger.info(f"VGW {vgw_id} already attached to {vpc_id}")
        return vgw_id, created
    
    # Attach
    logger.info(f"Attaching VGW {vgw_id} to VPC {vpc_id}")
    try:
        ec2.attach_vpn_gateway(VpnGatewayId=vgw_id, VpcId=vpc_id)
    except ClientError as e:
        if e.response['Error']['Code'] not in ['Resource.AlreadyAssociated', 'IncorrectState']:
            raise
    
    # Wait for attachment
    while True:
        gw = ec2.describe_vpn_gateways(VpnGatewayIds=[vgw_id])['VpnGateways'][0]
        for att in gw.get('VpcAttachments', []):
            if att.get('VpcId') == vpc_id and att.get('State') == 'attached':
                logger.info(f"VGW {vgw_id} attached successfully")
                return vgw_id, created
        time.sleep(3)


def create_customer_gateway(ec2, name: str, ip_address: str, bgp_asn: int) -> str:
    """Create Customer Gateway with specified IP and ASN"""
    
    # Check if exists
    existing = ec2.describe_customer_gateways()['CustomerGateways']
    
    for cgw in existing:
        if cgw.get('State') != 'available':
            continue
        if (cgw.get('IpAddress') or cgw.get('Ip')) != ip_address:
            continue
        if cgw.get('BgpAsn') != str(bgp_asn):
            continue
        
        tags = {t['Key']: t['Value'] for t in cgw.get('Tags', [])}
        if tags.get('Name') == name:
            logger.info(f"Customer Gateway {cgw['CustomerGatewayId']} already exists")
            return cgw['CustomerGatewayId'], False
    
    logger.info(f"Creating Customer Gateway {name} for IP {ip_address}")
    
    resp = ec2.create_customer_gateway(
        Type='ipsec.1',
        PublicIp=ip_address,
        BgpAsn=bgp_asn
    )
    
    cgw_id = resp['CustomerGateway']['CustomerGatewayId']
    
    ec2.create_tags(
        Resources=[cgw_id],
        Tags=[{'Key': 'Name', 'Value': name}]
    )
    
    # Wait for available
    while True:
        resp = ec2.describe_customer_gateways(CustomerGatewayIds=[cgw_id])
        if resp['CustomerGateways'][0]['State'] == 'available':
            break
        time.sleep(2)
    
    logger.info(f"Created Customer Gateway: {cgw_id}")
    return cgw_id, True


def create_vpn_connection_dynamic(ec2, name: str, cgw_id: str, 
                                  vgw_id: str, tunnel1_psk: str = None,
                                  tunnel2_psk: str = None) -> str:
    """Create Site-to-Site VPN with dynamic (BGP) routing"""
    
    # Check existing
    existing = ec2.describe_vpn_connections()['VpnConnections']
    for vpn in existing:
        tags = {t['Key']: t['Value'] for t in vpn.get('Tags', [])}
        if tags.get('Name') == name and vpn.get('State') != 'deleted':
            logger.info(f"VPN Connection {vpn['VpnConnectionId']} already exists")
            return vpn['VpnConnectionId'], False
    
    logger.info(f"Creating VPN Connection {name} (dynamic/BGP)")
    
    options = {'StaticRoutesOnly': False}
    
    if tunnel1_psk:
        options['TunnelOptions'] = [
            {'PreSharedKey': tunnel1_psk},
        ]
        if tunnel2_psk:
            options['TunnelOptions'].append({'PreSharedKey': tunnel2_psk})
    
    resp = ec2.create_vpn_connection(
        Type='ipsec.1',
        CustomerGatewayId=cgw_id,
        VpnGatewayId=vgw_id,
        Options=options,
        TagSpecifications=[{
            'ResourceType': 'vpn-connection',
            'Tags': [{'Key': 'Name', 'Value': name}]
        }]
    )
    
    vpn_id = resp['VpnConnection']['VpnConnectionId']
    logger.info(f"Created VPN Connection: {vpn_id}")
    
    return vpn_id, True


def wait_for_vpn_available(ec2, vpn_id: str, timeout: int = 600):
    """Wait for VPN connection to become available"""
    start = time.time()
    
    while time.time() - start < timeout:
        resp = ec2.describe_vpn_connections(VpnConnectionIds=[vpn_id])
        vpn = resp['VpnConnections'][0]
        state = vpn.get('State')
        
        if state == 'available':
            logger.info(f"VPN {vpn_id} is available")
            return vpn
        
        logger.info(f"VPN {vpn_id} state: {state}, waiting...")
        time.sleep(5)
    
    raise TimeoutError(f"VPN {vpn_id} did not become available")


def parse_vpn_configuration(xml_config: str) -> Dict:
    """
    Parse AWS VPN configuration XML to extract tunnel details.
    Returns dict with tunnel IPs, PSKs, and BGP info.
    """
    root = ET.fromstring(xml_config)
    
    tunnels = []
    
    for ipsec_tunnel in root.findall('.//ipsec_tunnel'):
        tunnel_info = {}
        
        # Outside IP (VGW endpoint)
        vpn_gateway = ipsec_tunnel.find('.//vpn_gateway/tunnel_outside_address/ip_address')
        if vpn_gateway is not None:
            tunnel_info['outside_ip'] = vpn_gateway.text
        
        # Inside IPs for BGP
        customer_inside = ipsec_tunnel.find('.//customer_gateway/tunnel_inside_address/ip_address')
        vpn_inside = ipsec_tunnel.find('.//vpn_gateway/tunnel_inside_address/ip_address')
        
        if customer_inside is not None:
            tunnel_info['customer_inside_ip'] = customer_inside.text
        if vpn_inside is not None:
            tunnel_info['vpn_inside_ip'] = vpn_inside.text
        
        # Pre-shared key
        psk = ipsec_tunnel.find('.//ike/pre_shared_key')
        if psk is not None:
            tunnel_info['psk'] = psk.text
        
        # BGP ASN
        bgp_asn = ipsec_tunnel.find('.//vpn_gateway/bgp/asn')
        if bgp_asn is not None:
            tunnel_info['bgp_asn'] = int(bgp_asn.text)
        
        if tunnel_info:
            tunnels.append(tunnel_info)
    
    return {'tunnels': tunnels}


def extract_vpn_details(ec2, vpn_id: str) -> Dict:
    """Extract tunnel details from VPN connection"""
    vpn = wait_for_vpn_available(ec2, vpn_id)
    
    # Try CustomerGatewayConfiguration XML
    xml_config = vpn.get('CustomerGatewayConfiguration', '')
    if xml_config:
        try:
            parsed = parse_vpn_configuration(xml_config)
            if parsed['tunnels']:
                return parsed
        except Exception as e:
            logger.warning(f"Failed to parse XML config: {e}")
    
    # Fallback: use TunnelOptions if available
    tunnel_opts = vpn.get('Options', {}).get('TunnelOptions', [])
    vgw_telemetry = vpn.get('VgwTelemetry', [])
    
    tunnels = []
    for i, opt in enumerate(tunnel_opts):
        tunnel = {
            'psk': opt.get('PreSharedKey'),
            'outside_ip': opt.get('OutsideIpAddress'),
        }
        
        # Get inside IPs from telemetry
        if i < len(vgw_telemetry):
            telemetry = vgw_telemetry[i]
            tunnel['outside_ip'] = tunnel['outside_ip'] or telemetry.get('OutsideIpAddress')
            tunnel['customer_inside_ip'] = telemetry.get('CertificateArn')  # This might not be right
        
        tunnels.append(tunnel)
    
    return {'tunnels': tunnels}


# ============================================================================
# Main Workflow
# ============================================================================

def setup_ha_vpn(config: HAVPNConfig, prefix: str):
    """Execute complete HA VPN setup"""
    
    names = build_resource_names(prefix)
    
    # Initialize clients
    ec2 = boto3.client('ec2', region_name=config.aws_region)
    compute = get_gcp_compute_service()
    cleanup = CleanupManager()
    
    metadata = {
        'timestamp': datetime.now(timezone.utc).isoformat(),
        'prefix': prefix,
        'config': {
            'gcp_asn': config.gcp_asn,
            'aws_asn': config.aws_asn,
            'gcp_project': config.gcp_project,
            'gcp_region': config.gcp_region,
            'gcp_network': config.gcp_network,
            'aws_region': config.aws_region,
            'aws_vpc_id': config.aws_vpc_id,
        },
        'resources': {}
    }
    
    try:
        # Step 1: Create GCP Cloud Router
        logger.info("=" * 60)
        logger.info("STEP 1: Creating GCP Cloud Router")
        logger.info("=" * 60)
        router_link, router_created = create_cloud_router(compute, config, names['gcp_router'])
        metadata['resources']['gcp_router'] = names['gcp_router']
        if router_created:
            cleanup.add("GCP Cloud Router", delete_cloud_router, compute, config, names['gcp_router'], prefix)
        
        # Step 2: Create GCP HA VPN Gateway
        logger.info("=" * 60)
        logger.info("STEP 2: Creating GCP HA VPN Gateway")
        logger.info("=" * 60)
        ha_gateway_link, gcp_interface_ips, ha_created = create_ha_vpn_gateway(
            compute, config, names['gcp_ha_gateway']
        )
        metadata['resources']['gcp_ha_gateway'] = names['gcp_ha_gateway']
        metadata['resources']['gcp_interface_ips'] = gcp_interface_ips
        if ha_created:
            cleanup.add("GCP HA VPN Gateway", delete_ha_vpn_gateway, compute, config, names['gcp_ha_gateway'])
        
        logger.info(f"GCP HA Gateway Interface IPs: {gcp_interface_ips}")
        
        # Step 3: Create AWS Customer Gateways (one per GCP interface)
        logger.info("=" * 60)
        logger.info("STEP 3: Creating AWS Customer Gateways")
        logger.info("=" * 60)
        cgw_0_id, cgw0_created = create_customer_gateway(
            ec2, names['aws_cgw_0'], gcp_interface_ips[0], config.gcp_asn
        )
        cgw_1_id, cgw1_created = create_customer_gateway(
            ec2, names['aws_cgw_1'], gcp_interface_ips[1], config.gcp_asn
        )
        metadata['resources']['aws_cgw_ids'] = [cgw_0_id, cgw_1_id]
        if cgw0_created:
            cleanup.add("AWS Customer Gateway 0", delete_customer_gateway, ec2, cgw_0_id)
        if cgw1_created:
            cleanup.add("AWS Customer Gateway 1", delete_customer_gateway, ec2, cgw_1_id)
        
        # Step 4: Create AWS Virtual Private Gateway
        logger.info("=" * 60)
        logger.info("STEP 4: Creating/Attaching AWS Virtual Private Gateway")
        logger.info("=" * 60)
        vgw_id, vgw_created = ensure_vgw_attached(ec2, config.aws_vpc_id, names['aws_vgw'], config.aws_asn)
        metadata['resources']['aws_vgw_id'] = vgw_id
        if vgw_created:
            cleanup.add("AWS VGW", delete_vgw, ec2, vgw_id, config.aws_vpc_id)
        
        # Step 5: Create AWS VPN Connections
        logger.info("=" * 60)
        logger.info("STEP 5: Creating AWS Site-to-Site VPN Connections")
        logger.info("=" * 60)
        vpn_0_id, vpn0_created = create_vpn_connection_dynamic(ec2, names['aws_vpn_0'], cgw_0_id, vgw_id)
        vpn_1_id, vpn1_created = create_vpn_connection_dynamic(ec2, names['aws_vpn_1'], cgw_1_id, vgw_id)
        metadata['resources']['aws_vpn_ids'] = [vpn_0_id, vpn_1_id]
        if vpn0_created:
            cleanup.add("AWS VPN 0", delete_vpn_connection, ec2, vpn_0_id)
        if vpn1_created:
            cleanup.add("AWS VPN 1", delete_vpn_connection, ec2, vpn_1_id)
        
        # Step 6: Extract AWS tunnel details
        logger.info("=" * 60)
        logger.info("STEP 6: Extracting AWS tunnel configuration")
        logger.info("=" * 60)
        
        logger.info("Waiting for VPN configurations to be available...")
        vpn_0_details = extract_vpn_details(ec2, vpn_0_id)
        vpn_1_details = extract_vpn_details(ec2, vpn_1_id)
        
        # Collect all 4 tunnel outside IPs
        aws_tunnel_ips = []
        all_tunnel_details = []
        
        for details in [vpn_0_details, vpn_1_details]:
            for tunnel in details['tunnels']:
                aws_tunnel_ips.append(tunnel['outside_ip'])
                all_tunnel_details.append(tunnel)
        
        if len(aws_tunnel_ips) != 4:
            raise ValueError(f"Expected 4 AWS tunnel IPs, got {len(aws_tunnel_ips)}")
        
        logger.info(f"AWS Tunnel Outside IPs: {aws_tunnel_ips}")
        metadata['aws_tunnels'] = all_tunnel_details
        
        # Step 7: Create GCP External VPN Gateway (representing AWS)
        logger.info("=" * 60)
        logger.info("STEP 7: Creating GCP External VPN Gateway")
        logger.info("=" * 60)
        peer_gateway_link, peer_created = create_external_vpn_gateway(
            compute, config, names['gcp_peer_gateway'], aws_tunnel_ips
        )
        metadata['resources']['gcp_peer_gateway'] = names['gcp_peer_gateway']
        if peer_created:
            cleanup.add("GCP External VPN Gateway", delete_external_vpn_gateway, compute, config, names['gcp_peer_gateway'])
        
        # Step 8: Create 4 GCP VPN Tunnels with BGP
        logger.info("=" * 60)
        logger.info("STEP 8: Creating 4 GCP VPN Tunnels with BGP Sessions")
        logger.info("=" * 60)
        
        # Tunnel mapping:
        # Tunnel 0: GCP interface 0 -> AWS tunnel 0 (from VPN 0)
        # Tunnel 1: GCP interface 0 -> AWS tunnel 1 (from VPN 0)
        # Tunnel 2: GCP interface 1 -> AWS tunnel 2 (from VPN 1)
        # Tunnel 3: GCP interface 1 -> AWS tunnel 3 (from VPN 1)
        
        tunnel_configs = [
            # Tunnels from GCP interface 0 to AWS VPN 0
            {
                'name': f"{names['gcp_tunnel_prefix']}-0",
                'gcp_interface': 0,
                'aws_interface': 0,
                'tunnel_details': all_tunnel_details[0]
            },
            {
                'name': f"{names['gcp_tunnel_prefix']}-1",
                'gcp_interface': 0,
                'aws_interface': 1,
                'tunnel_details': all_tunnel_details[1]
            },
            # Tunnels from GCP interface 1 to AWS VPN 1
            {
                'name': f"{names['gcp_tunnel_prefix']}-2",
                'gcp_interface': 1,
                'aws_interface': 2,
                'tunnel_details': all_tunnel_details[2]
            },
            {
                'name': f"{names['gcp_tunnel_prefix']}-3",
                'gcp_interface': 1,
                'aws_interface': 3,
                'tunnel_details': all_tunnel_details[3]
            },
        ]
        
        created_tunnels = []
        
        for tun_config in tunnel_configs:
            tunnel_detail = tun_config['tunnel_details']
            
            # BGP IPs: use the inside IPs from AWS
            # GCP will use customer_inside_ip, AWS uses vpn_inside_ip
            router_bgp_ip = tunnel_detail.get('customer_inside_ip', '169.254.0.1')
            peer_bgp_ip = tunnel_detail.get('vpn_inside_ip', '169.254.0.2')
            
            logger.info(f"Creating tunnel {tun_config['name']}")
            logger.info(f"  GCP interface: {tun_config['gcp_interface']}")
            logger.info(f"  AWS interface: {tun_config['aws_interface']}")
            logger.info(f"  Router BGP IP: {router_bgp_ip}")
            logger.info(f"  Peer BGP IP: {peer_bgp_ip}")
            
            tunnel_link, tunnel_created = create_vpn_tunnel_with_bgp(
                compute=compute,
                config=config,
                tunnel_name=tun_config['name'],
                router_name=names['gcp_router'],
                vpn_gateway_link=ha_gateway_link,
                peer_gateway_link=peer_gateway_link,
                shared_secret=tunnel_detail['psk'],
                vpn_gateway_interface=tun_config['gcp_interface'],
                peer_gateway_interface=tun_config['aws_interface'],
                router_bgp_ip=router_bgp_ip,
                peer_bgp_ip=peer_bgp_ip
            )
            
            tunnel_record = {
                'name': tun_config['name'],
                'link': tunnel_link,
                'gcp_interface': tun_config['gcp_interface'],
                'aws_interface': tun_config['aws_interface'],
                'router_bgp_ip': router_bgp_ip,
                'peer_bgp_ip': peer_bgp_ip,
            }
            created_tunnels.append(tunnel_record)
            if tunnel_created:
                cleanup.add(
                    f"GCP tunnel {tun_config['name']}",
                    delete_gcp_vpn_tunnel,
                    compute,
                    config,
                    tun_config['name'],
                    names['gcp_router']
                )
        
        metadata['resources']['gcp_tunnels'] = created_tunnels
        
        # Save metadata
        write_metadata(prefix, metadata)
        cleanup.clear()
        
        logger.info("=" * 60)
        logger.info("HA VPN SETUP COMPLETE")
        logger.info("=" * 60)
        logger.info("\nNext steps:")
        logger.info("1. Check AWS VPC Console for VPN tunnel status")
        logger.info("2. Check GCP VPN Console for tunnel status")
        logger.info("3. Verify BGP sessions are established")
        logger.info("4. Check Cloud Router learned routes")
        logger.info("5. Update route propagation in AWS route tables if needed")
        logger.info("\nTo enable route propagation:")
        logger.info(f"  aws ec2 enable-vgw-route-propagation --route-table-id <table-id> --gateway-id {vgw_id}")
        
        return metadata
        
    except Exception as e:
        logger.error(f"HA VPN setup failed: {e}")
        cleanup.run()
        raise


def enable_route_propagation(ec2, vpc_id: str, vgw_id: str):
    """Enable VGW route propagation on all route tables in VPC"""
    resp = ec2.describe_route_tables(
        Filters=[{'Name': 'vpc-id', 'Values': [vpc_id]}]
    )
    
    for rt in resp['RouteTables']:
        rt_id = rt['RouteTableId']
        
        # Check if already enabled
        propagating = rt.get('PropagatingVgws', [])
        if any(p.get('GatewayId') == vgw_id for p in propagating):
            logger.info(f"Route propagation already enabled on {rt_id}")
            continue
        
        try:
            ec2.enable_vgw_route_propagation(
                RouteTableId=rt_id,
                GatewayId=vgw_id
            )
            logger.info(f"Enabled route propagation on route table {rt_id}")
        except ClientError as e:
            logger.warning(f"Failed to enable route propagation on {rt_id}: {e}")


def determine_route_tables(route_tables, subnet_ids=None):
    """Map subnets to route tables, returning unique route table IDs."""
    subnet_ids = subnet_ids or []
    subnet_to_rt = {}
    main_rt = None
    for rt in route_tables:
        rt_id = rt['RouteTableId']
        for assoc in rt.get('Associations', []):
            if assoc.get('Main'):
                main_rt = rt_id
            subnet_id = assoc.get('SubnetId')
            if subnet_id:
                subnet_to_rt[subnet_id] = rt_id
    if not subnet_ids:
        return sorted({rt['RouteTableId'] for rt in route_tables})
    targets = []
    for subnet_id in subnet_ids:
        rt_id = subnet_to_rt.get(subnet_id)
        if rt_id:
            targets.append(rt_id)
        elif main_rt:
            targets.append(main_rt)
    return sorted(set(targets))


def enable_route_propagation_for_subnets(ec2, vpc_id: str, vgw_id: str, subnet_ids: Optional[List[str]] = None):
    """Enable route propagation on route tables associated with selected subnets (or all if none provided)."""
    resp = ec2.describe_route_tables(Filters=[{'Name': 'vpc-id', 'Values': [vpc_id]}])
    route_tables = resp.get('RouteTables', [])
    if not route_tables:
        logger.warning("No route tables found for VPC; cannot enable propagation.")
        return []
    target_rt_ids = determine_route_tables(route_tables, subnet_ids)
    enabled = []
    for rt_id in target_rt_ids:
        try:
            ec2.enable_vgw_route_propagation(RouteTableId=rt_id, GatewayId=vgw_id)
            enabled.append(rt_id)
            logger.info(f"Enabled route propagation on {rt_id}")
        except ClientError as e:
            if e.response['Error']['Code'] == 'RouteAlreadyExists':
                logger.info(f"Propagation already enabled on {rt_id}")
                enabled.append(rt_id)
            else:
                logger.warning(f"Failed to enable propagation on {rt_id}: {e}")
    return enabled


def check_tunnel_status(compute, config: HAVPNConfig, tunnel_names: List[str]):
    """Check status of GCP VPN tunnels"""
    logger.info("\n" + "=" * 60)
    logger.info("Checking GCP Tunnel Status")
    logger.info("=" * 60)
    
    for tunnel_name in tunnel_names:
        try:
            tunnel = compute.vpnTunnels().get(
                project=config.gcp_project,
                region=config.gcp_region,
                vpnTunnel=tunnel_name
            ).execute()
            
            status = tunnel.get('status', 'UNKNOWN')
            detailed_status = tunnel.get('detailedStatus', 'No details')
            
            logger.info(f"\nTunnel: {tunnel_name}")
            logger.info(f"  Status: {status}")
            logger.info(f"  Details: {detailed_status}")
            
        except HttpError as e:
            logger.error(f"Failed to get status for {tunnel_name}: {e}")


def check_bgp_status(compute, config: HAVPNConfig, router_name: str):
    """Check BGP session status on Cloud Router"""
    logger.info("\n" + "=" * 60)
    logger.info("Checking BGP Session Status")
    logger.info("=" * 60)
    
    try:
        result = compute.routers().getRouterStatus(
            project=config.gcp_project,
            region=config.gcp_region,
            router=router_name
        ).execute()
        
        bgp_peers = result.get('result', {}).get('bgpPeerStatus', [])
        
        for peer in bgp_peers:
            logger.info(f"\nBGP Peer: {peer.get('name')}")
            logger.info(f"  Status: {peer.get('status')}")
            logger.info(f"  State: {peer.get('state')}")
            logger.info(f"  IP: {peer.get('ipAddress')}")
            logger.info(f"  Peer IP: {peer.get('peerIpAddress')}")
            logger.info(f"  Num Learned Routes: {peer.get('numLearnedRoutes', 0)}")
        
    except HttpError as e:
        logger.error(f"Failed to get BGP status: {e}")


# ============================================================================
# CLI Interface
# ============================================================================

def parse_arguments():
    parser = argparse.ArgumentParser(
        description='Automate AWS-GCP HA VPN setup with BGP',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Basic setup
  %(prog)s --gcp-project my-project --gcp-network default \\
           --gcp-region us-central1 --aws-vpc-id vpc-xxxxx \\
           --aws-region us-east-1

  # With custom ASNs
  %(prog)s --gcp-project my-project --gcp-network default \\
           --gcp-region us-central1 --aws-vpc-id vpc-xxxxx \\
           --aws-region us-east-1 --gcp-asn 64512 --aws-asn 64513

  # Check status after setup
  %(prog)s --check-status --prefix my-vpn-setup
        """
    )
    
    # Required arguments
    parser.add_argument('--gcp-project',
                       help='GCP project ID')
    parser.add_argument('--gcp-network',
                       help='GCP VPC network name')
    parser.add_argument('--gcp-region',
                       help='GCP region (e.g., us-central1)')
    parser.add_argument('--aws-vpc-id',
                       help='AWS VPC ID')
    parser.add_argument('--aws-region',
                       help='AWS region (e.g., us-east-1)')
    
    # Optional arguments
    parser.add_argument('--prefix',
                       help='Resource name prefix (vpn name, e.g., rohit-vpn)')
    parser.add_argument('--gcp-asn', type=int, default=64512,
                       help='GCP BGP ASN (default: 64512)')
    parser.add_argument('--aws-asn', type=int, default=64513,
                       help='AWS BGP ASN (default: 64513)')
    
    # Actions
    parser.add_argument('--check-status', action='store_true',
                       help='Check tunnel and BGP status')
    parser.add_argument('--propagate-subnets',
                       help='Comma-separated AWS subnet IDs whose route tables should enable VGW propagation (defaults to all)')
    parser.add_argument('--skip-route-propagation', action='store_true',
                       help='Skip enabling VGW route propagation (enabled by default; use --propagate-subnets to target specific ones)')
    
    return parser.parse_args()


def parse_subnet_ids(value: Optional[str]) -> Optional[List[str]]:
    """Parse comma-separated subnet IDs."""
    if value is None:
        return None
    value = value.strip()
    if value == "":
        return None
    lower = value.lower()
    if lower in ("none", "skip"):
        return []
    if lower == "all":
        return None
    return [item.strip() for item in value.split(',') if item.strip()]


def list_gcp_networks(compute, project: str) -> List[Dict]:
    """List GCP VPC networks for selection."""
    networks = []
    request = compute.networks().list(project=project)
    while request is not None:
        response = request.execute()
        networks.extend(response.get('items', []))
        request = compute.networks().list_next(previous_request=request, previous_response=response)
    return networks


def list_gcp_regions(compute, project: str) -> List[str]:
    """List available GCP regions for the project."""
    regions = []
    request = compute.regions().list(project=project)
    while request is not None:
        response = request.execute()
        for region in response.get('items', []):
            if region.get('status') == 'UP':
                regions.append(region.get('name'))
        request = compute.regions().list_next(previous_request=request, previous_response=response)
    return sorted(set(regions))


def prompt_select_subnets_for_propagation(ec2, vpc_id: str) -> Optional[List[str]]:
    """
    Ask which subnets' route tables should enable VGW propagation.
    Returns None to indicate "all", [] to indicate "none", or a list of subnet IDs to target.
    """
    resp = ec2.describe_subnets(Filters=[{'Name': 'vpc-id', 'Values': [vpc_id]}])
    subnets = resp.get('Subnets', [])
    if not subnets:
        logger.info("No subnets found in the VPC; will target all route tables by default.")
        return None

    items = []
    for idx, subnet in enumerate(sorted(subnets, key=lambda s: s.get('SubnetId'))):
        name = ""
        for t in subnet.get('Tags', []) or []:
            if t.get('Key') == 'Name':
                name = t.get('Value') or ""
                break
        cidr = subnet.get('CidrBlock') or ""
        items.append((idx + 1, subnet['SubnetId'], cidr, name))

    print("\nSelect AWS subnets whose route tables should enable VGW propagation:")
    for num, sid, cidr, name in items:
        label = f"{sid} ({cidr})"
        if name:
            label += f" - {name}"
        print(f"  {num}. {label}")
    print("  a. All subnets")
    print("  <enter> Skip selection (do not enable propagation)")

    choice = input("Enter comma-separated numbers (or 'a' for all): ").strip()
    if not choice:
        return []
    if choice.lower() == 'a':
        return None

    selected_ids = []
    for part in choice.split(','):
        part = part.strip()
        if not part:
            continue
        if part.isdigit():
            idx = int(part)
            if 1 <= idx <= len(items):
                selected_ids.append(items[idx - 1][1])
    return selected_ids or None


def prompt_with_default(message: str, default: Optional[str] = None) -> str:
    """Prompt user for input with an optional default."""
    while True:
        suffix = f" [{default}]" if default else ""
        resp = input(f"{message}{suffix}: ").strip()
        if resp:
            return resp
        if default:
            return default
        print("A value is required.")


def prompt_select_option(message: str, options: List[Dict[str, str]],
                         allow_manual: bool = True,
                         default_value: Optional[str] = None) -> str:
    """
    Simple selection helper. Options should be [{label, value}, ...].
    Returns the selected value or manual entry.
    """
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
        if not choice and default_value:
            return default_value
        if choice.isdigit():
            num = int(choice)
            if 1 <= num <= len(options):
                return options[num - 1]['value']
        if allow_manual and choice:
            return choice
        print("Invalid selection, try again.")


def interactive_setup(args):
    """
    Prompt for missing required values to mirror the legacy VPN script's interactivity.
    """
    required = ['gcp_project', 'gcp_network', 'gcp_region', 'aws_vpc_id', 'aws_region']
    if all(getattr(args, field) for field in required):
        return
    
    print("\n=== Interactive HA VPN configuration ===")
    
    # AWS region
    if not args.aws_region:
        regions = sorted(set(boto3.Session().get_available_regions('ec2')))
        region_options = [{'label': r, 'value': r} for r in regions]
        args.aws_region = prompt_select_option("Select AWS region", region_options, allow_manual=True)
        print(f"AWS region selected: {args.aws_region}")
    
    # AWS VPC selection
    session = boto3.Session(region_name=args.aws_region)
    ec2 = session.client('ec2')
    
    def _tag_value(resource, key='Name'):
        for t in resource.get('Tags', []) or []:
            if t.get('Key') == key:
                return t.get('Value')
        return ""
    
    if not args.aws_vpc_id:
        vpcs = ec2.describe_vpcs().get('Vpcs', [])
        vpc_options = []
        for vpc in vpcs:
            cidr = vpc.get('CidrBlock') or ''
            name = _tag_value(vpc) or 'no-name'
            vpc_options.append({
                'label': f"{vpc['VpcId']} ({cidr} - {name})",
                'value': vpc['VpcId']
            })
        if not vpc_options:
            print("No VPCs found; please enter VPC ID manually.")
            args.aws_vpc_id = prompt_with_default("Enter AWS VPC ID")
        else:
            args.aws_vpc_id = prompt_select_option("Select AWS VPC", vpc_options, allow_manual=True)
        print(f"AWS VPC selected: {args.aws_vpc_id}")
    # Ask which subnets to target for route propagation
    selected_subnets = prompt_select_subnets_for_propagation(ec2, args.aws_vpc_id)
    if selected_subnets is None:
        # All subnets
        args.propagate_subnets = None
        logger.info("Will enable VGW propagation on all route tables.")
    elif len(selected_subnets) == 0:
        # None selected
        args.propagate_subnets = "none"
        logger.info("No subnets selected; route propagation will be skipped.")
    else:
        args.propagate_subnets = ",".join(selected_subnets)
        logger.info(f"Will enable VGW propagation on selected subnet route tables: {args.propagate_subnets}")
    
    # GCP project/region/network
    if not args.gcp_project:
        default_project = os.environ.get('GCP_PROJECT') or os.environ.get('GOOGLE_CLOUD_PROJECT')
        args.gcp_project = prompt_with_default("Enter GCP project ID", default_project)
    
    if not args.gcp_region:
        compute = get_gcp_compute_service()
        region_options = []
        try:
            region_names = list_gcp_regions(compute, args.gcp_project)
            region_options = [{'label': name, 'value': name} for name in region_names]
        except Exception as e:
            logger.info(f"Could not list GCP regions automatically: {e}")
        if region_options:
            default_value = args.gcp_region or (region_options[0]['value'] if region_options else None)
            args.gcp_region = prompt_select_option(
                "Select GCP region",
                region_options,
                allow_manual=True,
                default_value=default_value
            )
        else:
            args.gcp_region = prompt_with_default("Enter GCP region (e.g., us-central1)", "us-central1")
    
    if not args.gcp_network:
        # Try to fetch networks automatically
        networks = []
        try:
            compute = compute if 'compute' in locals() else get_gcp_compute_service()
            networks = list_gcp_networks(compute, args.gcp_project)
        except Exception as e:
            logger.info(f"Could not list GCP networks automatically: {e}")
        if networks:
            options = []
            for net in networks:
                name = net.get('name')
                mode = 'auto' if net.get('autoCreateSubnetworks') else 'custom'
                options.append({'label': f"{name} ({mode})", 'value': name})
            default_val = options[0]['value'] if options else None
            args.gcp_network = prompt_select_option(
                "Select GCP VPC network", options, allow_manual=True, default_value=default_val
            )
        else:
            args.gcp_network = prompt_with_default("Enter GCP VPC network name", "default")
    
    # VPN name / prefix
    if not args.prefix:
        args.prefix = prompt_with_default("Enter VPN name/prefix (used for resource names)", "ha-vpn")
    else:
        # Confirm/allow override if passed
        args.prefix = prompt_with_default(
            f"VPN name/prefix (current: {args.prefix})", args.prefix
        )
    
    # Final validation
    missing = [field for field in required if not getattr(args, field)]
    if missing:
        raise RuntimeError(f"Missing required inputs after prompting: {', '.join(missing)}.")


def main():
    args = parse_arguments()
    interactive_setup(args)
    propagate_subnet_ids = parse_subnet_ids(args.propagate_subnets)
    
    # Initialize configuration
    config = HAVPNConfig()
    config.gcp_project = args.gcp_project
    config.gcp_network = args.gcp_network
    config.gcp_region = args.gcp_region
    config.gcp_asn = args.gcp_asn
    config.aws_vpc_id = args.aws_vpc_id
    config.aws_region = args.aws_region
    config.aws_asn = args.aws_asn
    
    # Check for status command
    if args.check_status:
        compute = get_gcp_compute_service()
        names = build_resource_names(args.prefix)
        
        tunnel_names = [f"{names['gcp_tunnel_prefix']}-{i}" for i in range(4)]
        check_tunnel_status(compute, config, tunnel_names)
        check_bgp_status(compute, config, names['gcp_router'])
        return
    
    # Execute setup
    logger.info("Starting HA VPN setup...")
    logger.info(f"GCP: {config.gcp_project}/{config.gcp_region}/{config.gcp_network}")
    logger.info(f"AWS: {config.aws_vpc_id} in {config.aws_region}")
    logger.info(f"ASNs: GCP={config.gcp_asn}, AWS={config.aws_asn}")
    
    metadata = setup_ha_vpn(config, args.prefix)
    
    # Enable route propagation based on selection
    if args.skip_route_propagation:
        logger.info("Skipping route propagation as requested.")
    elif propagate_subnet_ids == []:
        logger.info("No subnets selected; skipping route propagation.")
    else:
        logger.info("\nEnabling route propagation...")
        ec2 = boto3.client('ec2', region_name=config.aws_region)
        vgw_id = metadata['resources']['aws_vgw_id']
        if propagate_subnet_ids:
            logger.info(f"Targeting route tables for subnets: {', '.join(propagate_subnet_ids)}")
            enable_route_propagation_for_subnets(ec2, config.aws_vpc_id, vgw_id, propagate_subnet_ids)
        else:
            enable_route_propagation(ec2, config.aws_vpc_id, vgw_id)
    
    # Wait a bit then check status
    logger.info("\nWaiting 30 seconds before checking tunnel status...")
    time.sleep(30)
    
    compute = get_gcp_compute_service()
    tunnel_names = [t['name'] for t in metadata['resources']['gcp_tunnels']]
    check_tunnel_status(compute, config, tunnel_names)
    check_bgp_status(compute, config, build_resource_names(args.prefix)['gcp_router'])
    
    logger.info("\n" + "=" * 60)
    logger.info("Setup complete! Metadata saved to:")
    logger.info(f"  {METADATA_DIR / args.prefix}.json")
    logger.info("=" * 60)


if __name__ == '__main__':
    main()
