#!/usr/bin/env python3
"""ECS → GKE Terraform helper inspired by terraform-vpc.py."""
import argparse
import json
import math
import os
import re
import shlex
import string
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple


def _author_signature() -> int:
    return sum(value << (idx * 8) for idx, value in enumerate((0x52, 0x6F, 0x68, 0x69, 0x74)))

DEFAULT_MACHINE_TYPE = "e2-standard-4"
DEFAULT_RELEASE_CHANNEL = "REGULAR"
DEFAULT_MASTER_IPV4_CIDR_BLOCK = "172.16.0.0/28"

MACHINE_TYPE_CAPACITY: Dict[str, Tuple[float, int]] = {
    "e2-medium": (2.0, 4096),
    "e2-standard-2": (2.0, 8192),
    "e2-standard-4": (4.0, 16384),
    "e2-standard-8": (8.0, 32768),
    "n1-standard-2": (2.0, 7680),
    "n1-standard-4": (4.0, 15360),
    "n1-standard-8": (8.0, 30720),
    "n2-standard-4": (4.0, 16384),
    "n2-standard-8": (8.0, 32768),
}

AWS_TO_GCP_REGION: Dict[str, str] = {
    "us-east-1": "us-east1",
    "us-east-2": "us-east1",
    "us-west-1": "us-west1",
    "us-west-2": "us-west1",
    "us-west-3": "us-west2",
    "us-west-4": "us-west2",
    "af-south-1": "africa-south1",
    "ap-south-1": "asia-south1",
    "ap-south-2": "asia-south1",
    "ap-southeast-1": "asia-southeast1",
    "ap-southeast-2": "australia-southeast1",
    "ap-southeast-3": "asia-southeast2",
    "ap-southeast-4": "australia-southeast2",
    "ap-northeast-1": "asia-northeast1",
    "ap-northeast-2": "asia-northeast3",
    "ap-northeast-3": "asia-northeast2",
    "ap-east-1": "asia-east2",
    "ca-central-1": "northamerica-northeast1",
    "ca-west-1": "northamerica-northeast2",
    "eu-west-1": "europe-west1",
    "eu-west-2": "europe-west2",
    "eu-west-3": "europe-west3",
    "eu-central-1": "europe-west3",
    "eu-central-2": "europe-west8",
    "eu-north-1": "europe-north1",
    "eu-south-1": "europe-southwest1",
    "eu-south-2": "europe-southwest1",
    "me-south-1": "me-central1",
    "me-central-1": "me-central2",
    "sa-east-1": "southamerica-east1",
}

GCP_REGION_ZONES: Dict[str, List[str]] = {
    "africa-south1": ["africa-south1-a", "africa-south1-b", "africa-south1-c"],
    "asia-east2": ["asia-east2-a", "asia-east2-b", "asia-east2-c"],
    "asia-northeast1": ["asia-northeast1-a", "asia-northeast1-b", "asia-northeast1-c"],
    "asia-northeast2": ["asia-northeast2-a", "asia-northeast2-b", "asia-northeast2-c"],
    "asia-northeast3": ["asia-northeast3-a", "asia-northeast3-b", "asia-northeast3-c"],
    "asia-south1": ["asia-south1-a", "asia-south1-b", "asia-south1-c"],
    "asia-southeast1": ["asia-southeast1-a", "asia-southeast1-b", "asia-southeast1-c"],
    "asia-southeast2": ["asia-southeast2-a", "asia-southeast2-b", "asia-southeast2-c"],
    "australia-southeast1": ["australia-southeast1-a", "australia-southeast1-b", "australia-southeast1-c"],
    "australia-southeast2": ["australia-southeast2-a", "australia-southeast2-b", "australia-southeast2-c"],
    "europe-north1": ["europe-north1-a", "europe-north1-b", "europe-north1-c"],
    "europe-southwest1": ["europe-southwest1-a", "europe-southwest1-b", "europe-southwest1-c"],
    "europe-west1": ["europe-west1-b", "europe-west1-c", "europe-west1-d"],
    "europe-west2": ["europe-west2-a", "europe-west2-b", "europe-west2-c"],
    "europe-west3": ["europe-west3-a", "europe-west3-b", "europe-west3-c"],
    "europe-west8": ["europe-west8-a", "europe-west8-b", "europe-west8-c"],
    "me-central1": ["me-central1-a", "me-central1-b", "me-central1-c"],
    "me-central2": ["me-central2-a", "me-central2-b", "me-central2-c"],
    "northamerica-northeast1": ["northamerica-northeast1-a", "northamerica-northeast1-b", "northamerica-northeast1-c"],
    "northamerica-northeast2": ["northamerica-northeast2-a", "northamerica-northeast2-b", "northamerica-northeast2-c"],
    "southamerica-east1": ["southamerica-east1-a", "southamerica-east1-b", "southamerica-east1-c"],
    "us-central1": ["us-central1-a", "us-central1-b", "us-central1-c", "us-central1-f"],
    "us-east1": ["us-east1-b", "us-east1-c", "us-east1-d"],
    "us-east4": ["us-east4-a", "us-east4-b", "us-east4-c"],
    "us-west1": ["us-west1-a", "us-west1-b", "us-west1-c"],
    "us-west2": ["us-west2-a", "us-west2-b", "us-west2-c"],
    "us-west3": ["us-west3-a", "us-west3-b", "us-west3-c"],
    "us-west4": ["us-west4-a", "us-west4-b", "us-west4-c"],
}

@dataclass
class EcsServiceDetail:
    name: str
    service: Dict[str, Any]
    task_definition: Dict[str, Any]
    desired_count: int
    cpu_vcpu: float
    memory_mb: int
    containers: List[Dict[str, Any]] = field(default_factory=list)

    def summary_line(self) -> str:
        launch = self.service.get("launchType", "") or ",".join(self.task_definition.get("requiresCompatibilities", []))
        containers_desc = []
        for container in self.containers:
            ports = ",".join(str(p) for p in container.get("ports", [])) or "none"
            containers_desc.append(
                f"{container['name']} (img={container['image']}, cpu={container.get('cpu') or '-'} units, "
                f"mem={container.get('memory') or container.get('memoryReservation') or '-'} MB, ports={ports})"
            )
        container_str = "; ".join(containers_desc) if containers_desc else "no containers"
        return (
            f"service={self.name} desired={self.desired_count} launch={launch or 'unspecified'} "
            f"task_cpu≈{self.cpu_vcpu:.2f} vCPU task_mem={self.memory_mb} MB | containers: {container_str}"
        )

    def to_payload(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "desired_count": self.desired_count,
            "launch_type": self.service.get("launchType"),
            "task_definition_arn": self.service.get("taskDefinition"),
            "task_cpu_vcpu": round(self.cpu_vcpu, 2),
            "task_memory_mb": self.memory_mb,
            "network_configuration": self.service.get("networkConfiguration"),
            "service_registries": self.service.get("serviceRegistries"),
            "scheduling_strategy": self.service.get("schedulingStrategy"),
            "deployment_configuration": self.service.get("deploymentConfiguration"),
            "containers": self.containers,
        }


# ---------- General helpers ----------

def sanitize_name(value: str, default: str = "ecs-cluster") -> str:
    value = (value or default).lower()
    value = re.sub(r"[^a-z0-9-]", "-", value)
    value = value.strip("-")
    return value[:61] if value else default


def sanitize_label_value(value: str) -> str:
    value = value or "ecs-cluster"
    cleaned = re.sub(r"[^a-z0-9_-]", "-", value.lower())
    cleaned = cleaned.strip("-")
    return cleaned or "ecs-cluster"


def configure_gcp_credentials(path: Optional[str]) -> None:
    if not path:
        return
    if not os.path.exists(path):
        raise SystemExit(f"Credential file not found: {path}")
    os.environ.setdefault("GOOGLE_APPLICATION_CREDENTIALS", path)


def run_aws_cli(command: Sequence[str]) -> Optional[str]:
    try:
        result = subprocess.run(command, check=True, capture_output=True, text=True)
    except subprocess.CalledProcessError as exc:
        stderr = exc.stderr.strip() if exc.stderr else ""
        cmd_display = " ".join(command)
        if "ClusterNotFoundException" in stderr:
            print(
                f"❌ AWS reports cluster not found when executing: {cmd_display}\n"
                "   Verify the ECS cluster name/region and credentials."
            )
        else:
            print(
                f"❌ AWS CLI command failed (exit code {exc.returncode}): {cmd_display}\n"
                f"   stderr: {stderr or 'No additional error output'}"
            )
        return None
    return result.stdout


def safe_int(value: Any) -> int:
    try:
        return int(str(value))
    except (TypeError, ValueError):
        return 0


def extract_task_resources(task_def: Dict[str, Any]) -> Tuple[float, int]:
    task_level_cpu = safe_int(task_def.get("cpu"))
    task_level_memory = safe_int(task_def.get("memory"))
    container_cpus = [safe_int(c.get("cpu")) for c in task_def.get("containerDefinitions", [])]
    container_mems = [
        safe_int(c.get("memory")) or safe_int(c.get("memoryReservation"))
        for c in task_def.get("containerDefinitions", [])
    ]
    total_cpu_units = task_level_cpu or sum(container_cpus)
    total_memory_mb = task_level_memory or sum(container_mems)
    cpu_vcpu = total_cpu_units / 1024 if total_cpu_units else 0
    return cpu_vcpu, total_memory_mb


def describe_ecs_cluster(cluster_name: str, region: str) -> Dict[str, Any]:
    response = run_aws_cli([
        "aws",
        "ecs",
        "describe-clusters",
        "--clusters",
        cluster_name,
        "--region",
        region,
    ])
    if not response:
        return {}
    data = json.loads(response)
    clusters = data.get("clusters", [])
    failures = data.get("failures", [])
    if failures:
        reason = failures[0].get("reason", "unknown")
        print(f"❌ Unable to access cluster '{cluster_name}': {reason}")
        return {}
    return clusters[0] if clusters else {}


def extract_container_details(container: Dict[str, Any]) -> Dict[str, Any]:
    env_pairs = container.get("environment", [])
    env = {pair.get("name"): pair.get("value") for pair in env_pairs if pair.get("name")}
    ports = [entry.get("containerPort") for entry in container.get("portMappings", []) if entry.get("containerPort")]
    return {
        "name": container.get("name", "container"),
        "image": container.get("image", "unknown"),
        "cpu": safe_int(container.get("cpu")),
        "memory": safe_int(container.get("memory")),
        "memoryReservation": safe_int(container.get("memoryReservation")),
        "essential": container.get("essential", True),
        "env": env,
        "ports": ports,
        "logConfiguration": container.get("logConfiguration"),
        "healthCheck": container.get("healthCheck"),
    }


def collect_ecs_services(cluster_name: str, region: str) -> Tuple[Dict[str, Any], List[EcsServiceDetail]]:
    cluster_details = describe_ecs_cluster(cluster_name, region)
    if not cluster_details:
        return {}, []

    svc_list_raw = run_aws_cli([
        "aws",
        "ecs",
        "list-services",
        "--cluster",
        cluster_name,
        "--region",
        region,
    ])
    if not svc_list_raw:
        return cluster_details, []

    service_arns = json.loads(svc_list_raw).get("serviceArns", [])
    services: List[EcsServiceDetail] = []
    for svc_arn in service_arns:
        svc_name = svc_arn.split("/")[-1]
        print(f"➡️ Processing service: {svc_name}")
        svc_desc_raw = run_aws_cli([
            "aws",
            "ecs",
            "describe-services",
            "--cluster",
            cluster_name,
            "--services",
            svc_name,
            "--region",
            region,
        ])
        if not svc_desc_raw:
            continue
        svc_data_list = json.loads(svc_desc_raw).get("services", [])
        if not svc_data_list:
            print(f"Service {svc_name} not found, skipping.")
            continue
        svc_data = svc_data_list[0]
        task_def_arn = svc_data.get("taskDefinition")
        if not task_def_arn:
            print(f"No task definition for service {svc_name}, skipping.")
            continue
        task_def_raw = run_aws_cli([
            "aws",
            "ecs",
            "describe-task-definition",
            "--task-definition",
            task_def_arn,
            "--region",
            region,
        ])
        if not task_def_raw:
            continue
        task_def = json.loads(task_def_raw).get("taskDefinition")
        if not task_def:
            print(f"Task definition {task_def_arn} not found, skipping.")
            continue

        cpu_vcpu, mem_mb = extract_task_resources(task_def)
        desired = max(safe_int(svc_data.get("desiredCount")), 0)
        containers = [extract_container_details(c) for c in task_def.get("containerDefinitions", [])]
        services.append(EcsServiceDetail(
            name=svc_name,
            service=svc_data,
            task_definition=task_def,
            desired_count=desired,
            cpu_vcpu=cpu_vcpu,
            memory_mb=mem_mb,
            containers=containers,
        ))
    return cluster_details, services


def list_ecs_subnets_with_names(region: str, subnet_ids: Iterable[str]) -> Dict[str, Dict[str, Optional[str]]]:
    """Return mapping {subnet_id: {"name": name_or_id, "zone": aws_zone}} from AWS."""
    if not subnet_ids:
        return {}

    cmd = [
        "aws",
        "ec2",
        "describe-subnets",
        "--subnet-ids",
        *subnet_ids,
        "--region",
        region,
        "--query",
        "Subnets[*].{Id:SubnetId,Name:Tags[?Key=='Name']|[0].Value,Zone:AvailabilityZone}",
        "--output",
        "json",
    ]
    result = run_aws_cli(cmd)
    if not result:
        return {subnet: {"name": subnet, "zone": None} for subnet in subnet_ids}

    data = json.loads(result)
    return {
        entry["Id"]: {
            "name": entry.get("Name") or entry["Id"],
            "zone": entry.get("Zone"),
        }
        for entry in data
    }


def list_available_aws_regions() -> List[str]:
    response = run_aws_cli([
        "aws",
        "ec2",
        "describe-regions",
        "--all-regions",
        "--region",
        "us-east-1",
        "--output",
        "json",
    ])
    if response:
        try:
            data = json.loads(response)
        except json.JSONDecodeError:
            data = {}
        regions = [entry.get("RegionName") for entry in data.get("Regions", []) if entry.get("RegionName")]
        if regions:
            return sorted(set(regions))
    fallback = sorted(set(AWS_TO_GCP_REGION.keys()))
    if "us-east-1" not in fallback:
        fallback.insert(0, "us-east-1")
    return fallback


def list_ecs_clusters(region: str) -> List[str]:
    response = run_aws_cli([
        "aws",
        "ecs",
        "list-clusters",
        "--region",
        region,
        "--output",
        "json",
    ])
    if not response:
        return []
    try:
        data = json.loads(response)
    except json.JSONDecodeError:
        return []
    arns = data.get("clusterArns", [])
    clusters = []
    for arn in arns:
        if not isinstance(arn, str):
            continue
        clusters.append(arn.split("/")[-1] if "/" in arn else arn)
    return sorted(set(clusters))


def aggregate_service_resources(services: Sequence[EcsServiceDetail]) -> Tuple[float, int, float, int, int]:
    total_cpu_vcpu = sum(s.cpu_vcpu * s.desired_count for s in services)
    total_memory_mb = sum(s.memory_mb * s.desired_count for s in services)
    max_task_cpu_vcpu = max((s.cpu_vcpu for s in services), default=0.0)
    max_task_mem_mb = max((s.memory_mb for s in services), default=0)
    total_desired = sum(s.desired_count for s in services)
    return total_cpu_vcpu, total_memory_mb, max_task_cpu_vcpu, max_task_mem_mb, total_desired


def build_services_summary(services: Sequence[EcsServiceDetail], total_cpu_vcpu: float, total_memory_mb: int,
                           total_desired: int) -> str:
    if not services:
        return "No services discovered."
    header = (
        f"Total services: {len(services)}\n"
        f"Total desired tasks: {total_desired}\n"
        f"Approx aggregate demand: {total_cpu_vcpu:.2f} vCPU / {total_memory_mb} MB\n"
    )
    lines = [header, "Per-service details:"]
    for svc in services:
        lines.append("- " + svc.summary_line())
    return "\n".join(lines)


def resolve_gcp_location(aws_region: str, provided_location: Optional[str]) -> Tuple[Optional[str], Optional[str]]:
    provided = provided_location.strip() if provided_location else None
    mapped = AWS_TO_GCP_REGION.get(aws_region)
    if provided:
        if mapped and provided != mapped:
            return provided, (
                f"Provided GCP location '{provided}' differs from suggested mapping '{mapped}' for AWS region '{aws_region}'."
            )
        return provided, None
    if not mapped:
        return None, (
            "No matching GCP location found for AWS region "
            f"'{aws_region}'. Provide --gcp-location manually."
        )
    return mapped, f"Mapped AWS region '{aws_region}' to GCP location '{mapped}'."


def recommend_machine_type(total_cpu_vcpu: float, total_memory_mb: int,
                            max_task_cpu_vcpu: float, max_task_mem_mb: int) -> str:
    if total_cpu_vcpu <= 0 or total_memory_mb <= 0:
        return DEFAULT_MACHINE_TYPE
    desired_ratio = total_memory_mb / total_cpu_vcpu if total_cpu_vcpu else 0
    best_type: Optional[str] = None
    best_score = float("inf")
    for machine_type, (cpu_capacity, mem_capacity) in MACHINE_TYPE_CAPACITY.items():
        if cpu_capacity < max(max_task_cpu_vcpu, 0.25):
            continue
        if mem_capacity < max_task_mem_mb:
            continue
        if cpu_capacity <= 0:
            continue
        ratio = mem_capacity / cpu_capacity
        score = abs(ratio - desired_ratio)
        if score < best_score:
            best_score = score
            best_type = machine_type
    return best_type or DEFAULT_MACHINE_TYPE


def determine_machine_capacity(machine_type: str, node_cpu_override: Optional[float],
                               node_mem_override: Optional[int]) -> Tuple[float, int]:
    machine_type = machine_type or DEFAULT_MACHINE_TYPE
    default_cpu, default_mem = MACHINE_TYPE_CAPACITY.get(machine_type, MACHINE_TYPE_CAPACITY[DEFAULT_MACHINE_TYPE])
    cpu = float(node_cpu_override) if node_cpu_override else default_cpu
    mem = int(node_mem_override) if node_mem_override else default_mem
    return cpu, mem


def recommend_node_counts(total_cpu_vcpu: float, total_memory_mb: int,
                          node_cpu_vcpu: float, node_memory_mb: int,
                          min_nodes_arg: Optional[int], max_nodes_arg: Optional[int]) -> Tuple[int, int, int]:
    if node_cpu_vcpu <= 0 or node_memory_mb <= 0:
        return 1, max(1, min_nodes_arg or 1), max(1, max_nodes_arg or min_nodes_arg or 1)
    base_nodes_cpu = math.ceil(total_cpu_vcpu / node_cpu_vcpu) if total_cpu_vcpu else 1
    base_nodes_mem = math.ceil(total_memory_mb / node_memory_mb) if total_memory_mb else 1
    base_nodes = max(base_nodes_cpu, base_nodes_mem, 1)
    recommended = max(1, math.ceil(base_nodes * 1.25))
    min_nodes = max(min_nodes_arg if min_nodes_arg and min_nodes_arg > 0 else recommended, 1)
    max_nodes = max(max_nodes_arg if max_nodes_arg and max_nodes_arg >= min_nodes else int(math.ceil(recommended * 1.5)), min_nodes)
    return recommended, min_nodes, max_nodes


def adjust_counts_for_zones(initial: int, min_nodes: int, max_nodes: int, zone_count: int) -> Tuple[int, int, int, int, int, int]:
    if zone_count <= 1:
        return initial, min_nodes, max_nodes, initial, min_nodes, max_nodes
    per_zone_initial = max(1, math.ceil(initial / zone_count))
    per_zone_min = max(1, math.ceil(min_nodes / zone_count))
    per_zone_max = max(per_zone_min, math.ceil(max_nodes / zone_count))
    total_initial = initial
    total_min = min_nodes
    total_max = max_nodes
    return per_zone_initial, per_zone_min, per_zone_max, total_initial, total_min, total_max


def parse_node_locations(value: Optional[str]) -> List[str]:
    if not value:
        return []
    return [loc.strip() for loc in value.split(",") if loc.strip()]


def infer_node_locations_for_location(gcp_location: Optional[str]) -> List[str]:
    if not gcp_location:
        return []
    if gcp_location.count("-") >= 2:
        return [gcp_location]
    region_zones = GCP_REGION_ZONES.get(gcp_location)
    if region_zones:
        return list(region_zones)
    fallback_letters = ("a", "b", "c")
    return [f"{gcp_location}-{letter}" for letter in fallback_letters]


def extract_region_from_location(location: Optional[str]) -> Optional[str]:
    if not location:
        return None
    if location.count("-") < 2:
        return location
    return location.rsplit("-", 1)[0]


def describe_gcp_subnetwork(project: str, region: Optional[str], subnetwork: str) -> Optional[Dict[str, Any]]:
    if not project or not subnetwork:
        return None
    cmd = [
        "gcloud",
        "compute",
        "networks",
        "subnets",
        "describe",
        subnetwork,
        "--project",
        project,
        "--format",
        "json",
    ]
    if region:
        cmd.extend(["--region", region])
    try:
        result = subprocess.run(cmd, check=True, capture_output=True, text=True)
    except subprocess.CalledProcessError as exc:
        stderr = exc.stderr.strip() if exc.stderr else ""
        print(
            f"⚠️ Unable to describe subnet '{subnetwork}': exit code {exc.returncode}. "
            f"gcloud stderr: {stderr or 'no details'}"
        )
        return None
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError:
        print("⚠️ Could not parse gcloud subnet description output.")
        return None


def validate_gcp_subnetwork(project: Optional[str], location: Optional[str],
                            network: Optional[str], subnetwork: Optional[str]) -> Optional[str]:
    if not project or not subnetwork or not network:
        return subnetwork
    region = extract_region_from_location(location)
    data = describe_gcp_subnetwork(project, region, subnetwork)
    if not data:
        return subnetwork
    network_self_link = data.get("network", "")
    expected_suffix = f"/{network}"
    if expected_suffix not in network_self_link:
        print(
            f"⚠️ Subnet '{subnetwork}' belongs to network '{network_self_link}' which does not match "
            f"the requested network '{network}'."
        )
        return None
    return subnetwork


def finalize_subnetwork_override(args: argparse.Namespace, interactive: bool) -> None:
    network = getattr(args, "network", None)
    subnetwork = getattr(args, "subnetwork", None)
    if not network or not subnetwork:
        return

    validated = validate_gcp_subnetwork(
        getattr(args, "gcp_project", None),
        getattr(args, "gcp_location", None),
        network,
        subnetwork,
    )
    if validated:
        args.subnetwork = validated
        return

    if not interactive:
        print("⚠️ Subnetwork validation failed; removing the override to avoid deployment issues.")
        args.subnetwork = None
        return

    while True:
        print("Subnetwork validation failed. Provide an existing subnetwork or leave blank.")
        new_value = prompt_text("Subnetwork (blank for none)", default=subnetwork or "") or None
        if not new_value:
            args.subnetwork = None
            return
        validated = validate_gcp_subnetwork(
            getattr(args, "gcp_project", None),
            getattr(args, "gcp_location", None),
            network,
            new_value,
        )
        if validated:
            args.subnetwork = validated
            return
        subnetwork = new_value


def build_gke_plan(cluster_name: str, aws_region: str, args: argparse.Namespace,
                   services: Sequence[EcsServiceDetail], resolved_location: Optional[str],
                   location_note: Optional[str]) -> Tuple[Dict[str, Any], Optional[str]]:
    total_cpu_vcpu, total_memory_mb, max_task_cpu_vcpu, max_task_mem_mb, total_desired = aggregate_service_resources(services)
    zone_list = parse_node_locations(args.node_locations)
    zone_count = 1
    zone_source = "single zone"
    effective_location = resolved_location
    if effective_location and effective_location.count("-") < 2:
        zone_count = len(zone_list) if zone_list else 3
        zone_source = (
            f"user-provided zones ({', '.join(zone_list)})" if zone_list else "default regional distribution"
        )
    elif zone_list:
        zone_count = len(zone_list)
        zone_source = f"zonal cluster with explicit node-locations ({', '.join(zone_list)})"

    suggested_machine_type = recommend_machine_type(total_cpu_vcpu, total_memory_mb, max_task_cpu_vcpu, max_task_mem_mb)
    chosen_machine_type = args.machine_type or suggested_machine_type
    node_cpu_vcpu, node_memory_mb = determine_machine_capacity(chosen_machine_type, args.node_cpu, args.node_memory)
    recommended_nodes, min_nodes, max_nodes = recommend_node_counts(
        total_cpu_vcpu,
        total_memory_mb,
        node_cpu_vcpu,
        node_memory_mb,
        args.min_nodes,
        args.max_nodes,
    )
    per_zone_initial, per_zone_min, per_zone_max, total_initial, total_min, total_max = adjust_counts_for_zones(
        recommended_nodes,
        min_nodes,
        max_nodes,
        zone_count,
    )
    enable_private_nodes = bool(getattr(args, "private_nodes", False))
    enable_private_endpoint = bool(getattr(args, "private_endpoint", False)) if enable_private_nodes else False
    master_ipv4_cidr_block = (args.master_ipv4_cidr or DEFAULT_MASTER_IPV4_CIDR_BLOCK) if enable_private_nodes else None
    gke_cluster_name = args.gke_cluster_name or f"{cluster_name}-gke"
    plan = {
        "project": args.gcp_project,
        "location": effective_location,
        "cluster_name": gke_cluster_name,
        "release_channel": args.release_channel or DEFAULT_RELEASE_CHANNEL,
        "machine_type": chosen_machine_type,
        "node_cpu_vcpu": node_cpu_vcpu,
        "node_memory_mb": node_memory_mb,
        "initial_nodes": total_initial,
        "min_nodes": total_min,
        "max_nodes": total_max,
        "total_initial_nodes": total_initial,
        "total_min_nodes": total_min,
        "total_max_nodes": total_max,
        "per_zone_initial_nodes": per_zone_initial,
        "per_zone_min_nodes": per_zone_min,
        "per_zone_max_nodes": per_zone_max,
        "zone_count": zone_count,
        "zone_source": zone_source,
        "node_locations": zone_list,
        "network": args.network,
        "subnetwork": args.subnetwork,
        "node_pool_service_account": args.service_account or "",
        "services_total_desired": total_desired,
        "workloads_cpu_vcpu": total_cpu_vcpu,
        "workloads_memory_mb": total_memory_mb,
        "aws_region": aws_region,
        "location_note": location_note,
        "enable_private_nodes": enable_private_nodes,
        "enable_private_endpoint": enable_private_endpoint,
        "master_ipv4_cidr_block": master_ipv4_cidr_block if enable_private_nodes else None,
    }
    plan["node_pools"] = getattr(args, "node_pools", [])
    return plan, location_note


def build_plan_recommendations(plan: Dict[str, Any], services: Sequence[EcsServiceDetail]) -> List[str]:
    if not services:
        return []

    total_services = len(services)
    total_tasks = plan.get("services_total_desired") or 0
    total_cpu = plan.get("workloads_cpu_vcpu") or 0.0
    total_mem = plan.get("workloads_memory_mb") or 0
    node_cpu = plan.get("node_cpu_vcpu") or 0.0
    node_mem = plan.get("node_memory_mb") or 0
    initial_nodes = plan.get("total_initial_nodes") or 0
    min_nodes = plan.get("total_min_nodes") or 0
    max_nodes = plan.get("total_max_nodes") or 0
    zone_count = plan.get("zone_count") or 1

    def format_memory(value_mb: int) -> str:
        if value_mb >= 1024:
            return f"{value_mb / 1024:.1f} GiB"
        return f"{value_mb} MB"

    recommendations: List[str] = []
    recommendations.append(
        f"Plan covers {total_services} services (~{total_tasks} desired tasks); align workload scaling with the "
        f"autoscaler window of {min_nodes}-{max_nodes} nodes across {zone_count} zone"
        f"{'s' if zone_count != 1 else ''}."
    )

    if initial_nodes and node_cpu:
        capacity_cpu = node_cpu * initial_nodes
        utilization_cpu = total_cpu / capacity_cpu if capacity_cpu else 0.0
        recommendations.append(
            f"Initial capacity provides ≈{capacity_cpu:.1f} vCPU; projected load is {utilization_cpu:.0%}."
        )
        if utilization_cpu > 0.75:
            recommendations.append(
                "CPU headroom <25%; consider larger nodes or a higher max node count before cutover."
            )
        elif utilization_cpu < 0.35 and total_cpu:
            recommendations.append(
                "CPU headroom is generous; tighten min/max nodes once production metrics confirm usage."
            )

    if initial_nodes and node_mem:
        capacity_mem = node_mem * initial_nodes
        utilization_mem = total_mem / capacity_mem if capacity_mem else 0.0
        recommendations.append(
            f"Memory footprint ≈{format_memory(total_mem)} vs. {format_memory(capacity_mem)} provisioned ({utilization_mem:.0%} utilized)."
        )
        if utilization_mem > 0.75:
            recommendations.append(
                "Memory utilization is high; validate container limits/requests before migration."
            )
        elif utilization_mem < 0.35 and total_mem:
            recommendations.append(
                "Memory headroom allows room for bursty workloads; monitor actual usage to tune autoscaling."
            )

    max_task_cpu = max((svc.cpu_vcpu for svc in services), default=0.0)
    max_task_mem = max((svc.memory_mb for svc in services), default=0)
    if node_cpu and max_task_cpu > node_cpu:
        recommendations.append(
            f"Largest task ({max_task_cpu:.2f} vCPU) exceeds per-node CPU ({node_cpu:.2f}); use a bigger machine type or split the task."
        )
    if node_mem and max_task_mem > node_mem:
        recommendations.append(
            f"Largest task ({format_memory(max_task_mem)}) exceeds per-node memory ({format_memory(node_mem)}); adjust machine sizing."
        )

    cpu_sorted = sorted(
        (svc for svc in services if svc.cpu_vcpu > 0 and svc.desired_count > 0),
        key=lambda svc: svc.cpu_vcpu * svc.desired_count,
        reverse=True,
    )
    if cpu_sorted:
        top_cpu = cpu_sorted[:3]
        cpu_entries = [
            f"{svc.name} (~{svc.cpu_vcpu * svc.desired_count:.1f} vCPU)"
            for svc in top_cpu
        ]
        recommendations.append("CPU-heavy services: " + ", ".join(cpu_entries))

    mem_sorted = sorted(
        (svc for svc in services if svc.memory_mb > 0 and svc.desired_count > 0),
        key=lambda svc: svc.memory_mb * svc.desired_count,
        reverse=True,
    )
    if mem_sorted:
        top_mem = mem_sorted[:3]
        mem_entries = [
            f"{svc.name} (~{format_memory(svc.memory_mb * svc.desired_count)})"
            for svc in top_mem
        ]
        recommendations.append("Memory-heavy services: " + ", ".join(mem_entries))

    launch_types = {
        (svc.service.get("launchType") or ",".join(svc.task_definition.get("requiresCompatibilities", [])) or "UNKNOWN").upper()
        for svc in services
    }
    if launch_types - {"EC2"}:
        recommendations.append(
            "At least one service runs on Fargate/other launch modes; review networking (awsvpc) and daemon workloads for GKE parity."
        )

    return recommendations


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
        items: List[str] = []
        for item in value:
            rendered = format_hcl_value(item, indent + 2)
            items.append(" " * (indent + 2) + f"{rendered},")
        return "[\n" + "\n".join(items) + "\n" + spacer + "]"
    if isinstance(value, dict):
        if not value:
            return "{}"
        lines = []
        for key in sorted(value.keys()):
            key_repr = key if re.match(r"^[A-Za-z_][A-Za-z0-9_]*$", key) else json.dumps(key)
            val_repr = format_hcl_value(value[key], indent + 2)
            lines.append(" " * (indent + 2) + f"{key_repr} = {val_repr}")
        return "{\n" + "\n".join(lines) + "\n" + spacer + "}"
    raise TypeError(f"Unsupported value type for HCL formatting: {type(value)!r}")


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
    if main_body and "google_container_cluster" not in main_body:
        errors.append("main.tf missing google_container_cluster resource.")
    if main_body and "google_container_node_pool" not in main_body:
        errors.append("main.tf missing google_container_node_pool resource.")

    if variables_body and "variable \"project_id\"" not in variables_body:
        errors.append("variables.tf missing variable 'project_id'.")
    if variables_body and "variable \"cluster_name\"" not in variables_body:
        errors.append("variables.tf missing variable 'cluster_name'.")
    if variables_body and "variable \"node_pool\"" not in variables_body:
        errors.append("variables.tf missing variable 'node_pool'.")

    if tfvars_body and "project_id" not in tfvars_body:
        errors.append("terraform.tfvars missing project_id assignment.")
    if tfvars_body and "cluster_name" not in tfvars_body:
        errors.append("terraform.tfvars missing cluster_name assignment.")

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
            fh.write(content if content.endswith("\n") else content + "\n")


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


def deterministic_terraform_bundle(plan: Dict[str, Any], cluster_name: str) -> Dict[str, str]:
    node_pool_defaults = {
        "machine_type": plan.get("machine_type", DEFAULT_MACHINE_TYPE),
        "initial_node_count": plan.get("initial_nodes", 1),
        "min_node_count": plan.get("min_nodes", 1),
        "max_node_count": plan.get("max_nodes", max(plan.get("min_nodes", 1), plan.get("initial_nodes", 1))),
        "disk_size_gb": 100,
        "disk_type": "pd-standard",
        "oauth_scopes": ["https://www.googleapis.com/auth/cloud-platform"],
        "labels": {
            "source": "ecs-migration",
            "ecs_cluster": sanitize_label_value(cluster_name),
        },
        "tags": ["ecs-migration"],
        "service_account": plan.get("node_pool_service_account", ""),
    }
    node_pool_hcl = format_hcl_value(node_pool_defaults, indent=2)
    node_locations = plan.get("node_locations") or []
    node_pools = plan.get("node_pools") or [{"name": "primary", "gcp_subnet": plan.get("subnetwork")}]
    node_pools_hcl: List[str] = []
    for np in node_pools:
        np_name = sanitize_label_value(np.get("name") or "primary")
        subnetwork = np.get("gcp_subnet") or plan.get("subnetwork")
        label_value = sanitize_label_value(subnetwork or np.get("name") or np_name)
        node_locations_override = [loc for loc in np.get("node_locations", []) if loc]
        node_pool_lines = [
            f'resource "google_container_node_pool" "{np_name}" {{',
            f'  name     = "{np_name}"',
            "  location = var.location",
            "  cluster  = google_container_cluster.primary.name",
            "",
            "  initial_node_count = var.node_pool.initial_node_count",
            "",
            "  autoscaling {",
            "    min_node_count = var.node_pool.min_node_count",
            "    max_node_count = var.node_pool.max_node_count",
            "  }",
            "",
            "  management {",
            "    auto_repair  = true",
            "    auto_upgrade = true",
            "  }",
        ]
        if node_locations_override:
            zones_literal = "[" + ", ".join(json.dumps(zone) for zone in node_locations_override) + "]"
            node_pool_lines.extend([
                "",
                f"  node_locations = {zones_literal}",
            ])
        node_pool_lines.extend([
            "",
            "  node_config {",
            "    machine_type = var.node_pool.machine_type",
            "    disk_size_gb = var.node_pool.disk_size_gb",
            "    disk_type    = var.node_pool.disk_type",
            "    oauth_scopes = var.node_pool.oauth_scopes",
            f"    labels = merge(var.node_pool.labels, {{ subnet = \"{label_value}\" }})",
            "    tags   = var.node_pool.tags",
            "    service_account = var.node_pool.service_account != \"\" ? var.node_pool.service_account : null",
            "  }",
        ])
        node_pool_lines.append("}")
        node_pools_hcl.append("\n".join(node_pool_lines))
    tfvars = [
        f"project_id = {format_hcl_value(plan.get('project'))}",
        f"cluster_name = {format_hcl_value(plan.get('cluster_name'))}",
        f"location = {format_hcl_value(plan.get('location'))}",
        f"release_channel = {format_hcl_value(plan.get('release_channel', DEFAULT_RELEASE_CHANNEL))}",
        f"network = {format_hcl_value(plan.get('network'))}",
        f"subnetwork = {format_hcl_value(plan.get('subnetwork'))}",
        f"node_locations = {format_hcl_value(node_locations)}",
        f"node_pool = {node_pool_hcl}",
    ]
    tfvars_body = "\n".join(tfvars) + "\n"
    variables_body = """variable "project_id" {
  type = string
}

variable "cluster_name" {
  type = string
}

variable "location" {
  type = string
}

variable "release_channel" {
  type    = string
  default = "REGULAR"
}

variable "network" {
  type    = string
  default = null
}

variable "subnetwork" {
  type    = string
  default = null
}

variable "node_locations" {
  type    = list(string)
  default = []
}

variable "node_pool" {
  type = object({
    machine_type       = string
    initial_node_count = number
    min_node_count     = number
    max_node_count     = number
    disk_size_gb       = number
    disk_type          = string
    oauth_scopes       = list(string)
    labels             = map(string)
    tags               = list(string)
    service_account    = string
  })
}
"""
    private_cluster_block = ""
    if plan.get("enable_private_nodes"):
        master_cidr = plan.get("master_ipv4_cidr_block") or DEFAULT_MASTER_IPV4_CIDR_BLOCK
        private_endpoint = "true" if plan.get("enable_private_endpoint") else "false"
        private_cluster_block = (
            "\n  private_cluster_config {\n"
            "    enable_private_nodes    = true\n"
            f"    enable_private_endpoint = {private_endpoint}\n"
            f"    master_ipv4_cidr_block  = \"{master_cidr}\"\n"
            "  }\n"
        )

    main_body = f"""terraform {{
  required_version = ">= 1.5.0"
  required_providers {{
    google = {{
      source  = "hashicorp/google"
      version = ">= 5.0.0"
    }}
  }}
}}

provider "google" {{
  project = var.project_id
  region  = var.location
}}

resource "google_container_cluster" "primary" {{
  name     = var.cluster_name
  location = var.location

  remove_default_node_pool = true
  deletion_protection      = false
  initial_node_count       = 1

  release_channel {{
    channel = upper(var.release_channel)
  }}

  network    = var.network
  subnetwork = var.subnetwork

  logging_config {{
    enable_components = []
  }}

  monitoring_config {{
    enable_components = []
    managed_prometheus {{
      enabled = false
    }}
  }}

  node_locations = var.node_locations{private_cluster_block}

  networking_mode = "VPC_NATIVE"
}}

"""
    if node_pools_hcl:
        main_body += "\n\n" + "\n\n".join(node_pools_hcl) + "\n"
    return {
        "main.tf": main_body,
        "variables.tf": variables_body,
        "terraform.tfvars": tfvars_body,
    }


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
        if not ans and default is not None:
            return default
        if ans:
            return ans
        print("Please provide a value.")


def prompt_select(prompt: str, options: List[str]) -> int:
    if not options:
        raise ValueError("prompt_select requires at least one option")
    while True:
        print(f"\n{prompt}")
        for idx, option in enumerate(options, 1):
            print(f"  {idx}. {option}")
        try:
            ans = input("Choose an option: ").strip()
        except EOFError:
            ans = ""
        if not ans:
            continue
        if not ans.isdigit():
            print("Enter the number corresponding to your choice.")
            continue
        idx = int(ans)
        if 1 <= idx <= len(options):
            return idx - 1
        print("Choice out of range.")


def prompt_ecs_subnets_and_create_nodepools(
    region: str,
    services: Sequence[EcsServiceDetail],
    gcp_location: Optional[str],
    existing_node_locations: Optional[Sequence[str]] = None,
    default_gcp_subnetwork: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """Detect ECS subnets and build a single node pool for GKE."""
    subnet_set = set()
    for svc in services:
        conf = (
            svc.service.get("networkConfiguration", {})
            .get("awsvpcConfiguration", {})
        )
        for subnet in conf.get("subnets", []):
            subnet_set.add(subnet)

    if not subnet_set:
        print("⚠️ No ECS subnets detected in services.")
        return []

    subnet_map = list_ecs_subnets_with_names(region, subnet_set)
    ordered_subnets = [(subnet_id, subnet_map[subnet_id]) for subnet_id in sorted(subnet_map.keys())]

    print("\n🕸️ ECS subnets detected across all services:")
    for idx, (subnet_id, metadata) in enumerate(ordered_subnets, 1):
        name = metadata.get("name", subnet_id)
        aws_zone = metadata.get("zone") or "unknown-zone"
        print(f"  {idx}. {subnet_id} → {name} (AWS zone: {aws_zone})")

    print(f"\nTotal unique ECS subnets: {len(subnet_map)}")
    print("Select which subnet should back the single GKE node pool.\n")

    options = [
        f"{subnet_id} → {metadata.get('name', subnet_id)} (AWS zone: {metadata.get('zone') or 'unknown'})"
        for subnet_id, metadata in ordered_subnets
    ]
    options.append("Enter subnet manually")
    choice = prompt_select("Choose subnet for the primary node pool", options)

    manual_entry = choice == len(options) - 1
    selected_subnet_id: Optional[str]
    default_gcp_subnetwork = default_gcp_subnetwork or None
    if manual_entry:
        selected_subnet_id = prompt_text("AWS subnet ID to mirror (blank if unavailable)", default="").strip() or None
        default_subnet_name = default_gcp_subnetwork or selected_subnet_id or "primary-subnet"
    else:
        selected_subnet_id, metadata = ordered_subnets[choice]
        default_subnet_name = default_gcp_subnetwork or metadata.get("name") or selected_subnet_id

    pool_name_default = sanitize_label_value(default_gcp_subnetwork or default_subnet_name or "primary")
    pool_name = prompt_text(
        "Node pool name (GKE resource name)", default=pool_name_default
    )
    subnet_prompt = (
        "GCP subnet name for the node pool "
        "(must already exist in the selected network; leave blank to skip)"
    )
    if default_subnet_name:
        subnet_prompt += f" [suggested: {default_subnet_name}]"
    gcp_subnet_input = prompt_text(subnet_prompt, default="").strip()
    gcp_subnet = gcp_subnet_input or None
    if not gcp_subnet and default_subnet_name:
        print("ℹ️ No subnet specified. Terraform configuration will rely on the cluster defaults.")

    normalized_existing = [loc for loc in (existing_node_locations or []) if loc]
    candidate_zones = normalized_existing or infer_node_locations_for_location(gcp_location)
    selected_zone: Optional[str] = None
    if candidate_zones:
        zone_options = candidate_zones + ["Enter zone manually"]
        choice_idx = prompt_select("Select GCP zone for the node pool", zone_options)
        if choice_idx == len(candidate_zones):
            selected_zone = prompt_text("GCP zone (e.g. asia-south1-a)").strip() or None
        else:
            selected_zone = candidate_zones[choice_idx]
    else:
        print("⚠️ Unable to infer GCP zones; please provide one manually.")
        selected_zone = prompt_text("GCP zone (e.g. asia-south1-a)").strip() or None

    if selected_zone:
        print(f"🌐 Node pool will target GCP zone: {selected_zone}")
    else:
        print("⚠️ No zone selected; node pool will not override zones.")
    node_locations = [selected_zone] if selected_zone else []

    return [{
        "aws_subnet_id": selected_subnet_id,
        "gcp_subnet": gcp_subnet,
        "name": sanitize_label_value(pool_name or "primary"),
        "node_locations": node_locations,
    }]


def build_interactive_cli_args() -> List[str]:
    print("\n=== Terraform ECS → GKE Toolkit ===")
    print("No arguments supplied; entering interactive mode. Press Ctrl+C to abort.\n")
    modes = [
        ("generate-terraform", "Generate Terraform bundle for a new GKE cluster"),
        ("exit", "Exit the toolkit"),
    ]
    mode_choice = prompt_select("Select operation", [f"{title} — {desc}" for title, desc in modes])
    mode = modes[mode_choice][0]
    if mode == "exit":
        print("\n👋 Exiting without action.")
        sys.exit(0)
    args: List[str] = [mode]
    region_options = list_available_aws_regions()
    if region_options:
        region_prompt_options = region_options + ["Enter region manually"]
        selected_idx = prompt_select("Select AWS region", region_prompt_options)
        if selected_idx == len(region_options):
            region = prompt_text("AWS region", default="us-east-1")
        else:
            region = region_options[selected_idx]
    else:
        region = prompt_text("AWS region", default="us-east-1")

    clusters = list_ecs_clusters(region)
    if clusters:
        cluster_prompt = clusters + ["Enter cluster manually"]
        cluster_idx = prompt_select(f"Select ECS cluster in {region}", cluster_prompt)
        if cluster_idx == len(clusters):
            cluster = prompt_text("ECS cluster name")
        else:
            cluster = clusters[cluster_idx]
    else:
        print(f"⚠️ No ECS clusters discovered in {region} (or insufficient permissions).")
        cluster = prompt_text("ECS cluster name")

    args.extend(["--region", region, "--cluster", cluster])
    if mode == "generate-terraform":
        args.append("--interactive")
    return args


def display_plan(cluster_name: str, plan: Dict[str, Any], services: Sequence[EcsServiceDetail],
                 summary: str, recommendations: Optional[Sequence[str]]) -> None:
    print("\n📋 ECS cluster summary:\n" + summary)
    if recommendations:
        print("\n🔍 Recommendations:")
        for line in recommendations:
            print(f"- {line}")
    location_note = plan.get("location_note")
    if location_note:
        print("\nℹ️ " + location_note)
    print(
        "\n📊 Estimated ECS footprint: "
        f"~{plan['workloads_cpu_vcpu']:.2f} vCPU / {plan['workloads_memory_mb']} MB memory"
    )
    print(f"🧾 Machine type: {plan['machine_type']}")
    print(
        "🧮 Node capacity ≈ "
        f"{plan['node_cpu_vcpu']:.1f} vCPU / {plan['node_memory_mb']} MB per node"
    )
    total_initial = plan.get("total_initial_nodes") or 0
    total_min = plan.get("total_min_nodes") or 0
    total_max = plan.get("total_max_nodes") or 0
    per_zone_initial = plan.get("per_zone_initial_nodes")
    per_zone_min = plan.get("per_zone_min_nodes")
    per_zone_max = plan.get("per_zone_max_nodes")
    zone_count = plan.get("zone_count") or 1
    zone_phrase = f"{zone_count} zone{'s' if zone_count != 1 else ''}"
    if per_zone_initial or per_zone_min or per_zone_max:
        per_zone_desc = (
            f" (per-zone ≈{per_zone_initial or '?'} / {per_zone_min or '?'}-{per_zone_max or '?'} across "
            f"{zone_phrase}; {plan.get('zone_source')})"
        )
    else:
        per_zone_desc = f" ({zone_phrase}; {plan.get('zone_source')})"
    print(
        f"📌 Node counts → total initial: {total_initial}, min: {total_min}, max: {total_max}{per_zone_desc}"
    )
    if zone_count > 1:
        def rounded(total: Optional[int], per_zone: Optional[int]) -> bool:
            if total is None or per_zone is None:
                return False
            return per_zone * zone_count != total

        if any(rounded(val, per_zone) for val, per_zone in [
            (total_initial, per_zone_initial),
            (total_min, per_zone_min),
            (total_max, per_zone_max),
        ]):
            print(
                "⚠️ Totals are not evenly divisible across zones; Terraform values reflect totals, while gcloud "
                "commands will round per-zone counts up as needed."
            )
    if plan.get("node_locations"):
        print(f"🌐 Node locations: {', '.join(plan['node_locations'])}")
    if plan.get("network"):
        print(f"🕸️ Network override: {plan['network']}")
    if plan.get("subnetwork"):
        print(f"🕳️ Subnetwork override: {plan['subnetwork']}")
    if plan.get("enable_private_nodes"):
        endpoint_desc = "private endpoint only" if plan.get("enable_private_endpoint") else "public endpoint exposed"
        cidr_block = plan.get("master_ipv4_cidr_block")
        cidr_note = f"; control plane CIDR {cidr_block}" if cidr_block else ""
        print(f"🔒 Private nodes enabled ({endpoint_desc}{cidr_note}).")
    else:
        print("🌐 Private nodes disabled (nodes receive public IPs).")


def ensure_generate_args(args: argparse.Namespace) -> None:
    if not args.cluster:
        args.cluster = prompt_text("ECS cluster name")
    if not args.region:
        args.region = prompt_text("AWS region", default="us-east-1")
    if not args.gcp_project:
        args.gcp_project = prompt_text("GCP project ID")
    fallback_location = AWS_TO_GCP_REGION.get(args.region, "us-central1")
    if not args.gcp_location:
        args.gcp_location = prompt_text("GKE location", default=fallback_location)
    if not args.gke_cluster_name:
        args.gke_cluster_name = prompt_text("GKE cluster name", default=f"{args.cluster}-gke")
    if args.network is None:
        args.network = prompt_text("VPC network (blank for default)", default="") or None
    if isinstance(getattr(args, "subnetwork", None), str) and not args.subnetwork.strip():
        args.subnetwork = None
    if isinstance(getattr(args, "node_locations", None), str) and not args.node_locations.strip():
        args.node_locations = None
    if args.service_account is None:
        args.service_account = prompt_text("Node pool service account (blank for default)", default="") or ""
    else:
        args.service_account = args.service_account or ""
    if args.network and args.subnetwork:
        validated = validate_gcp_subnetwork(args.gcp_project, args.gcp_location, args.network, args.subnetwork)
        if not validated:
            print("⚠️ Provided subnetwork could not be validated against the selected network; proceeding but you may be re-prompted later.")
        else:
            args.subnetwork = validated
    if args.private_endpoint:
        args.private_nodes = True
    if args.private_nodes:
        args.master_ipv4_cidr = args.master_ipv4_cidr or DEFAULT_MASTER_IPV4_CIDR_BLOCK
    else:
        args.master_ipv4_cidr = None


def run_generate_terraform_mode(args: argparse.Namespace) -> None:
    if args.interactive:
        ensure_generate_args(args)
    cluster_details, services = collect_ecs_services(args.cluster, args.region)
    if not cluster_details:
        print("⚠️ Could not retrieve ECS cluster metadata; aborting Terraform generation.")
        return
    if not services:
        print("⚠️ Unable to retrieve any ECS services; nothing to model.")
        return
    if getattr(args, "private_endpoint", False):
        args.private_nodes = True
    if getattr(args, "private_nodes", True):
        if not getattr(args, "master_ipv4_cidr", None):
            args.master_ipv4_cidr = DEFAULT_MASTER_IPV4_CIDR_BLOCK
    else:
        args.master_ipv4_cidr = None
    existing_node_locations = parse_node_locations(getattr(args, "node_locations", None))
    location_hint = args.gcp_location or AWS_TO_GCP_REGION.get(args.region)
    node_pools = prompt_ecs_subnets_and_create_nodepools(
        args.region,
        services,
        location_hint,
        existing_node_locations,
        args.subnetwork,
    )
    if node_pools:
        args.node_pools = node_pools
        first_pool_subnet = node_pools[0].get("gcp_subnet")
        if first_pool_subnet:
            if not getattr(args, "subnetwork", None):
                args.subnetwork = first_pool_subnet
        if not existing_node_locations:
            derived_node_locations: List[str] = []
            for pool in node_pools:
                for loc in pool.get("node_locations", []):
                    if loc and loc not in derived_node_locations:
                        derived_node_locations.append(loc)
            if derived_node_locations:
                args.node_locations = ",".join(derived_node_locations)
                existing_node_locations = derived_node_locations
    finalize_subnetwork_override(args, getattr(args, "interactive", False) or sys.stdin.isatty())
    total_cpu_vcpu, total_memory_mb, _, _, total_desired = aggregate_service_resources(services)
    summary = build_services_summary(services, total_cpu_vcpu, total_memory_mb, total_desired)
    resolved_location, location_note = resolve_gcp_location(args.region, args.gcp_location)
    plan, _ = build_gke_plan(args.cluster, args.region, args, services, resolved_location, location_note)
    recommendations = build_plan_recommendations(plan, services)
    display_plan(args.cluster, plan, services, summary, recommendations)

    folder_name = sanitize_name(plan.get("cluster_name") or args.cluster)
    output_dir = os.path.join(args.output_root, folder_name)
    if os.path.isdir(output_dir) and not args.overwrite:
        raise SystemExit(f"Output directory already exists: {output_dir} (use --overwrite to replace)")

    bundle = deterministic_terraform_bundle(plan, args.cluster)
    try:
        structure_messages = validate_terraform_bundle_structure(bundle)
    except RuntimeError as exc:
        raise SystemExit(str(exc))
    write_terraform_files(bundle, output_dir, overwrite=True)

    validation_messages: List[str] = []
    if not args.skip_terraform_validate:
        try:
            validation_messages = terraform_cli_validate(output_dir)
        except RuntimeError as exc:
            print(f"⚠️ Terraform validation failed: {exc}")

    print(f"\n🗂️ Terraform configuration written to {output_dir}")
    for msg in structure_messages:
        print(f"✅ {msg}")
    if validation_messages:
        for msg in validation_messages:
            print(f"✅ {msg}")
    print("\n✅ Terraform bundle ready.")


def main() -> None:
    parser = argparse.ArgumentParser(description="ECS → GKE Terraform toolkit (inspired by terraform-vpc.py). Safe dry-run by default.")
    parser.add_argument("--gcp-credential-file", "--credential-file-override", dest="gcp_credential_file",
                        help="Path to a Google Cloud service-account JSON for downstream commands.")
    sub = parser.add_subparsers(dest="mode", required=True)


    tf_parser = sub.add_parser("generate-terraform", help="Generate Terraform configuration for a GKE cluster sized for ECS workloads.")
    tf_parser.add_argument("--cluster")
    tf_parser.add_argument("--region")
    tf_parser.add_argument("--gcp-project")
    tf_parser.add_argument("--gcp-location")
    tf_parser.add_argument("--gke-cluster-name")
    tf_parser.add_argument("--machine-type")
    tf_parser.add_argument("--node-cpu", type=float)
    tf_parser.add_argument("--node-memory", type=int)
    tf_parser.add_argument("--min-nodes", type=int)
    tf_parser.add_argument("--max-nodes", type=int)
    tf_parser.add_argument("--network")
    tf_parser.add_argument("--subnetwork")
    tf_parser.add_argument("--node-locations")
    tf_parser.add_argument("--service-account")
    tf_parser.add_argument("--release-channel", default=DEFAULT_RELEASE_CHANNEL)
    tf_parser.add_argument("--private-nodes", dest="private_nodes", action="store_true", default=True,
                           help="Configure GKE nodes with private IPs (requires VPC-native networking).")
    tf_parser.add_argument("--no-private-nodes", dest="private_nodes", action="store_false",
                           help="Allow GKE nodes to use external IPs.")
    tf_parser.add_argument("--private-endpoint", action="store_true",
                           help="Restrict the GKE control plane to a private endpoint (implies --private-nodes).")
    tf_parser.add_argument("--master-ipv4-cidr",
                           help=f"/28 CIDR block for the GKE control plane when private nodes are enabled (default {DEFAULT_MASTER_IPV4_CIDR_BLOCK}).")
    tf_parser.add_argument("--output-root", default="terraform")
    tf_parser.add_argument("--overwrite", action="store_true")
    tf_parser.add_argument("--interactive", action="store_true")
    tf_parser.add_argument("--skip-terraform-validate", action="store_true",
                           help="Skip running terraform init/validate after writing files.")

    if len(sys.argv) == 1:
        try:
            interactive_args = build_interactive_cli_args()
        except KeyboardInterrupt:
            print("\n✋ Aborted before selecting a mode.")
            return
        args = parser.parse_args(interactive_args)
    else:
        args = parser.parse_args()

    configure_gcp_credentials(getattr(args, "gcp_credential_file", None))

    if args.mode == "generate-terraform":
        run_generate_terraform_mode(args)
    else:
        parser.error(f"Unknown mode: {args.mode}")


if __name__ == "__main__":
    main()
