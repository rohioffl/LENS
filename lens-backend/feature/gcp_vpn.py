"""Shared helpers for GCP network discovery and VPN planning."""

from __future__ import annotations

import base64
import ipaddress
import json
from typing import Any, Dict, Iterable, List, Optional

from . import terraform_vpc as vpc_mod

try:  # pragma: no cover - optional dependency
    from google.cloud import resourcemanager_v3
except ImportError:  # pragma: no cover - dependency hint
    resourcemanager_v3 = None

ensure_compute_client = vpc_mod.ensure_compute_client
compute_v1 = vpc_mod.compute_v1
gcp_exceptions = vpc_mod.gcp_exceptions
service_account = vpc_mod.service_account

SCOPES = ["https://www.googleapis.com/auth/cloud-platform"]


class GcpVpnError(RuntimeError):
    """Raised when VPN planning or discovery fails."""


def compute_covering_cidr(cidrs: Iterable[str]) -> str:
    """Return a single CIDR that covers all provided CIDRs."""
    networks = [ipaddress.ip_network(str(c).strip(), strict=False) for c in cidrs if str(c).strip()]
    if not networks:
        raise GcpVpnError("Cannot compute covering CIDR for an empty list.")
    min_ip = min(net.network_address for net in networks)
    max_ip = max(net.broadcast_address for net in networks)
    summary = list(ipaddress.summarize_address_range(min_ip, max_ip))
    if not summary:
        raise GcpVpnError("Unable to compute a covering CIDR.")
    combined = summary[0]
    for net in summary[1:]:
        while not combined.supernet_of(net):
            combined = combined.supernet()
    return str(combined)


def _decode_service_key(raw_value: str) -> Dict[str, Any]:
    text = (raw_value or "").strip()
    if not text:
        raise GcpVpnError("GCP service key is required.")
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        try:
            decoded = base64.b64decode(text).decode("utf-8")
        except Exception as exc:  # pragma: no cover - guardrail
            raise GcpVpnError("GCP service key must be valid JSON or base64 encoded JSON.") from exc
        try:
            return json.loads(decoded)
        except json.JSONDecodeError as exc:  # pragma: no cover - guardrail
            raise GcpVpnError("Decoded GCP service key is not valid JSON.") from exc


def _build_gcp_credentials(service_key: str):
    ensure_compute_client()
    info = _decode_service_key(service_key)
    if service_account is None:  # pragma: no cover - dependency hint
        raise GcpVpnError("google-auth is required to work with service-account keys.")
    credentials = service_account.Credentials.from_service_account_info(info, scopes=SCOPES)
    project_id = info.get("project_id")
    if not project_id:
        raise GcpVpnError("Service account JSON is missing 'project_id'.")
    return credentials, project_id


def _ensure_resource_manager_client() -> None:
    if resourcemanager_v3 is None:
        raise GcpVpnError(
            "google-cloud-resource-manager is required to list GCP projects. "
            "Install with `pip install google-cloud-resource-manager`."
        )
    if gcp_exceptions is None:
        raise GcpVpnError(
            "google-api-core is required to talk to the Resource Manager API. "
            "Install with `pip install google-api-core`."
        )


def _resolve_subnetwork_details(credentials, project: str, subnetwork_url: str, region_filter: Optional[str] = None) -> Optional[Dict[str, Any]]:
    parts = subnetwork_url.split("/")
    try:
        region_idx = parts.index("regions")
        region = parts[region_idx + 1]
        name = parts[-1]
    except (ValueError, IndexError):
        region = "unknown"
        name = parts[-1]
    if region_filter and region_filter != region:
        # Skip subnets outside the requested region entirely.
        return None
    subnet_client = compute_v1.SubnetworksClient(credentials=credentials)
    cidr = None
    try:
        subnet = subnet_client.get(project=project, region=region, subnetwork=name)
        cidr = getattr(subnet, "ip_cidr_range", None)
    except gcp_exceptions.GoogleAPICallError:
        cidr = None
    return {"name": name, "region": region, "cidr": cidr}


def list_gcp_networks(service_key: str, project_id: Optional[str] = None) -> tuple[str, List[Dict[str, Any]]]:
    """Return all GCP VPC networks visible to the provided service account."""
    if compute_v1 is None:  # pragma: no cover - dependency hint
        raise GcpVpnError("google-cloud-compute is required to list GCP VPCs.")
    credentials, inferred_project = _build_gcp_credentials(service_key)
    project = project_id or inferred_project
    client = compute_v1.NetworksClient(credentials=credentials)
    networks: List[Dict[str, Any]] = []
    try:
        for network in client.list(project=project):
            raw_subnets = getattr(network, "subnetworks", []) or []
            subnetworks = [str(item) for item in raw_subnets]
            networks.append(
                {
                    "name": network.name,
                    "auto_create_subnetworks": bool(getattr(network, "auto_create_subnetworks", False)),
                    "routing_mode": getattr(getattr(network, "routing_config", None), "routing_mode", "REGIONAL"),
                    "subnet_count": len(subnetworks),
                    "subnetworks": subnetworks,
                }
            )
    except gcp_exceptions.GoogleAPICallError as exc:  # pragma: no cover - API guard
        raise GcpVpnError(f"Failed to list GCP networks: {exc}") from exc
    return project, networks


def list_gcp_projects(service_key: str) -> tuple[str, List[Dict[str, Any]]]:
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
            raise GcpVpnError(f"Failed to list GCP projects: {exc}") from exc

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


def get_gcp_network(service_key: str, project_id: str, network_name: str, region_filter: Optional[str] = None) -> Dict[str, Any]:
    if not network_name:
        raise GcpVpnError("A GCP VPC network must be selected.")
    credentials, inferred_project = _build_gcp_credentials(service_key)
    project = project_id or inferred_project
    client = compute_v1.NetworksClient(credentials=credentials)
    try:
        network = client.get(project=project, network=network_name)
    except gcp_exceptions.NotFound as exc:
        raise GcpVpnError(f"GCP network '{network_name}' not found in project '{project}'.") from exc
    subnetworks: List[Dict[str, Any]] = []
    for url in getattr(network, "subnetworks", []) or []:
        details = _resolve_subnetwork_details(credentials, project, url, region_filter=region_filter)
        if details:
            subnetworks.append(details)
    return {
        "name": network.name,
        "project": project,
        "auto_create_subnetworks": getattr(network, "auto_create_subnetworks", False),
        "routing_mode": getattr(getattr(network, "routing_config", None), "routing_mode", "REGIONAL"),
        "subnetworks": subnetworks,
        "self_link": getattr(network, "self_link", ""),
    }
