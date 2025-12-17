#!/usr/bin/env python3
"""
Export Kubernetes manifests from an EKS cluster using read-only AWS/kubectl commands
and write GKE-ready YAML without using Gemini. The script performs deterministic
cleanup (drops AWS-only annotations, remaps storage classes, and carries forward
ConfigMaps) but never mutates the source cluster.
"""

import argparse
import configparser
import copy
import getpass
import json
import os
import re
import subprocess
import sys
from pathlib import Path

import yaml

DEFAULT_RESOURCE_TYPES = [
    "deployments.apps",
    "statefulsets.apps",
    "daemonsets.apps",
    "jobs.batch",
    "cronjobs.batch",
    "services",
    "ingresses.networking.k8s.io",
    "configmaps",
    "secrets",
    "persistentvolumeclaims",
    "horizontalpodautoscalers.autoscaling",
]

SYSTEM_NAMESPACES = {
    "kube-node-lease",
    "kube-public",
    "kube-system",
    "aws-observability",
    "amazon-cloudwatch",
}

SAFE_KUBECTL_VERBS = {"get", "api-resources"}
STRIP_ANNOTATION_PREFIXES = (
    "service.beta.kubernetes.io/aws-",
    "alb.ingress.kubernetes.io/",
    "eks.amazonaws.com/",
    "meta.helm.sh/",
)
AWS_STORAGE_CLASSES = {"gp2", "gp3", "ebs", "ebs-sc", "aws-efs", "efs-sc", "efs"}
IGNORED_KINDS = {
    "PersistentVolume",
    "ReplicaSet",
    "Pod",
    "Endpoint",
    "Endpoints",
    "EndpointSlice",
    "Event",
    "ControllerRevision",
}
UNSUPPORTED_KINDS = {
    "TargetGroupBinding",
    "PodMetrics",
    "NodeMetrics",
}
UNSUPPORTED_API_PREFIXES = (
    "metrics.k8s.io/",
    "elbv2.k8s.aws/",
)
SENSITIVE_SECRET_TYPES = {
    "kubernetes.io/service-account-token",
    "helm.sh/release.v1",
}
SENSITIVE_SECRET_NAME_PATTERNS = (
    "sh.helm.release",
    "aws-creds",
    "aws-credentials",
)
SENSITIVE_SECRET_KEYS = {
    "aws_access_key_id",
    "aws_secret_access_key",
    "aws_session_token",
}


def _prompt_for_value(current, prompt_text, allow_empty=False):
    if current:
        return current
    if not sys.stdin.isatty():
        if allow_empty:
            return ""
        raise RuntimeError(f"Missing required input: {prompt_text.rstrip(': ')}")
    while True:
        response = input(prompt_text).strip()
        if response or allow_empty:
            return response
        print("Input cannot be empty. Please try again.")


def _prompt_for_secret(prompt_text):
    if not sys.stdin.isatty():
        raise RuntimeError(f"{prompt_text.rstrip(': ')} is required but cannot prompt in non-interactive mode.")
    while True:
        value = getpass.getpass(prompt_text).strip()
        if value:
            return value
        print("Input cannot be empty. Please try again.")


def _to_k8s_name(*parts, default="default"):
    text = "-".join(part for part in (part or "" for part in parts) if part)
    text = text.lower()
    text = "".join(char if char.isalnum() or char == "-" else "-" for char in text)
    text = "-".join(segment for segment in text.split("-") if segment)
    if not text:
        text = default
    if len(text) > 63:
        text = text[:63].rstrip("-")
    return text or default


def _prompt_for_choices(candidates, label):
    if not candidates:
        return []
    if not sys.stdin.isatty():
        return candidates

    while True:
        print(f"Available {label}:")
        for idx, entry in enumerate(candidates, start=1):
            print(f"  {idx}) {entry}")
        print("Enter comma-separated numbers or names, or press Enter for all.")
        selection = input(f"{label.title()} selection: ").strip()
        if not selection or selection.lower() == "all":
            return candidates
        tokens = [token.strip() for token in selection.split(",") if token.strip()]
        if not tokens:
            print("No valid selection detected. Try again.")
            continue
        chosen = []
        for token in tokens:
            if token.isdigit():
                idx = int(token)
                if 1 <= idx <= len(candidates):
                    chosen.append(candidates[idx - 1])
                else:
                    print(f"Selection '{token}' is out of range.")
                    break
            else:
                if token in candidates:
                    chosen.append(token)
                else:
                    print(f"Value '{token}' not found in list.")
                    break
        else:
            seen = set()
            unique = []
            for entry in chosen:
                if entry not in seen:
                    seen.add(entry)
                    unique.append(entry)
            if unique:
                return unique
        print("Please try again.")


def _list_aws_regions():
    cmd = [
        "aws",
        "ec2",
        "describe-regions",
        "--output",
        "json",
    ]
    try:
        raw = subprocess.check_output(cmd, text=True)
    except FileNotFoundError as exc:
        raise RuntimeError("AWS CLI not found in PATH.") from exc
    except subprocess.CalledProcessError as exc:
        raise RuntimeError(f"Failed to describe AWS regions: {exc}") from exc
    payload = json.loads(raw) if raw else {}
    regions = []
    for entry in payload.get("Regions", []):
        name = entry.get("RegionName")
        if name:
            regions.append(name)
    regions.sort()
    return regions


def _prompt_for_region(initial):
    if initial:
        return initial
    if not sys.stdin.isatty():
        raise RuntimeError("AWS region required when running non-interactively.")

    while True:
        regions = _list_aws_regions()
        if regions:
            print("Available AWS regions:")
            for idx, name in enumerate(regions, start=1):
                print(f"  {idx}) {name}")
            selection = input("Select region by number or enter name: ").strip()
            if not selection:
                continue
            if selection.isdigit():
                idx = int(selection)
                if 1 <= idx <= len(regions):
                    return regions[idx - 1]
                print("Invalid selection number.")
            elif selection:
                if selection in regions:
                    return selection
                print(f"Region '{selection}' not in list, but using custom value.")
                return selection
        else:
            manual = input("Enter AWS region: ").strip()
            if manual:
                return manual
        print("Please try again.")


def _list_eks_clusters(region):
    clusters = []
    next_token = None
    while True:
        cmd = [
            "aws",
            "eks",
            "list-clusters",
            "--region",
            region,
        ]
        if next_token:
            cmd.extend(["--next-token", next_token])
        try:
            raw = subprocess.check_output(cmd, text=True)
        except FileNotFoundError as exc:
            raise RuntimeError("AWS CLI not found in PATH.") from exc
        except subprocess.CalledProcessError as exc:
            raise RuntimeError(f"Failed to list EKS clusters in region {region}: {exc}") from exc
        payload = json.loads(raw) if raw else {}
        clusters.extend(payload.get("clusters", []))
        next_token = payload.get("nextToken")
        if not next_token:
            break
    return clusters


def _prompt_for_cluster(initial, region):
    if initial:
        return initial
    if not sys.stdin.isatty():
        raise RuntimeError("Cluster name required when running non-interactively.")

    while True:
        clusters = _list_eks_clusters(region)
        if clusters:
            print(f"EKS clusters in {region}:")
            for idx, name in enumerate(clusters, start=1):
                print(f"  {idx}) {name}")
            selection = input("Select cluster by number or enter name: ").strip()
            if not selection:
                continue
            if selection.isdigit():
                idx = int(selection)
                if 1 <= idx <= len(clusters):
                    return clusters[idx - 1]
                print("Invalid selection number.")
            elif selection:
                return selection
        else:
            print(f"No EKS clusters detected in region {region}.")
            manual = input("Enter cluster name manually: ").strip()
            if manual:
                return manual
        print("Please try again.")


def _update_kubeconfig(cluster, region, alias):
    cmd = [
        "aws",
        "eks",
        "update-kubeconfig",
        "--name",
        cluster,
        "--region",
        region,
    ]
    if alias:
        cmd.extend(["--alias", alias])
    try:
        subprocess.check_call(cmd)
    except FileNotFoundError as exc:
        raise RuntimeError("AWS CLI not found in PATH.") from exc
    except subprocess.CalledProcessError as exc:
        raise RuntimeError(f"aws eks update-kubeconfig failed: {exc}") from exc


class KubectlAuthError(RuntimeError):
    """Raised when kubectl needs a refreshed AWS token/credentials."""


def _run_kubectl(args, description):
    if not args:
        raise RuntimeError("kubectl command requires arguments.")
    verb = args[0].lower()
    if verb not in SAFE_KUBECTL_VERBS:
        raise RuntimeError(
            f"kubectl command '{verb}' is not allowed. This script is read-only and only permits: {', '.join(sorted(SAFE_KUBECTL_VERBS))}."
        )
    cmd = ["kubectl", *args]
    try:
        result = subprocess.run(cmd, capture_output=True, check=False)
    except FileNotFoundError as exc:
        raise RuntimeError("kubectl not found in PATH.") from exc
    stdout = (result.stdout or b"").decode("utf-8", errors="replace")
    stderr = (result.stderr or b"").decode("utf-8", errors="replace")
    if result.returncode == 0:
        return stdout
    detail = stderr or stdout or ""
    auth_markers = (
        "You must be logged in to the server",
        "the server has asked for the client to provide credentials",
        "Unauthorized",
    )
    if any(marker in detail for marker in auth_markers):
        raise KubectlAuthError(
            f"kubectl {description} failed because the cluster requested credentials. "
            "Ensure your AWS access key/secret (or AWS_PROFILE) is configured and rerun the script."
        )
    raise RuntimeError(f"kubectl {description} failed: {detail or 'unknown error'}")


def _list_namespaces(include_system, cluster_name=None):
    description = "get namespaces"
    if cluster_name:
        description += f" from cluster {cluster_name}"
    raw = _run_kubectl(["get", "ns", "-o", "json"], description)
    payload = json.loads(raw) if raw else {}
    names = []
    for item in payload.get("items", []):
        name = item.get("metadata", {}).get("name")
        if not name:
            continue
        if not include_system and name in SYSTEM_NAMESPACES:
            continue
        names.append(name)
    return names


def _fetch_namespace_objects(namespace, resources):
    objects = []
    for resource in resources:
        try:
            raw = _run_kubectl(
                [
                    "get",
                    resource,
                    "-n",
                    namespace,
                    "-o",
                    "json",
                    "--ignore-not-found",
                ],
                f"get {resource} in namespace {namespace}",
            )
        except KubectlAuthError:
            raise
        except RuntimeError as exc:
            print(f"[WARN] {exc}")
            continue
        if not raw.strip():
            continue
        payload = json.loads(raw)
        items = payload.get("items")
        if items is None and payload:
            items = [payload]
        for item in items or []:
            if not isinstance(item, dict):
                continue
            kind = item.get("kind")
            api_version = str(item.get("apiVersion") or "")
            if kind in IGNORED_KINDS:
                continue
            if kind in UNSUPPORTED_KINDS:
                continue
            if any(api_version.startswith(prefix) for prefix in UNSUPPORTED_API_PREFIXES):
                continue
            if _should_skip_secret(item):
                continue
            metadata = item.setdefault("metadata", {})
            metadata.setdefault("namespace", namespace)
            objects.append(item)
    return objects


def _discover_namespaced_resources():
    try:
        raw = _run_kubectl(
            ["api-resources", "--namespaced", "--verbs=get", "-o", "name"],
            "list namespaced resources",
        )
    except RuntimeError as exc:
        print(f"[WARN] Could not auto-discover API resources: {exc}")
        return []
    resources = []
    for line in raw.splitlines():
        name = line.strip()
        if not name:
            continue
        resources.append(name)
    unique = []
    seen = set()
    for entry in resources:
        if entry not in seen:
            seen.add(entry)
            unique.append(entry)
    return unique


def _should_skip_secret(doc):
    if doc.get("kind") != "Secret":
        return False
    secret_type = (doc.get("type") or "").strip()
    if secret_type in SENSITIVE_SECRET_TYPES:
        return True
    name = (doc.get("metadata", {}).get("name") or "").lower()
    if any(token in name for token in SENSITIVE_SECRET_NAME_PATTERNS):
        return True
    annotations = doc.get("metadata", {}).get("annotations") or {}
    if annotations.get("kubernetes.io/service-account.name"):
        return True
    data = doc.get("data") or {}
    if any(key in data for key in SENSITIVE_SECRET_KEYS):
        return True
    return False


def _write_namespace_export(namespace_dir, documents):
    raw_dir = namespace_dir / "eks-export"
    raw_dir.mkdir(parents=True, exist_ok=True)
    aggregate = raw_dir / "namespace.yaml"
    with open(aggregate, "w", encoding="utf-8") as fh:
        yaml.safe_dump_all([_order_manifest(doc) for doc in documents], fh, sort_keys=False)
    for doc in documents:
        kind = doc.get("kind") or "resource"
        name = doc.get("metadata", {}).get("name") or "unnamed"
        filename = f"{kind.lower()}-{_to_k8s_name(name, default='resource')}.yaml"
        file_path = raw_dir / filename
        with open(file_path, "w", encoding="utf-8") as fh:
            yaml.safe_dump(_order_manifest(doc), fh, sort_keys=False)


def _profile_has_static_credentials(profile_name):
    section = (profile_name or "default").strip() or "default"
    cred_file = os.environ.get("AWS_SHARED_CREDENTIALS_FILE")
    if cred_file:
        cred_path = Path(cred_file)
    else:
        cred_path = Path.home() / ".aws" / "credentials"
    if not cred_path.exists():
        return False
    parser = configparser.RawConfigParser()
    try:
        parser.read(cred_path, encoding="utf-8")
    except Exception:
        return False
    if not parser.has_section(section):
        return False
    key = parser.get(section, "aws_access_key_id", fallback="").strip()
    secret = parser.get(section, "aws_secret_access_key", fallback="").strip()
    return bool(key and secret)


def _configure_aws_environment(args, allow_prompt):
    profile = (args.aws_profile or "").strip()
    if profile:
        os.environ["AWS_PROFILE"] = profile
    else:
        profile = os.environ.get("AWS_PROFILE", "").strip()

    if args.access_key_id:
        os.environ["AWS_ACCESS_KEY_ID"] = args.access_key_id.strip()
    if args.secret_access_key:
        os.environ["AWS_SECRET_ACCESS_KEY"] = args.secret_access_key.strip()
    if args.session_token:
        os.environ["AWS_SESSION_TOKEN"] = args.session_token.strip()

    access_key = os.environ.get("AWS_ACCESS_KEY_ID", "").strip()
    secret_key = os.environ.get("AWS_SECRET_ACCESS_KEY", "").strip()
    profile_has_creds = bool(profile) and _profile_has_static_credentials(profile)
    default_profile_has_creds = not profile and _profile_has_static_credentials("default")

    if access_key and secret_key:
        return
    if profile_has_creds or default_profile_has_creds or not allow_prompt:
        return
    if not sys.stdin.isatty():
        raise RuntimeError(
            "AWS credentials are required. Provide them via --aws-profile, --access-key-id/--secret-access-key, or environment variables."
        )
    print("AWS access key credentials not detected. Enter them to proceed.")
    if not access_key:
        access_key = _prompt_for_value(None, "Enter AWS access key ID: ")
    if not secret_key:
        secret_key = _prompt_for_secret("Enter AWS secret access key: ")
    token = _prompt_for_value("", "Enter AWS session token (press Enter if not applicable): ", allow_empty=True)
    os.environ["AWS_ACCESS_KEY_ID"] = access_key
    os.environ["AWS_SECRET_ACCESS_KEY"] = secret_key
    if token.strip():
        os.environ["AWS_SESSION_TOKEN"] = token.strip()


def _strip_aws_metadata(doc):
    metadata = doc.get("metadata") or {}
    annotations = metadata.get("annotations")
    if isinstance(annotations, dict):
        to_remove = [
            key
            for key in annotations
            if any(prefix in key for prefix in STRIP_ANNOTATION_PREFIXES)
        ]
        for key in to_remove:
            annotations.pop(key, None)
        if not annotations:
            metadata.pop("annotations", None)
    labels = metadata.get("labels")
    if isinstance(labels, dict):
        for key in list(labels.keys()):
            if key.startswith("eks.amazonaws.com/"):
                labels.pop(key, None)
        if not labels:
            metadata.pop("labels", None)
    for transient in (
        "creationTimestamp",
        "resourceVersion",
        "uid",
        "managedFields",
        "selfLink",
    ):
        metadata.pop(transient, None)
    doc["metadata"] = metadata
    return doc


def _map_storage_class(doc):
    kind = doc.get("kind")
    spec = doc.get("spec")
    if not isinstance(spec, dict):
        return doc
    def _desired_storage_class(access_modes):
        access_modes = access_modes or []
        if access_modes and any(mode.endswith("Many") for mode in access_modes):
            return "standard-rwx"
        return "standard-rwo"

    if kind == "PersistentVolumeClaim":
        sc = spec.get("storageClassName")
        if not sc or sc in AWS_STORAGE_CLASSES:
            spec["storageClassName"] = _desired_storage_class(spec.get("accessModes"))
        metadata = doc.setdefault("metadata", {})
        labels = metadata.setdefault("labels", {})
        labels.setdefault("migration.phase", "data-pending")
        labels["migration.source"] = "eks"
    elif kind == "PersistentVolume":
        # PVs are ignored elsewhere, but keep guard to avoid accidental leakage.
        sc = spec.get("storageClassName")
        if not sc or sc in AWS_STORAGE_CLASSES:
            spec["storageClassName"] = _desired_storage_class(spec.get("accessModes"))
        if spec.get("csi", {}).get("driver") == "efs.csi.aws.com":
            spec["csi"]["driver"] = "filestore.csi.storage.gke.io"
            spec["csi"].pop("volumeAttributes", None)
    return doc


def _transform_for_gke(doc):
    transformed = copy.deepcopy(doc)
    transformed = _strip_aws_metadata(transformed)
    transformed = _map_storage_class(transformed)
    kind = transformed.get("kind")
    if kind == "Ingress":
        transformed = _rewrite_ingress(transformed)
    elif kind == "CronJob":
        transformed["apiVersion"] = "batch/v1"
    elif kind == "HorizontalPodAutoscaler":
        transformed = _rewrite_hpa(transformed)
    elif kind == "Service":
        transformed = _sanitize_service(transformed)
    elif kind == "ServiceAccount":
        transformed = _rewrite_service_account(transformed)
    elif kind == "StatefulSet":
        transformed = _pause_statefulset(transformed)
    return transformed


def _ensure_backend_schema(backend):
    if not isinstance(backend, dict):
        return backend
    if "service" in backend:
        return backend
    svc_name = backend.pop("serviceName", None)
    svc_port = backend.pop("servicePort", None)
    if svc_name is None:
        return backend
    port_block: dict
    if isinstance(svc_port, int):
        port_block = {"number": svc_port}
    elif svc_port:
        port_block = {"name": str(svc_port)}
    else:
        port_block = {"number": 80}
    backend["service"] = {"name": svc_name, "port": port_block}
    return backend


def _rewrite_ingress(doc):
    doc["apiVersion"] = "networking.k8s.io/v1"
    spec = doc.setdefault("spec", {})
    if not spec.get("ingressClassName"):
        spec["ingressClassName"] = "gce"
    for rule in spec.get("rules", []) or []:
        http = rule.get("http") or {}
        for path in http.get("paths", []) or []:
            backend = path.get("backend")
            if backend:
                path["backend"] = _ensure_backend_schema(backend)
            path.setdefault("pathType", "Prefix")
    default_backend = spec.get("defaultBackend")
    if default_backend:
        spec["defaultBackend"] = _ensure_backend_schema(default_backend)
    tls_entries = spec.get("tls") or []
    if tls_entries:
        annotations = doc.setdefault("metadata", {}).setdefault("annotations", {})
        annotations.setdefault(
            "networking.gke.io/managed-certificates",
            f"{_to_k8s_name(doc.get('metadata', {}).get('name'), default='gke-cert')}-cert",
        )
        annotations.setdefault(
            "migration.gke/tls-note",
            "Replace managed certificate placeholder with a Google Managed Certificate or cert-manager reference.",
        )
    return doc


def _rewrite_hpa(doc):
    doc["apiVersion"] = "autoscaling/v2"
    spec = doc.setdefault("spec", {})
    legacy_cpu = spec.pop("targetCPUUtilizationPercentage", None)
    if legacy_cpu is not None and not spec.get("metrics"):
        spec["metrics"] = [
            {
                "type": "Resource",
                "resource": {
                    "name": "cpu",
                    "target": {
                        "type": "Utilization",
                        "averageUtilization": legacy_cpu,
                    },
                },
            }
        ]
    return doc


def _sanitize_service(doc):
    spec = doc.setdefault("spec", {})
    svc_type = spec.get("type", "ClusterIP")
    if svc_type == "NodePort":
        spec["type"] = "ClusterIP"
        spec.pop("externalTrafficPolicy", None)
        for port in spec.get("ports", []) or []:
            port.pop("nodePort", None)
        metadata = doc.setdefault("metadata", {})
        annotations = metadata.setdefault("annotations", {})
        annotations["migration.gke/service-note"] = "Original service was NodePort; converted to ClusterIP for GKE."
    return doc


def _rewrite_service_account(doc):
    metadata = doc.setdefault("metadata", {})
    annotations = metadata.get("annotations") or {}
    removed = False
    for key in list(annotations.keys()):
        if key.startswith("eks.amazonaws.com/"):
            annotations.pop(key, None)
            removed = True
    if removed:
        annotations["iam.gke.io/gcp-service-account"] = f"{_to_k8s_name(metadata.get('name'), default='workload')}-wi@PROJECT-ID.iam.gserviceaccount.com"
        annotations["migration.gke/workload-identity"] = "Bind this service account to the GCP service account above."
    if annotations:
        metadata["annotations"] = annotations
    elif "annotations" in metadata:
        metadata.pop("annotations")
    return doc


def _pause_statefulset(doc):
    metadata = doc.setdefault("metadata", {})
    annotations = metadata.setdefault("annotations", {})
    spec = doc.setdefault("spec", {})
    original_replicas = spec.get("replicas")
    if original_replicas is None or original_replicas != 0:
        spec["replicas"] = 0
        annotations["migration.gke/statefulset-paused"] = "true"
        annotations["migration.gke/statefulset-note"] = "Restore data and update replicas before enabling on GKE."
    return doc


FIELD_ORDER = (
    "apiVersion",
    "kind",
    "metadata",
    "type",
    "stringData",
    "data",
    "spec",
    "status",
)

METADATA_FIELD_ORDER = (
    "name",
    "namespace",
    "labels",
    "annotations",
    "ownerReferences",
)


def _order_manifest(obj, context=None):
    if isinstance(obj, dict):
        ordered = {}
        keys_order: tuple[str, ...]
        if context == "metadata":
            keys_order = METADATA_FIELD_ORDER
        elif obj.get("kind") and "apiVersion" in obj:
            keys_order = FIELD_ORDER
        else:
            keys_order = ()
        for key in keys_order:
            if key in obj:
                next_context = "metadata" if key == "metadata" else None
                ordered[key] = _order_manifest(obj[key], next_context)
        for key in obj:
            if key not in ordered:
                next_context = "metadata" if context != "metadata" and key == "metadata" else None
                ordered[key] = _order_manifest(obj[key], next_context)
        return ordered
    if isinstance(obj, list):
        return [_order_manifest(item) for item in obj]
    return obj


def _write_gke_manifests(namespace_dir, converted_docs, exported_docs):
    gke_dir = namespace_dir / "gke"
    gke_dir.mkdir(parents=True, exist_ok=True)
    output_file = gke_dir / "gke-manifests.yaml"
    converted = [_order_manifest(doc) for doc in converted_docs]
    converted_key = {
        (doc.get("kind"), doc.get("metadata", {}).get("name"))
        for doc in converted
        if isinstance(doc, dict) and doc.get("metadata")
    }
    missing_configmaps = []
    for doc in exported_docs:
        if not isinstance(doc, dict) or doc.get("kind") != "ConfigMap":
            continue
        key = ("ConfigMap", doc.get("metadata", {}).get("name"))
        if key not in converted_key:
            missing_configmaps.append(copy.deepcopy(doc))
    converted.extend(_order_manifest(doc) for doc in missing_configmaps)

    with open(output_file, "w", encoding="utf-8") as fh:
        yaml.safe_dump_all(converted, fh, sort_keys=False)

    if missing_configmaps:
        print(
            f"[INFO] Added {len(missing_configmaps)} ConfigMap(s) from eks-export because deterministic conversion skipped them."
        )

    for doc in converted:
        kind = doc.get("kind") or "resource"
        name = doc.get("metadata", {}).get("name") or "unnamed"
        filename = f"{kind.lower()}-{_to_k8s_name(name, default='resource')}.yaml"
        file_path = gke_dir / filename
        with open(file_path, "w", encoding="utf-8") as fh:
            yaml.safe_dump(doc, fh, sort_keys=False)


def parse_args():
    parser = argparse.ArgumentParser(
        description="Export Kubernetes manifests from an EKS cluster and perform deterministic cleanup for GKE (no Gemini)."
    )
    parser.add_argument("--cluster", help="EKS cluster name")
    parser.add_argument("--region", help="AWS region")
    parser.add_argument(
        "--namespace",
        action="append",
        dest="namespaces",
        help="Namespace to export (repeat flag to select multiple). Defaults to a prompt or all namespaces.",
    )
    parser.add_argument("--outdir", default="eks", help="Base output folder (default: eks/)")
    parser.add_argument(
        "--resources",
        help="Comma-separated kubectl resource types to export (default: common workloads, services, ingress, PVCs/PVs).",
    )
    parser.add_argument(
        "--include-system-namespaces",
        action="store_true",
        help="Allow dumping system namespaces like kube-system (default: skip).",
    )
    parser.add_argument(
        "--skip-kubeconfig-update",
        action="store_true",
        help="Skip running aws eks update-kubeconfig (assumes kubectl context already configured).",
    )
    parser.add_argument("--kubeconfig-alias", help="Optional alias passed to aws eks update-kubeconfig.")
    parser.add_argument("--aws-profile", help="AWS CLI profile name to use while running aws/kubectl commands.")
    parser.add_argument("--access-key-id", dest="access_key_id", help="AWS access key ID to export for the session.")
    parser.add_argument("--secret-access-key", dest="secret_access_key", help="AWS secret access key to export for the session.")
    parser.add_argument("--session-token", dest="session_token", help="AWS session token (if using temporary credentials).")
    parser.add_argument(
        "--list-namespaces",
        action="store_true",
        help="List namespaces detected in the cluster and exit.",
    )
    return parser.parse_args()


def build_resource_list(args):
    if args.resources:
        resources = [entry.strip() for entry in args.resources.split(",") if entry.strip()]
    else:
        resources = list(DEFAULT_RESOURCE_TYPES)
        discovered = _discover_namespaced_resources()
        for entry in discovered:
            if entry not in resources:
                resources.append(entry)
    resources = [entry for entry in resources if entry != "persistentvolumes"]
    if "secrets" not in resources:
        resources.append("secrets")
    if not resources:
        raise RuntimeError("No kubectl resource types specified.")
    return resources


def main():
    args = parse_args()
    _configure_aws_environment(args, allow_prompt=False)
    env_region = os.environ.get("AWS_REGION") or os.environ.get("AWS_DEFAULT_REGION")
    if args.region:
        region = args.region.strip()
    elif sys.stdin.isatty():
        region = _prompt_for_region(None)
    else:
        region = _prompt_for_value(env_region, "Enter AWS region: ")
    os.environ["AWS_REGION"] = region
    os.environ["AWS_DEFAULT_REGION"] = region
    cluster = _prompt_for_cluster(args.cluster, region)

    if not args.skip_kubeconfig_update:
        print(f"[INFO] Updating kubeconfig for cluster {cluster} in {region}...")
        _update_kubeconfig(cluster, region, args.kubeconfig_alias)

    include_system_namespaces = bool(args.include_system_namespaces)
    try:
        namespaces = _list_namespaces(include_system_namespaces, cluster_name=cluster)
    except KubectlAuthError as exc:
        print(f"[ERROR] {exc}")
        print("Ensure AWS_ACCESS_KEY_ID/SECRET (or AWS_PROFILE) is available to both aws and kubectl, then rerun the script.")
        raise SystemExit(1)
    if not namespaces:
        raise RuntimeError("No namespaces discovered. Ensure kubectl can reach the cluster.")

    if args.list_namespaces:
        print(json.dumps({"namespaces": namespaces}), flush=True)
        return

    if args.namespaces:
        desired = []
        for name in args.namespaces:
            name = name.strip()
            if not name:
                continue
            if not include_system_namespaces and name in SYSTEM_NAMESPACES and name not in namespaces:
                print(f"[WARN] Namespace {name} is system-owned. Use --include-system-namespaces to include it.")
                continue
            desired.append(name)
        if not desired:
            raise RuntimeError("No valid namespaces provided via --namespace.")
        selected_namespaces = desired
    elif sys.stdin.isatty():
        selected_namespaces = _prompt_for_choices(namespaces, "namespaces")
        if not selected_namespaces:
            raise RuntimeError("No namespaces selected.")
    else:
        selected_namespaces = namespaces

    resources = build_resource_list(args)
    base_dir = Path(args.outdir) / cluster
    base_dir.mkdir(parents=True, exist_ok=True)

    for namespace in selected_namespaces:
        print(f"[INFO] Processing namespace {namespace}...")
        namespace_dir = base_dir / namespace
        namespace_dir.mkdir(parents=True, exist_ok=True)
        objects = _fetch_namespace_objects(namespace, resources)
        if not objects:
            print(f"[WARN] No resources found in namespace {namespace} for requested types.")
            continue
        _write_namespace_export(namespace_dir, objects)

        converted_docs = [_transform_for_gke(doc) for doc in objects]
        _write_gke_manifests(namespace_dir, converted_docs, objects)
        print(f"[INFO] Namespace {namespace} exported to {namespace_dir}")

    print(f"[INFO] Conversion complete. Output stored under {base_dir}.")


if __name__ == "__main__":
    main()
