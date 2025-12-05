#!/usr/bin/env python3
"""
Automate AWS <--> GCP Classic VPN (static) setup.

- Creates/uses AWS Virtual Private Gateway (VGW), Customer Gateway (CGW),
  Site-to-Site VPN (static).
- Creates/reserves a GCP external IP, Classic VPN gateway, two GCP tunnels,
  routes, and firewall rule.
- Requires AWS creds (env/profile) and GCP service account JSON in
  GOOGLE_APPLICATION_CREDENTIALS.

Run with --help for usage.
"""

import argparse
import time
import json
import sys
import ipaddress
from pathlib import Path
from datetime import datetime
import boto3
from botocore.exceptions import ClientError

# GCP libs
from google.oauth2 import service_account
from googleapiclient import discovery
from googleapiclient.errors import HttpError


def build_resource_names(prefix):
    return {
        'aws_vgw': f"{prefix}-vgw",
        'aws_cgw': f"{prefix}-cgw",
        'aws_vpn': f"{prefix}-vpn",
        'gcp_gateway': f"{prefix}-gcp-gateway",
        'gcp_tunnel_prefix': f"{prefix}-tunnel",
        'gcp_firewall': f"{prefix}-allow-aws",
        'gcp_address': f"{prefix}-vpn-ip",
        'gcp_route_prefix': f"{prefix}-aws-route",
        'gcp_forwarding_esp': f"{prefix}-esp-fr",
        'gcp_forwarding_udp500': f"{prefix}-udp500-fr",
        'gcp_forwarding_udp4500': f"{prefix}-udp4500-fr",
    }


METADATA_DIR = Path(__file__).resolve().parent / "vpn_runs"


def _write_metadata(prefix, payload):
    METADATA_DIR.mkdir(parents=True, exist_ok=True)
    path = METADATA_DIR / f"{prefix}.json"
    tmp_path = path.with_suffix(".tmp")
    with tmp_path.open('w', encoding='utf-8') as fh:
        json.dump(payload, fh, indent=2, sort_keys=True)
    tmp_path.replace(path)
    print(f"Metadata written to {path}")


class CleanupManager:
    """Track created resources and remove them if the run fails."""

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
                print(f"Cleaning up: {description}")
                func(*args, **kwargs)
            except Exception as exc:
                print(f"  Cleanup failed for {description}: {exc}")


def wait_for_compute_operation(compute, project, operation, region=None, is_global=False, poll_interval=2):
    """Wait for a Google Compute Engine operation to finish."""
    op_name = operation.get('name')
    while True:
        if region:
            result = compute.regionOperations().get(project=project, region=region, operation=op_name).execute()
        elif is_global:
            result = compute.globalOperations().get(project=project, operation=op_name).execute()
        else:
            zone_ref = operation.get('zone')
            if not zone_ref:
                raise ValueError("Zone must be specified for zonal operations.")
            zone = zone_ref.split('/')[-1]
            result = compute.zoneOperations().get(project=project, zone=zone, operation=op_name).execute()
        status = result.get('status')
        if status == 'DONE':
            if 'error' in result:
                raise RuntimeError(f"GCP operation {op_name} failed: {result['error']}")
            return result
        time.sleep(poll_interval)


def wait_for_resource_absence(check_fn, description, timeout=600, poll_interval=5):
    """Polls check_fn until it raises or returns False, meaning resource no longer exists."""
    t0 = time.time()
    while True:
        exists = False
        try:
            exists = check_fn()
        except Exception:
            exists = False
        if not exists:
            return
        if time.time() - t0 > timeout:
            raise TimeoutError(f"Timed out waiting for {description} to be fully removed.")
        time.sleep(poll_interval)


def compute_covering_cidr(cidrs):
    """Return a single CIDR that covers all provided CIDRs."""
    networks = [ipaddress.ip_network(c.strip(), strict=False) for c in cidrs if c.strip()]
    if not networks:
        raise ValueError("Cannot compute covering CIDR for empty list.")
    min_ip = min(net.network_address for net in networks)
    max_ip = max(net.broadcast_address for net in networks)
    summary = list(ipaddress.summarize_address_range(min_ip, max_ip))
    if not summary:
        raise ValueError("Unable to summarize address range.")
    # summary may contain multiple networks; merge into one supernet if needed
    combined = summary[0]
    for net in summary[1:]:
        while not combined.supernet_of(net):
            combined = combined.supernet()
    return str(combined)

# ---------- Helpers ----------
def _parse_customer_gateway_config(cfg):
    outs, psks = [], []
    if not cfg:
        return outs, psks
    for line in cfg.splitlines():
        line = line.strip()
        if '<outside-ip-address>' in line:
            try:
                val = line.split('>')[1].split('<')[0].strip()
                outs.append(val)
            except Exception:
                continue
        if '<pre-shared-key>' in line:
            try:
                val = line.split('>')[1].split('<')[0].strip()
                psks.append(val)
            except Exception:
                continue
    return outs, psks


def wait_for_aws_vpn_tunnels(ec2, vpn_id, timeout=600, poll_interval=5):
    """Poll until AWS provides tunnel information and PSKs."""
    t0 = time.time()
    next_log = 0
    while True:
        resp = ec2.describe_vpn_connections(VpnConnectionIds=[vpn_id])['VpnConnections'][0]
        options = resp.get('Options', {}) or {}
        tunnel_opts = options.get('TunnelOptions') or []
        outs = [opt.get('OutsideIpAddress') for opt in tunnel_opts if opt.get('OutsideIpAddress')]
        psks = [opt.get('PreSharedKey') for opt in tunnel_opts if opt.get('PreSharedKey')]

        if len(outs) >= 2 and len(psks) >= 2:
            return outs, psks, resp

        cfg_outs, cfg_psks = _parse_customer_gateway_config(resp.get('CustomerGatewayConfiguration'))
        if len(cfg_outs) >= 2 and len(cfg_psks) >= 2:
            return cfg_outs, cfg_psks, resp

        elapsed = time.time() - t0
        if elapsed >= next_log:
            status = resp.get('State')
            print(f"  Waiting for AWS tunnels (state={status}, elapsed={int(elapsed)}s)...")
            next_log = elapsed + 30
        if elapsed > timeout:
            raise TimeoutError(
                "Timed out waiting for AWS VPN config (PSK/outside IP). "
                "Check the AWS console to confirm the VPN connection is available."
            )
        time.sleep(poll_interval)

def ensure_vgw_attached(ec2, vpc_id, vgw_name):
    """Create a VGW if needed, attach to the VPC, and wait until attachment is complete."""
    created = False
    resp = ec2.describe_vpn_gateways(
        Filters=[{'Name': 'tag:Name', 'Values': [vgw_name]}]
    )['VpnGateways']
    active_gateways = [gw for gw in resp if gw.get('State') != 'deleted']
    if active_gateways:
        vgw = active_gateways[0]
        vgw_id = vgw['VpnGatewayId']
        print(f"Found existing VGW: {vgw_id}")
    else:
        print("Creating Virtual Private Gateway (VGW)...")
        resp = ec2.create_vpn_gateway(Type='ipsec.1')
        vgw_id = resp['VpnGateway']['VpnGatewayId']
        ec2.create_tags(Resources=[vgw_id], Tags=[{'Key': 'Name', 'Value': vgw_name}])
        vgw = resp['VpnGateway']
        created = True

    def _attachment_state(gateway):
        for att in gateway.get('VpcAttachments', []):
            if att.get('VpcId') == vpc_id:
                return att.get('State')
        return None

    state = _attachment_state(vgw)
    if state == 'attached':
        print(f"VGW {vgw_id} is already attached to VPC {vpc_id}.")
        return vgw_id, created

    def _attach_gateway(gateway_id):
        print(f"Attaching VGW {gateway_id} to VPC {vpc_id}...")
        try:
            ec2.attach_vpn_gateway(VpnGatewayId=gateway_id, VpcId=vpc_id)
            return gateway_id
        except ClientError as exc:
            code = exc.response['Error']['Code']
            if code in ('Resource.AlreadyAssociated', 'IncorrectState'):
                return gateway_id
            if code == 'InvalidVpnGatewayID.NotFound':
                return None
            raise

    new_id = _attach_gateway(vgw_id)
    if new_id is None:
        print("VGW ID not found or deleted; creating a new VGW and retrying attach.")
        resp = ec2.create_vpn_gateway(Type='ipsec.1')
        vgw_id = resp['VpnGateway']['VpnGatewayId']
        ec2.create_tags(Resources=[vgw_id], Tags=[{'Key': 'Name', 'Value': vgw_name}])
        created = True
        _attach_gateway(vgw_id)

    print("Waiting for VGW to attach...")
    while True:
        gw = ec2.describe_vpn_gateways(VpnGatewayIds=[vgw_id])['VpnGateways'][0]
        state = _attachment_state(gw)
        print(f"  VGW state: {state or 'detached'}")
        if state == 'attached':
            break
        time.sleep(5)

    print(f"VGW {vgw_id} attached successfully.")
    return vgw_id, created

def ensure_customer_gateway(ec2, gcp_pub_ip, cgw_base_name, bgp_asn=65000):
    """
    Reuse an available Customer Gateway that already points at gcp_pub_ip,
    otherwise create a NEW gateway with incremental suffix <base>-1, <base>-2, ...
    """
    def _wait_for_cgw(cgw_id, timeout=300, poll_interval=5):
        t0 = time.time()
        while True:
            try:
                resp = ec2.describe_customer_gateways(CustomerGatewayIds=[cgw_id])
            except ClientError as exc:
                if exc.response['Error']['Code'] == 'InvalidCustomerGatewayID.NotFound':
                    if time.time() - t0 > timeout:
                        raise
                    time.sleep(poll_interval)
                    continue
                raise
            gateways = resp.get('CustomerGateways')
            if gateways:
                state = gateways[0].get('State')
                if state == 'available':
                    return
            if time.time() - t0 > timeout:
                raise TimeoutError(f"Timed out waiting for customer gateway {cgw_id} to become available.")
            time.sleep(poll_interval)

    existing_cgws = ec2.describe_customer_gateways()['CustomerGateways']
    nums = []
    prefix = f"{cgw_base_name}-"
    for c in existing_cgws:
        name = ""
        for t in c.get('Tags', []) or []:
            if t['Key'] == 'Name':
                name = t.get('Value') or ''
                break
        if c.get('State') == 'available' and (c.get('IpAddress') or c.get('Ip')) == gcp_pub_ip:
            print(f"Found existing Customer Gateway {c.get('CustomerGatewayId')} with matching IP {gcp_pub_ip}; reusing.")
            return c['CustomerGatewayId'], False
        if name.startswith(prefix):
            try:
                nums.append(int(name[len(prefix):]))
            except (ValueError, TypeError):
                continue
    next_num = max(nums) + 1 if nums else 1
    new_name = f"{cgw_base_name}-{next_num}"
    print(f"Creating NEW Customer Gateway: {new_name}")

    resp = ec2.create_customer_gateway(
        Type='ipsec.1',
        PublicIp=gcp_pub_ip,
        BgpAsn=bgp_asn
    )
    cgw_id = resp['CustomerGateway']['CustomerGatewayId']
    ec2.create_tags(Resources=[cgw_id], Tags=[{'Key': 'Name', 'Value': new_name}])
    _wait_for_cgw(cgw_id)
    print(f"Created Customer Gateway {cgw_id} with name {new_name}")
    return cgw_id, True

def create_vpn_connection_static(ec2, cgw_id, vgw_id, name, static_prefixes):
    """Create Site-to-Site VPN connection with static routing."""
    for v in ec2.describe_vpn_connections()['VpnConnections']:
        tags = {t['Key']: t['Value'] for t in v.get('Tags', [])}
        if tags.get('Name') == name:
            return v['VpnConnectionId'], False
    resp = ec2.create_vpn_connection(
        CustomerGatewayId=cgw_id,
        Type='ipsec.1',
        VpnGatewayId=vgw_id,
        Options={'StaticRoutesOnly': True},
        TagSpecifications=[{'ResourceType': 'vpn-connection', 'Tags': [{'Key':'Name','Value':name}]}]
    )
    vpn_id = resp['VpnConnection']['VpnConnectionId']
    # add static routes
    for prefix in static_prefixes:
        try:
            ec2.create_vpn_connection_route(VpnConnectionId=vpn_id, DestinationCidrBlock=prefix)
        except ClientError as e:
            # may already exist
            pass
    return vpn_id, True

# ---------- GCP helpers ----------
def get_compute_service():
    creds = service_account.Credentials.from_service_account_file(
        # path will be read from env var; allow default by GOOGLE_APPLICATION_CREDENTIALS
        os.environ.get('GOOGLE_APPLICATION_CREDENTIALS')
    )
    return discovery.build('compute', 'v1', credentials=creds, cache_discovery=False)

import os

def reserve_address(compute, project, region, name):
    try:
        req = compute.addresses().get(project=project, region=region, address=name)
        resp = req.execute()
        return resp['address'], False
    except HttpError as e:
        if getattr(e, 'resp', None) is None or e.resp.status != 404:
            raise
    body = {'name': name}
    op = compute.addresses().insert(project=project, region=region, body=body).execute()
    wait_for_compute_operation(compute, project, op, region=region)
    a = compute.addresses().get(project=project, region=region, address=name).execute()
    return a['address'], True

def create_classic_vpn_gateway_and_tunnels(
    compute,
    project,
    region,
    gateway_name,
    reserved_ip_name,
    reserved_ip_address,
    tunnels,
    forwarding_specs=None,
    forwarding_address=None,
    target_gateway_link=None
):
    """
    tunnels: list of dicts:
      { 'name': 'tunnel-a', 'peer_ip': '13.234.x.x', 'psk': '...', 'remote_ranges': ['10.0.0.0/21'] }
    """
    gateway_created = False
    try:
        compute.targetVpnGateways().get(project=project, region=region, targetVpnGateway=gateway_name).execute()
        print("GCP Classic VPN gateway already exists:", gateway_name)
    except HttpError as e:
        if getattr(e, 'resp', None) is None or e.resp.status != 404:
            raise
        body = {
            "name": gateway_name,
            "network": f"projects/{project}/global/networks/{args.gcp_network}",
            "region": region,
        }
        op = compute.targetVpnGateways().insert(project=project, region=region, body=body).execute()
        wait_for_compute_operation(compute, project, op, region=region)
        gateway_created = True
    created_forwarding = []
    created_tunnels = []
    ensured_tunnels = []

    if forwarding_specs:
        if not forwarding_address or not target_gateway_link:
            raise ValueError("Forwarding rules require forwarding_address and target_gateway_link.")
        for spec in forwarding_specs:
            created = ensure_forwarding_rule(
                compute,
                project,
                region,
                spec['name'],
                forwarding_address,
                target_gateway_link,
                spec['ip_protocol'],
                spec.get('port_range'),
                expected_ip=reserved_ip_address
            )
            if created:
                created_forwarding.append(spec['name'])

    for t in tunnels:
        ike_ver = t.get('ike_version', 1)
        body = {
            "name": t['name'],
            "peerIp": t['peer_ip'],
            "ikeVersion": ike_ver,
            "sharedSecret": t['psk'],
            "targetVpnGateway": f"projects/{project}/regions/{region}/targetVpnGateways/{gateway_name}",
        }
        remote = [cidr for cidr in (t.get('remote_ranges') or []) if cidr]
        if remote:
            body["remoteTrafficSelector"] = remote

        local = [cidr for cidr in (t.get('local_ranges') or []) if cidr]
        if local:
            if ike_ver == 1 and len(local) > 1:
                aggregated = compute_covering_cidr(local)
                print(f"  IKEv1 tunnel {t['name']} using aggregated local selector {aggregated}.")
                local = [aggregated]
            body["localTrafficSelector"] = local
        try:
            op = compute.vpnTunnels().insert(project=project, region=region, body=body).execute()
            wait_for_compute_operation(compute, project, op, region=region)
            created_tunnels.append(t['name'])
            ensured_tunnels.append(t['name'])
        except HttpError as e:
            if getattr(e, 'resp', None) is not None and e.resp.status == 409:
                print(f"Tunnel {t['name']} already exists; skipping creation.")
                ensured_tunnels.append(t['name'])
                continue
            print(f"Tunnel create error for {t['name']}: {e}")
            raise
    if len(ensured_tunnels) < len(tunnels):
        # For tunnels we skipped (should not happen), verify they exist
        for t in tunnels:
            name = t['name']
            if name in ensured_tunnels:
                continue
            compute.vpnTunnels().get(project=project, region=region, vpnTunnel=name).execute()
            ensured_tunnels.append(name)
    return {
        'gateway_created': gateway_created,
        'tunnels_created': created_tunnels,
        'tunnels_present': ensured_tunnels,
        'forwarding_created': created_forwarding,
    }

def ensure_gcp_routes(compute, project, routes, network_self_link=None):
    created = []
    for route in routes:
        name = route['name']
        body = {
            "name": name,
            "destRange": route['destRange'],
            "priority": 1000,
            "nextHopVpnTunnel": route['nextHopVpnTunnel'],
        }
        network_link = route.get('network') or network_self_link
        if network_link:
            body["network"] = network_link
        try:
            compute.routes().get(project=project, route=name).execute()
            print(f"GCP route {name} already exists.")
            continue
        except HttpError as e:
            if getattr(e, 'resp', None) is None or e.resp.status != 404:
                print("create route error:", e)
                raise
        op = compute.routes().insert(project=project, body=body).execute()
        wait_for_compute_operation(compute, project, op, is_global=True)
        created.append(name)
    return created

def ensure_firewall_rule(compute, project, name, network, src_ranges):
    desired_allowed = [
        {"IPProtocol": "udp", "ports": ["500", "4500"]},
        {"IPProtocol": "esp"},
        {"IPProtocol": "ah"},
    ]
    src_ranges = sorted(src_ranges)
    try:
        rule = compute.firewalls().get(project=project, firewall=name).execute()
        current_allowed = rule.get('allowed', [])
        current_sources = sorted(rule.get('sourceRanges', []))
        if current_allowed == desired_allowed and current_sources == src_ranges:
            print("Firewall rule already matches desired configuration.")
            return False
        patch_body = {
            "allowed": desired_allowed,
            "sourceRanges": src_ranges,
            "priority": 1000,
            "direction": "INGRESS",
            "network": f"projects/{project}/global/networks/{network}",
        }
        op = compute.firewalls().patch(project=project, firewall=name, body=patch_body).execute()
        wait_for_compute_operation(compute, project, op, is_global=True)
        print("Updated existing firewall rule to desired configuration.")
        return False
    except HttpError as e:
        if getattr(e, 'resp', None) is not None and e.resp.status != 404:
            print("firewall exists or error:", e)
            raise
    body = {
        "name": name,
        "network": f"projects/{project}/global/networks/{network}",
        "direction": "INGRESS",
        "allowed": desired_allowed,
        "sourceRanges": src_ranges,
        "priority": 1000,
    }
    op = compute.firewalls().insert(project=project, body=body).execute()
    wait_for_compute_operation(compute, project, op, is_global=True)
    return True


def delete_gcp_address(compute, project, region, name):
    try:
        op = compute.addresses().delete(project=project, region=region, address=name).execute()
    except HttpError as e:
        if getattr(e, 'resp', None) is not None and e.resp.status == 404:
            return
        raise
    wait_for_compute_operation(compute, project, op, region=region)


def delete_gcp_vpn_gateway(compute, project, region, gateway_name):
    try:
        op = compute.targetVpnGateways().delete(project=project, region=region, targetVpnGateway=gateway_name).execute()
    except HttpError as e:
        if getattr(e, 'resp', None) is not None and e.resp.status == 404:
            return
        raise
    wait_for_compute_operation(compute, project, op, region=region)


def delete_gcp_vpn_tunnel(compute, project, region, tunnel_name):
    try:
        op = compute.vpnTunnels().delete(project=project, region=region, vpnTunnel=tunnel_name).execute()
    except HttpError as e:
        if getattr(e, 'resp', None) is not None and e.resp.status == 404:
            return
        raise
    wait_for_compute_operation(compute, project, op, region=region)


def delete_gcp_firewall(compute, project, name):
    try:
        op = compute.firewalls().delete(project=project, firewall=name).execute()
    except HttpError as e:
        if getattr(e, 'resp', None) is not None and e.resp.status == 404:
            return
        raise
    wait_for_compute_operation(compute, project, op, is_global=True)


def ensure_forwarding_rule(compute, project, region, name, address, target_gateway, ip_protocol, port_range=None, expected_ip=None):
    try:
        rule = compute.forwardingRules().get(project=project, region=region, forwardingRule=name).execute()
        current_ip = rule.get('IPAddress')
        current_target = rule.get('target')
        current_protocol = rule.get('IPProtocol')
        current_range = rule.get('portRange') or ''
        desired_range = port_range or ''
        needs_reset = False
        reasons = []
        if expected_ip and current_ip != expected_ip:
            needs_reset = True
            reasons.append(f"IP {current_ip} != {expected_ip}")
        if current_target != target_gateway:
            needs_reset = True
            reasons.append("target mismatch")
        if current_protocol != ip_protocol:
            needs_reset = True
            reasons.append("protocol mismatch")
        if current_range != desired_range:
            needs_reset = True
            reasons.append("port range mismatch")
        if not needs_reset:
            print(f"Forwarding rule {name} already matches desired configuration.")
            return False
        reason_text = ", ".join(reasons) or "configuration drift"
        print(f"Recreating forwarding rule {name} due to {reason_text}.")
        delete_forwarding_rule(compute, project, region, name)
    except HttpError as e:
        if getattr(e, 'resp', None) is not None and e.resp.status != 404:
            print(f"Error checking forwarding rule {name}: {e}")
            raise
    body = {
        "name": name,
        "IPAddress": address,
        "IPProtocol": ip_protocol,
        "target": target_gateway,
    }
    if port_range:
        body["portRange"] = port_range
    op = compute.forwardingRules().insert(project=project, region=region, body=body).execute()
    wait_for_compute_operation(compute, project, op, region=region)
    print(f"Created forwarding rule {name}.")
    return True


def delete_forwarding_rule(compute, project, region, name):
    try:
        op = compute.forwardingRules().delete(project=project, region=region, forwardingRule=name).execute()
    except HttpError as e:
        if getattr(e, 'resp', None) is not None and e.resp.status == 404:
            return
        raise
    wait_for_compute_operation(compute, project, op, region=region)


def delete_gcp_route(compute, project, name):
    try:
        op = compute.routes().delete(project=project, route=name).execute()
    except HttpError as e:
        if getattr(e, 'resp', None) is not None and e.resp.status == 404:
            return
        raise
    wait_for_compute_operation(compute, project, op, is_global=True)


def delete_aws_vpn_connection(ec2, vpn_id):
    try:
        ec2.delete_vpn_connection(VpnConnectionId=vpn_id)
    except ClientError as exc:
        if exc.response['Error']['Code'] in ('InvalidVpnConnectionID.NotFound',):
            return
        raise


def delete_aws_customer_gateway(ec2, cgw_id):
    try:
        ec2.delete_customer_gateway(CustomerGatewayId=cgw_id)
    except ClientError as exc:
        if exc.response['Error']['Code'] in ('InvalidCustomerGatewayID.NotFound',):
            return
        raise


def delete_aws_routes(ec2, routes):
    for route in routes:
        try:
            ec2.delete_route(
                RouteTableId=route['RouteTableId'],
                DestinationCidrBlock=route['DestinationCidrBlock']
            )
        except ClientError as exc:
            if exc.response['Error']['Code'] in ('InvalidRoute.NotFound', 'InvalidRouteTableID.NotFound'):
                continue
            raise


def delete_aws_vgw(ec2, vgw_id, vpc_id=None):
    try:
        gw = ec2.describe_vpn_gateways(VpnGatewayIds=[vgw_id])['VpnGateways'][0]
    except ClientError as exc:
        if exc.response['Error']['Code'] == 'InvalidVpnGatewayID.NotFound':
            return
        raise
    attachments = gw.get('VpcAttachments', [])
    target_vpcs = set()
    for attachment in attachments:
        if attachment.get('State') in ('attached', 'attaching'):
            if not vpc_id or attachment.get('VpcId') == vpc_id:
                target_vpcs.add(attachment.get('VpcId'))
    for target_vpc in target_vpcs:
        try:
            ec2.detach_vpn_gateway(VpnGatewayId=vgw_id, VpcId=target_vpc)
        except ClientError:
            pass
    # Wait until detached
    for _ in range(40):
        gw = ec2.describe_vpn_gateways(VpnGatewayIds=[vgw_id])['VpnGateways'][0]
        states = [att.get('State') for att in gw.get('VpcAttachments', [])]
        if not states or all(state in ('detached',) for state in states):
            break
        time.sleep(3)
    for attempt in range(5):
        try:
            ec2.delete_vpn_gateway(VpnGatewayId=vgw_id)
            return
        except ClientError as exc:
            code = exc.response['Error']['Code']
            if code == 'InvalidVpnGatewayID.NotFound':
                return
            if code == 'IncorrectState' and attempt < 4:
                time.sleep(5)
                continue
            raise

# ---------- Interactive helpers ----------
def prompt_with_default(message, default=None):
    while True:
        suffix = f" [{default}]:" if default else ":"
        value = input(f"{message}{suffix} ").strip()
        if value:
            return value
        if default:
            return default
        print("Value required.")


def prompt_select_option(title, options, allow_manual=False, default_value=None, default_label=None):
    if not options and not allow_manual:
        raise ValueError("No options available for selection.")
    while True:
        print(f"\n{title}")
        if options:
            for idx, option in enumerate(options, start=1):
                print(f"  [{idx}] {option['label']}")
        prompt = "Enter choice number"
        if allow_manual:
            prompt += " or type a value"
        if default_value is not None:
            label = default_label if default_label is not None else str(default_value)
            prompt += f" [default {label}]"
        prompt += ": "
        choice = input(prompt).strip()
        if not choice and default_value is not None:
            return default_value
        if allow_manual and choice and not choice.isdigit():
            return choice
        if choice.isdigit():
            idx = int(choice)
            if 1 <= idx <= len(options):
                return options[idx - 1]['value']
        print("Invalid selection. Try again.")


def prompt_multi_select(title, items, allow_all=True):
    if not items:
        raise ValueError("No items available for selection.")
    while True:
        print(f"\n{title}")
        for idx, option in enumerate(items, start=1):
            print(f"  [{idx}] {option['label']}")
        suffix = " or 'all'" if allow_all else ""
        raw = input(f"Enter comma-separated numbers{suffix}: ").strip().lower()
        if allow_all and raw in ('all', 'a', '*'):
            return [item['value'] for item in items], True
        if not raw:
            print("Selection required.")
            continue
        tokens = [token.strip() for token in raw.split(',') if token.strip()]
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
        return [items[idx]['value'] for idx in unique_indices], False


def get_tag_value(resource, key='Name'):
    for tag in resource.get('Tags', []) or []:
        if tag.get('Key') == key:
            return tag.get('Value')
    return ''


def list_aws_vpcs(ec2):
    vpcs = []
    resp = ec2.describe_vpcs()
    for vpc in resp.get('Vpcs', []):
        name = get_tag_value(vpc)
        label = f"{vpc['VpcId']} ({vpc.get('CidrBlock','?')})"
        if name:
            label += f" - {name}"
        vpcs.append({'label': label, 'value': vpc})
    return vpcs


def list_aws_subnets(ec2, vpc_id):
    subnets = []
    resp = ec2.describe_subnets(Filters=[{'Name': 'vpc-id', 'Values': [vpc_id]}])
    for subnet in resp.get('Subnets', []):
        name = get_tag_value(subnet)
        label = f"{subnet['SubnetId']} ({subnet.get('CidrBlock','?')} | {subnet.get('AvailabilityZone','?')})"
        if name:
            label += f" - {name}"
        subnets.append({'label': label, 'value': subnet})
    return subnets


def list_gcp_regions(compute, project):
    regions = []
    try:
        request = compute.regions().list(project=project)
        while request is not None:
            response = request.execute()
            for region in response.get('items', []):
                regions.append(region['name'])
            request = compute.regions().list_next(previous_request=request, previous_response=response)
    except HttpError as e:
        print("Unable to list GCP regions; falling back to manual entry:", e)
    return sorted(set(regions))


def list_gcp_networks(compute, project):
    networks = []
    try:
        request = compute.networks().list(project=project)
        while request is not None:
            response = request.execute()
            for network in response.get('items', []):
                label = f"{network['name']} (autoCreateSubnets={'on' if network.get('autoCreateSubnetworks') else 'off'})"
                networks.append({'label': label, 'value': network})
            request = compute.networks().list_next(previous_request=request, previous_response=response)
    except HttpError as e:
        print("Unable to list GCP networks; you may need to type one manually:", e)
    return networks


def parse_subnetwork_self_link(link):
    parts = link.split('/')
    try:
        project_index = parts.index('projects')
        project = parts[project_index + 1]
        region_index = parts.index('regions')
        region = parts[region_index + 1]
        subnet_index = parts.index('subnetworks')
        subnetwork = parts[subnet_index + 1]
        return project, region, subnetwork
    except (ValueError, IndexError):
        return None, None, None


def fetch_subnetworks_from_links(compute, links, preferred_region=None):
    subnetworks = []
    for link in links:
        project, region, subnetwork_name = parse_subnetwork_self_link(link)
        if not project or not region or not subnetwork_name:
            continue
        if preferred_region and region != preferred_region:
            continue
        try:
            subnet = compute.subnetworks().get(project=project, region=region, subnetwork=subnetwork_name).execute()
            subnetworks.append(subnet)
        except HttpError as e:
            print(f"Unable to fetch subnet {subnetwork_name} in {region}: {e}")
    return subnetworks


def list_gcp_subnets(compute, project, region, network_self_link, network_details=None):
    subnetworks = []
    if network_details:
        links = network_details.get('subnetworks', [])
        if links:
            subnetworks.extend(fetch_subnetworks_from_links(compute, links, preferred_region=region))
    if subnetworks:
        return subnetworks
    try:
        request = compute.subnetworks().list(project=project, region=region)
        while request is not None:
            response = request.execute()
            for subnet in response.get('items', []):
                if subnet.get('network') != network_self_link:
                    continue
                subnetworks.append(subnet)
            request = compute.subnetworks().list_next(previous_request=request, previous_response=response)
    except HttpError as e:
        print(f"Unable to list subnetworks in {region}: {e}")
    if subnetworks:
        return subnetworks
    try:
        request = compute.subnetworks().aggregatedList(project=project)
        while request is not None:
            response = request.execute()
            for region_name, region_subnets in response.get('items', {}).items():
                for subnet in region_subnets.get('subnetworks', []):
                    if subnet.get('network') == network_self_link:
                        if not region or subnet.get('region', '').endswith(f"/{region}"):
                            subnetworks.append(subnet)
            request = compute.subnetworks().aggregatedList_next(previous_request=request, previous_response=response)
    except HttpError as e:
        print("Unable to list subnetworks via aggregated API:", e)
    return subnetworks


def prompt_cidr_list(message, default=None):
    while True:
        suffix = f" [{default}]" if default else ""
        raw = input(f"{message}{suffix}: ").strip()
        if not raw and default:
            raw = default
        cidrs = [cidr.strip() for cidr in (raw or '').split(',') if cidr.strip()]
        if cidrs:
            return cidrs
        print("At least one CIDR range is required.")


def normalize_cidr_list(value):
    if value is None:
        return []
    if isinstance(value, (list, tuple, set)):
        return [str(item).strip() for item in value if str(item).strip()]
    return [segment.strip() for segment in str(value).split(',') if segment.strip()]


def interactive_setup(args):
    required_for_run = ['aws_vpc_id', 'aws_vpc_cidr', 'aws_region', 'gcp_project', 'gcp_network', 'gcp_region', 'gcp_subnets']
    needs_interactive = any(not getattr(args, field) for field in required_for_run)
    if not needs_interactive:
        return

    print("\n=== Interactive VPN configuration ===")

    # AWS region
    available_regions = sorted(set(boto3.Session().get_available_regions('ec2')))
    region_options = [{'label': region, 'value': region} for region in available_regions]
    default_region_label = f"{args.aws_region} (current)" if args.aws_region else None
    args.aws_region = prompt_select_option(
        "Select AWS region", region_options, allow_manual=True,
        default_value=args.aws_region if args.aws_region else None,
        default_label=default_region_label
    )
    print(f"AWS region selected: {args.aws_region}")

    aws_session = boto3.Session(region_name=args.aws_region)
    ec2 = aws_session.client('ec2')

    # AWS VPC
    vpc_options = list_aws_vpcs(ec2)
    if not vpc_options:
        raise RuntimeError("No VPCs found in the selected AWS region.")
    default_vpc = None
    default_vpc_label = None
    if args.aws_vpc_id:
        resp = ec2.describe_vpcs(VpcIds=[args.aws_vpc_id])
        if resp.get('Vpcs'):
            default_vpc = resp['Vpcs'][0]
            vpc_name = get_tag_value(default_vpc)
            default_vpc_label = f"{args.aws_vpc_id} ({default_vpc.get('CidrBlock')} - {vpc_name or 'no name'})"
    selected_vpc = prompt_select_option(
        "Select AWS VPC",
        vpc_options,
        allow_manual=True,
        default_value=default_vpc,
        default_label=default_vpc_label
    )
    if isinstance(selected_vpc, dict):
        selected_vpc_data = selected_vpc
    else:
        resp = ec2.describe_vpcs(VpcIds=[selected_vpc])
        if not resp.get('Vpcs'):
            raise RuntimeError(f"AWS VPC {selected_vpc} not found.")
        selected_vpc_data = resp['Vpcs'][0]
    args.aws_vpc_id = selected_vpc_data['VpcId']
    selected_vpc_name = get_tag_value(selected_vpc_data)
    print(f"AWS VPC selected: {args.aws_vpc_id} ({selected_vpc_data.get('CidrBlock')} - {selected_vpc_name or 'no name'})")

    if not args.aws_vpc_cidr:
        args.aws_vpc_cidr = selected_vpc_data.get('CidrBlock')
        print(f"Detected AWS VPC CIDR: {args.aws_vpc_cidr}")

    # AWS subnet selection
    aws_subnet_items = list_aws_subnets(ec2, args.aws_vpc_id)
    args.aws_selected_subnet_ids = None
    if aws_subnet_items:
        selected_subnets, used_all = prompt_multi_select(
            "Select AWS subnets whose route tables should receive GCP routes", aws_subnet_items, allow_all=True
        )
        if used_all or len(selected_subnets) == len(aws_subnet_items):
            print("Selected all AWS subnets.")
            args.aws_selected_subnet_ids = None
        else:
            args.aws_selected_subnet_ids = [subnet['SubnetId'] for subnet in selected_subnets]
            print("AWS subnets chosen:", ", ".join(args.aws_selected_subnet_ids))
    else:
        print("No subnets found in the VPC; route updates will target the main route table.")

    # GCP project
    if not args.gcp_project:
        default_project = os.environ.get('GCP_PROJECT') or os.environ.get('GOOGLE_CLOUD_PROJECT')
        args.gcp_project = prompt_with_default("Enter GCP project ID", default_project)

    compute = get_compute_service()

    # GCP region
    region_names = list_gcp_regions(compute, args.gcp_project)
    region_options = [{'label': name, 'value': name} for name in region_names] if region_names else None
    if region_options:
        default_region_label = f"{args.gcp_region} (current)" if args.gcp_region else None
        args.gcp_region = prompt_select_option(
            "Select GCP region",
            region_options,
            allow_manual=True,
            default_value=args.gcp_region if args.gcp_region else None,
            default_label=default_region_label
        )
    else:
        args.gcp_region = prompt_with_default("Enter GCP region", args.gcp_region or 'asia-south1')
    print(f"GCP region selected: {args.gcp_region}")

    # GCP network
    network_options = list_gcp_networks(compute, args.gcp_project)
    selected_network_details = None
    if network_options:
        default_network_label = f"{args.gcp_network} (current)" if args.gcp_network else None
        args.gcp_network = prompt_select_option(
            "Select GCP VPC network",
            network_options,
            allow_manual=True,
            default_value=args.gcp_network if args.gcp_network else None,
            default_label=default_network_label
        )
        if isinstance(args.gcp_network, dict):
            selected_network_details = args.gcp_network
            args.gcp_network = selected_network_details['name']
    else:
        args.gcp_network = prompt_with_default("Enter GCP VPC network name", args.gcp_network)
    if selected_network_details is None:
        try:
            selected_network_details = compute.networks().get(project=args.gcp_project, network=args.gcp_network).execute()
        except HttpError as e:
            print(f"Unable to fetch network details for {args.gcp_network}: {e}")
            selected_network_details = {}
    print(f"GCP network selected: {args.gcp_network}")

    # GCP subnet selection
    network_self_link = selected_network_details.get('selfLink') or f"projects/{args.gcp_project}/global/networks/{args.gcp_network}"
    gcp_subnets = list_gcp_subnets(compute, args.gcp_project, args.gcp_region, network_self_link, selected_network_details)
    if gcp_subnets:
        subnet_items = []
        for subnet in gcp_subnets:
            label = f"{subnet['name']} ({subnet.get('ipCidrRange')})"
            subnet_items.append({'label': label, 'value': subnet})
        selected_subnets, _ = prompt_multi_select(
            "Select GCP subnetworks to advertise to AWS", subnet_items, allow_all=True
        )
        args.gcp_subnets = [subnet['ipCidrRange'] for subnet in selected_subnets]
        print("Selected GCP CIDRs:", ", ".join(args.gcp_subnets))
    else:
        print("No subnetworks discovered; please enter CIDR ranges manually.")
        args.gcp_subnets = prompt_cidr_list("Enter comma-separated GCP subnet CIDRs")

def determine_route_tables(route_tables, subnet_ids=None):
    subnet_ids = subnet_ids or []
    subnet_to_rt = {}
    main_rt = None
    for rt in route_tables:
        for assoc in rt.get('Associations', []):
            if assoc.get('Main'):
                main_rt = rt['RouteTableId']
            subnet_id = assoc.get('SubnetId')
            if subnet_id:
                subnet_to_rt[subnet_id] = rt['RouteTableId']
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


def update_route_tables_with_prefixes(ec2, vpc_id, prefixes, vgw_id, subnet_ids=None):
    if not prefixes:
        return [], []
    resp = ec2.describe_route_tables(Filters=[{'Name': 'vpc-id', 'Values': [vpc_id]}])
    route_tables = resp.get('RouteTables', [])
    if not route_tables:
        print("No route tables found for the VPC; skipping route updates.")
        return [], []
    target_route_table_ids = determine_route_tables(route_tables, subnet_ids)
    if not target_route_table_ids:
        print("No matching route tables found for the selected subnets; skipping route updates.")
        return [], []
    created_routes = []
    for rt_id in target_route_table_ids:
        for prefix in prefixes:
            try:
                ec2.create_route(RouteTableId=rt_id, DestinationCidrBlock=prefix, GatewayId=vgw_id)
                created_routes.append({'RouteTableId': rt_id, 'DestinationCidrBlock': prefix})
            except ClientError:
                # Ignore errors for routes that already exist.
                pass
    return target_route_table_ids, created_routes

# ---------- Main ----------
def parse_args():
    p = argparse.ArgumentParser()
    p.add_argument('--aws-vpc-id')
    p.add_argument('--aws-vpc-cidr')
    p.add_argument('--aws-region', default=os.environ.get('AWS_DEFAULT_REGION','ap-south-1'))
    p.add_argument('--gcp-project')
    p.add_argument('--gcp-network')
    p.add_argument('--gcp-region', default='asia-south1')
    p.add_argument('--gcp-subnets', help='comma separated subnet cidrs e.g. 10.1.0.0/23,10.1.2.0/23')
    p.add_argument('--name-prefix', default='rohit-automation')
    return p.parse_args()

def main():
    global args
    args = parse_args()
    interactive_setup(args)
    required_after_prompt = ['aws_vpc_id', 'aws_vpc_cidr', 'aws_region', 'gcp_project', 'gcp_network', 'gcp_region', 'gcp_subnets']
    missing_after_prompt = [field for field in required_after_prompt if not getattr(args, field)]
    if missing_after_prompt:
        raise RuntimeError(f"Missing required inputs after prompting: {', '.join(missing_after_prompt)}.")

    aws_session = boto3.Session(region_name=args.aws_region)
    ec2 = aws_session.client('ec2')

    if not args.aws_vpc_cidr:
        resp = ec2.describe_vpcs(VpcIds=[args.aws_vpc_id])
        if not resp.get('Vpcs'):
            raise RuntimeError(f"AWS VPC {args.aws_vpc_id} not found.")
        args.aws_vpc_cidr = resp['Vpcs'][0].get('CidrBlock')
        print(f"Detected AWS VPC CIDR from API: {args.aws_vpc_cidr}")

    resource_names = build_resource_names(args.name_prefix)
    cleanup = CleanupManager()
    compute = get_compute_service()
    reserved_name = resource_names['gcp_address']
    updated_route_tables = []
    gcp_gateway_result = {}
    selected_subnet_ids = getattr(args, 'aws_selected_subnet_ids', None)
    try:
        # ---------- GCP: reserve external IP ----------
        print("Reserving GCP external IP...")
        gcp_ip, gcp_ip_created = reserve_address(compute, args.gcp_project, args.gcp_region, reserved_name)
        print("Reserved GCP IP:", gcp_ip)
        if gcp_ip_created:
            cleanup.add(
                "GCP reserved address",
                delete_gcp_address,
                compute,
                args.gcp_project,
                args.gcp_region,
                reserved_name
            )

        # ---------- AWS: ensure VGW attached ----------
        print("Ensuring VGW exists and attached to VPC...")
        vgw_id, vgw_created = ensure_vgw_attached(ec2, args.aws_vpc_id, resource_names['aws_vgw'])
        print("VGW:", vgw_id)
        if vgw_created:
            cleanup.add("AWS VGW", delete_aws_vgw, ec2, vgw_id, args.aws_vpc_id)

        # ---------- AWS: create CGW pointing to GCP IP ----------
        print("Creating/ensuring Customer Gateway (GCP public IP)...")
        cgw_id, cgw_created = ensure_customer_gateway(ec2, gcp_ip, resource_names['aws_cgw'])
        print("Customer Gateway ID:", cgw_id)
        if cgw_created:
            cleanup.add("AWS Customer Gateway", delete_aws_customer_gateway, ec2, cgw_id)

        # ---------- AWS: create VPN connection (static) ----------
        print("Creating AWS VPN connection (static)...")
        gcp_prefixes = normalize_cidr_list(args.gcp_subnets)
        if not gcp_prefixes:
            raise RuntimeError("No GCP subnet CIDRs provided.")
        vpn_name = resource_names['aws_vpn']
        vpn_id, vpn_created = create_vpn_connection_static(ec2, cgw_id, vgw_id, vpn_name, gcp_prefixes)
        print("VPN Connection ID:", vpn_id)
        if vpn_created:
            cleanup.add("AWS VPN connection", delete_aws_vpn_connection, ec2, vpn_id)

        # Wait for AWS to populate CustomerGatewayConfiguration with outside IPs and PSKs
        print("Waiting for AWS to generate tunnel info (outside IPs & PSKs)...")
        outs, psks, awsvpn = wait_for_aws_vpn_tunnels(ec2, vpn_id, timeout=600)
        print("AWS Outside IPs:", outs)
        print("AWS PSKs:", psks)
        if len(outs) < 2 or len(psks) < 2:
            raise RuntimeError(
                "Expected two AWS VPN tunnels but AWS has not provided two outside IPs/PSKs yet. "
                "Verify the VPN connection status in the AWS console and try again."
            )

        # Build two GCP tunnels mapping to the AWS outside IPs
        tunnels = []
        for i, outside in enumerate(outs[:2]):
            psk = psks[i] if i < len(psks) else psks[0]
            ike_version = 1  # Classic VPN supports only IKEv1 reliably.
            tunnels.append({
                'name': f"{resource_names['gcp_tunnel_prefix']}-{i+1}",
                'peer_ip': outside,
                'psk': psk,
                'remote_ranges': [args.aws_vpc_cidr],
                'local_ranges': gcp_prefixes,
                'ike_version': ike_version,
            })

        # ---------- GCP: create Classic VPN gateway and tunnels ----------
        print("Creating GCP Classic VPN gateway and tunnels...")
        target_gateway_link = f"projects/{args.gcp_project}/regions/{args.gcp_region}/targetVpnGateways/{resource_names['gcp_gateway']}"
        address_link = f"projects/{args.gcp_project}/regions/{args.gcp_region}/addresses/{reserved_name}"
        forwarding_specs = [
            {'name': resource_names['gcp_forwarding_esp'], 'ip_protocol': 'ESP'},
            {'name': resource_names['gcp_forwarding_udp500'], 'ip_protocol': 'UDP', 'port_range': '500-500'},
            {'name': resource_names['gcp_forwarding_udp4500'], 'ip_protocol': 'UDP', 'port_range': '4500-4500'},
        ]
        gcp_gateway_result = create_classic_vpn_gateway_and_tunnels(
            compute,
            args.gcp_project,
            args.gcp_region,
            resource_names['gcp_gateway'],
            reserved_name,
            gcp_ip,
            tunnels,
            forwarding_specs=forwarding_specs,
            forwarding_address=address_link,
            target_gateway_link=target_gateway_link
        )
        if gcp_gateway_result['gateway_created']:
            cleanup.add(
                "GCP Classic VPN gateway",
                delete_gcp_vpn_gateway,
                compute,
                args.gcp_project,
                args.gcp_region,
                resource_names['gcp_gateway']
            )
        for tunnel_name in gcp_gateway_result['tunnels_created']:
            cleanup.add(
                f"GCP VPN tunnel {tunnel_name}",
                delete_gcp_vpn_tunnel,
                compute,
                args.gcp_project,
                args.gcp_region,
                tunnel_name
            )
        print("Created/ensured tunnels:", gcp_gateway_result['tunnels_created'])
        for fr_name in gcp_gateway_result.get('forwarding_created', []):
            cleanup.add(
                f"GCP forwarding rule {fr_name}",
                delete_forwarding_rule,
                compute,
                args.gcp_project,
                args.gcp_region,
                fr_name
            )

        # ---------- GCP: create routes for AWS CIDR ----------
        tunnel_links = [
            f"projects/{args.gcp_project}/regions/{args.gcp_region}/vpnTunnels/{name}"
            for name in gcp_gateway_result['tunnels_present']
        ]
        if not tunnel_links:
            raise RuntimeError("No GCP tunnels exist to attach routes.")
        network_link = f"projects/{args.gcp_project}/global/networks/{args.gcp_network}"
        route_specs = [{
            'name': f"{resource_names['gcp_route_prefix']}-primary",
            'destRange': args.aws_vpc_cidr,
            'nextHopVpnTunnel': tunnel_links[0],
            'network': network_link,
        }]
        print("Ensuring GCP routes exist for AWS CIDR...")
        created_gcp_routes = ensure_gcp_routes(compute, args.gcp_project, route_specs, network_self_link=network_link)
        for route_name in created_gcp_routes:
            cleanup.add(
                f"GCP route {route_name}",
                delete_gcp_route,
                compute,
                args.gcp_project,
                route_name
            )

        # ---------- GCP: create firewall to allow AWS ranges into GCP -->
        print("Creating GCP firewall to allow AWS CIDR into GCP VPC...")
        firewall_name = resource_names['gcp_firewall']
        firewall_created = ensure_firewall_rule(compute, args.gcp_project, firewall_name, args.gcp_network, [args.aws_vpc_cidr])
        if firewall_created:
            cleanup.add(
                "GCP firewall rule",
                delete_gcp_firewall,
                compute,
                args.gcp_project,
                firewall_name
            )

        # ---------- AWS: ensure route table entries point to VGW ----------
        print("Adding routes in AWS route table to point to VGW...")
        updated_route_tables, created_routes = update_route_tables_with_prefixes(
            ec2, args.aws_vpc_id, gcp_prefixes, vgw_id, selected_subnet_ids
        )
        if created_routes:
            cleanup.add("AWS routes", delete_aws_routes, ec2, created_routes)
        print("Updated route tables:", updated_route_tables)
    except Exception:
        print("Error encountered. Rolling back created resources...")
        cleanup.run()
        raise

    metadata = {
        "timestamp_utc": datetime.utcnow().isoformat() + "Z",
        "name_prefix": args.name_prefix,
        "aws_region": args.aws_region,
        "aws_vpc_id": args.aws_vpc_id,
        "aws_vpc_cidr": args.aws_vpc_cidr,
        "aws_selected_subnet_ids": selected_subnet_ids,
        "gcp_project": args.gcp_project,
        "gcp_region": args.gcp_region,
        "gcp_network": args.gcp_network,
        "gcp_subnets": gcp_prefixes,
        "resources": {
            "gcp_reserved_ip": gcp_ip,
            "aws_vgw_id": vgw_id,
            "aws_cgw_id": cgw_id,
            "aws_vpn_id": vpn_id,
            "gcp_tunnels": gcp_gateway_result.get('tunnels_present'),
            "gcp_forwarding_rules": [spec['name'] for spec in forwarding_specs],
            "gcp_route": f"{resource_names['gcp_route_prefix']}-primary",
            "gcp_firewall": resource_names['gcp_firewall'],
            "aws_route_tables": updated_route_tables,
        }
    }
    _write_metadata(args.name_prefix, metadata)
    print("Done. VPN should be established shortly. Verify AWS and GCP consoles.")

if __name__ == '__main__':
    main()
