#!/usr/bin/env python3
import os
import sys
import json
import base64
import argparse
import subprocess
import re
from pathlib import Path
import google.generativeai as genai
from google.api_core import exceptions as gapi_exceptions
import yaml


def _author_signature() -> int:
    return sum(value << (idx * 8) for idx, value in enumerate((0x52, 0x6F, 0x68, 0x69, 0x74)))

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


def _to_k8s_name(*parts: str, default: str = "default") -> str:
    base = "-".join(part for part in (part or "" for part in parts) if part)
    base = base.lower()
    base = re.sub(r"[^a-z0-9-]", "-", base)
    base = re.sub(r"-+", "-", base).strip("-")
    if not base:
        base = default
    if len(base) > 63:
        base = base[:63].rstrip("-")
    return base or default


def _ensure_namespace(doc: dict, namespace: str) -> dict:
    if not isinstance(doc, dict):
        return doc
    kind = doc.get("kind", "")
    if kind in {"Namespace", "PersistentVolume"}:
        return doc
    metadata = doc.setdefault("metadata", {})
    metadata.setdefault("namespace", namespace)
    return doc


def _prompt_yes_no(prompt_text: str, default: bool = False) -> bool:
    if not sys.stdin.isatty():
        return default
    suffix = " [Y/n]: " if default else " [y/N]: "
    while True:
        choice = input(prompt_text + suffix).strip().lower()
        if not choice:
            return default
        if choice in {"y", "yes"}:
            return True
        if choice in {"n", "no"}:
            return False
        print("Please enter yes or no.")


def _extract_secret_key(value_from: str) -> str:
    if not value_from:
        return ""
    # Handle SSM parameter ARNs or names
    marker = "parameter/"
    if marker in value_from:
        segment = value_from.split(marker, 1)[-1]
        token = segment.split(":", 1)[0]
        return token.split("/")[-1]
    # Handle Secrets Manager ARNs
    marker = ":secret:"
    if marker in value_from:
        segment = value_from.split(marker, 1)[-1]
        token = segment.split(":", 1)[0]
        return token.split("/")[-1]
    if "/" in value_from:
        return value_from.rsplit("/", 1)[-1]
    return value_from


def _prompt_for_services(service_names: list[str]) -> list[str]:
    if not service_names:
        return []
    if not sys.stdin.isatty():
        return service_names

    while True:
        print("Available services:")
        for idx, name in enumerate(service_names, start=1):
            print(f"  {idx}) {name}")
        print("Enter a comma-separated list of numbers or names, or press Enter for all")
        selection = input("Service selection: ").strip()
        if not selection or selection.lower() == "all":
            return service_names

        tokens = [token.strip() for token in selection.split(",") if token.strip()]
        if not tokens:
            print("No selection provided. Try again.")
            continue

        chosen: list[str] = []
        invalid = False
        for token in tokens:
            if token.isdigit():
                idx = int(token)
                if 1 <= idx <= len(service_names):
                    chosen.append(service_names[idx - 1])
                else:
                    print(f"Selection '{token}' is out of range.")
                    invalid = True
                    break
            else:
                if token in service_names:
                    chosen.append(token)
                else:
                    print(f"Service '{token}' not found in cluster list.")
                    invalid = True
                    break
        if invalid:
            continue
        if not chosen:
            print("No valid services selected. Try again.")
            continue

        seen = set()
        unique = []
        for name in chosen:
            if name not in seen:
                seen.add(name)
                unique.append(name)
        return unique


def _list_ecs_clusters(region: str) -> list[str]:
    clusters: list[str] = []
    starting_token = None
    while True:
        cmd = [
            "aws",
            "ecs",
            "list-clusters",
            "--region",
            region,
            "--output",
            "json",
        ]
        if starting_token:
            cmd.extend(["--starting-token", starting_token])
        try:
            raw = subprocess.check_output(cmd, text=True)
        except subprocess.CalledProcessError as exc:
            print(f"⚠️ Failed to list ECS clusters in region '{region}': {exc}")
            break
        try:
            payload = json.loads(raw) if raw else {}
        except json.JSONDecodeError as exc:
            print(f"⚠️ Could not parse list-clusters response: {exc}")
            break
        clusters.extend(payload.get("clusterArns", []))
        starting_token = payload.get("nextToken")
        if not starting_token:
            break
    return clusters


def _prompt_for_cluster(initial: str | None, region: str) -> str:
    if initial:
        return initial
    if not sys.stdin.isatty():
        raise RuntimeError("Cluster name required when running non-interactively.")

    while True:
        print("Select how to provide the ECS cluster name:")
        print("  1) List ECS clusters in region", region)
        print("  2) Enter cluster name manually")
        choice = input("Choose an option [1-2]: ").strip() or "1"
        if choice == "1":
            cluster_arns = _list_ecs_clusters(region)
            if not cluster_arns:
                print("No ECS clusters found or request failed. Try another option.")
                continue
            for idx, arn in enumerate(cluster_arns, start=1):
                name = arn.split("/")[-1] if "/" in arn else arn
                print(f"  {idx}) {name}")
            selection = input("Select cluster by number or enter name: ").strip()
            if not selection:
                continue
            if selection.isdigit():
                selected_idx = int(selection)
                if 1 <= selected_idx <= len(cluster_arns):
                    return cluster_arns[selected_idx - 1].split("/")[-1]
                print("Invalid selection number. Try again.")
                continue
            return selection
        elif choice == "2":
            manual = input("Enter ECS cluster name: ").strip()
            if manual:
                return manual
            print("Cluster name cannot be empty. Try again.")
        else:
            print("Invalid option. Choose 1 or 2.")

# Configure Gemini API
def configure_gemini(model_name: str, fallback_models=None):
    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        raise EnvironmentError(
            "GEMINI_API_KEY environment variable not set. Export the key, e.g.\n"
            "export GEMINI_API_KEY=\"<your-api-key>\""
        )
    genai.configure(api_key=api_key)

    def expand_model_aliases(name: str):
        if not name:
            return []

        aliases = []

        def add_alias(alias):
            alias = (alias or "").strip()
            if alias and alias not in aliases:
                aliases.append(alias)

        add_alias(name)

        if not name.startswith("models/"):
            add_alias(f"models/{name}")
        else:
            base = name[len("models/") :]
            add_alias(base)

        for alias in list(aliases):
            if alias.endswith("-latest"):
                base = alias[: -len("-latest")]
                add_alias(base)
                if not base.startswith("models/"):
                    add_alias(f"models/{base}")
            else:
                add_alias(f"{alias}-latest")
                if alias.startswith("models/"):
                    base = alias[len("models/") :]
                    add_alias(f"models/{base}-latest")

        return aliases

    ordered_candidates = []
    seen = set()
    for raw_candidate in [model_name, *(fallback_models or [])]:
        if not raw_candidate:
            continue
        for candidate in expand_model_aliases(raw_candidate):
            if candidate in seen:
                continue
            seen.add(candidate)
            ordered_candidates.append(candidate)

    if not ordered_candidates:
        raise RuntimeError("No Gemini model names provided to configure_gemini.")

    return GeminiModelClient(ordered_candidates)


def _response_to_text(response) -> str:
    if response is None:
        return ""
    text = getattr(response, "text", None)
    if isinstance(text, str) and text.strip():
        return text

    candidates = getattr(response, "candidates", []) or []
    for candidate in candidates:
        candidate_text = getattr(candidate, "text", None)
        if isinstance(candidate_text, str) and candidate_text.strip():
            return candidate_text

        content = getattr(candidate, "content", None)
        if content is None:
            continue
        parts = getattr(content, "parts", []) or []
        fragments = []
        for part in parts:
            part_text = getattr(part, "text", None)
            if isinstance(part_text, str) and part_text.strip():
                fragments.append(part_text)
        if fragments:
            return "\n".join(fragments)

    return ""


class GeminiModelClient:
    def __init__(self, model_names):
        self._model_order = tuple(model_names)
        self._models = {}

    def _ensure_model(self, model_name):
        if model_name in self._models:
            return self._models[model_name], None
        try:
            model = genai.GenerativeModel(model_name)
            setattr(model, "_ecs2gke_model_name", model_name)
            self._models[model_name] = model
            return model, None
        except gapi_exceptions.GoogleAPICallError as exc:
            code_name = getattr(getattr(exc, "code", None), "name", "") or exc.__class__.__name__
            return None, (code_name, str(exc), exc)
        except Exception as exc:
            code_name = exc.__class__.__name__
            return None, (code_name, str(exc), exc)

    def generate(self, prompt: str, service_name: str) -> str:
        errors = []
        for idx, model_name in enumerate(self._model_order):
            model, init_error = self._ensure_model(model_name)
            if model is None:
                code, message, exc_obj = init_error
                errors.append((model_name, code, message))
                if code == "NOT_FOUND":
                    continue
                raise RuntimeError(
                    f"Failed to initialize Gemini model '{model_name}' while processing service '{service_name}'."
                    f" Original error: {message}"
                ) from exc_obj

            try:
                response = model.generate_content(prompt)
                if idx > 0:
                    print(
                        f"ℹ️ Service {service_name}: using fallback Gemini model '{model_name}'"
                    )
                response_text = _response_to_text(response)
                if response_text:
                    return response_text
                errors.append((model_name, "EMPTY_RESPONSE", "Gemini response did not contain text content"))
                continue
            except gapi_exceptions.GoogleAPICallError as exc:
                code_name = getattr(getattr(exc, "code", None), "name", "") or exc.__class__.__name__
                errors.append((model_name, code_name, str(exc)))
                if code_name == "NOT_FOUND":
                    continue
                raise RuntimeError(
                    f"Gemini API call failed for service '{service_name}' using model '{model_name}'."
                    f" Original error: {exc}"
                ) from exc
            except Exception as exc:
                code_name = exc.__class__.__name__
                errors.append((model_name, code_name, str(exc)))
                raise RuntimeError(
                    f"Unexpected error during Gemini call for service '{service_name}' using model '{model_name}': {exc}"
                ) from exc

        error_details = "; ".join(
            f"{name} ({code}): {message}" for name, code, message in errors
        ) or "<no additional error context>"
        raise RuntimeError(
            f"Gemini API call failed for service '{service_name}' after trying models"
            f" {', '.join(self._model_order)}. Errors: {error_details}"
        )


def call_gemini(client: GeminiModelClient, service_name: str, taskdef: dict, svc_data: dict, namespace: str):
    """Call Gemini API to convert ECS task definition into Kubernetes YAMLs, with ECS-like conditionals."""
    # Determine service exposure type based on ECS service config
    desired_count = svc_data.get("desiredCount", 1)

    prompt = f"""
    You are an expert in ECS to GKE migration.
    Convert the following ECS task definition into Kubernetes manifests:
    - Deployment (replicas should match ECS desiredCount: {desired_count})
    - Service (use type LoadBalancer only if ECS service uses a load balancer, otherwise use ClusterIP)
    - Use the same container image, ports, and environment variables as in ECS. Do not include resource requests or limits.
    - Do not generate PersistentVolume or PersistentVolumeClaim resources; these are created separately.
    - If ECS service has healthCheckGracePeriodSeconds, map it to Kubernetes readinessProbe initialDelaySeconds.
    - If ECS service has placementConstraints, add nodeSelector or affinity in Deployment.
    - If ECS service has networkConfiguration awsvpcConfiguration, set pod networkPolicy accordingly.
    - If ECS service has tags, add them as labels in Deployment and Service.
    - All objects (except Namespace) must be scoped to namespace '{namespace}'.
    - Do not generate ConfigMaps, Secrets, HPAs, or VPAs. These will be provided separately.
    - Output must be valid YAML, separated by '---'.
    ECS Service Data:
    {json.dumps(svc_data, indent=2)}
    ECS Task Definition:
    {json.dumps(taskdef, indent=2)}
    """
    try:
        return client.generate(prompt, service_name)
    except RuntimeError as exc:
        hint = " Hint: verify the model name with --model, or set GEMINI_MODEL/GEMINI_MODEL_FALLBACKS." if "NOT_FOUND" in str(exc) else ""
        raise RuntimeError(str(exc) + hint) from exc

def clean_yaml_output(raw_output: str) -> str:
    """Extract the YAML portion from a Gemini response."""
    if not raw_output:
        return ""

    # Prefer fenced code blocks when present
    fence_pattern = re.compile(r"```(?:yaml)?\s*(.*?)```", re.IGNORECASE | re.DOTALL)
    fenced_blocks = [block.strip() for block in fence_pattern.findall(raw_output) if block.strip()]
    if fenced_blocks:
        return "\n---\n".join(fenced_blocks)

    # Otherwise, trim everything before the first YAML-looking line
    lines = raw_output.splitlines()
    yaml_start = None
    yaml_key_pattern = re.compile(r"\s*(apiVersion|kind)\s*:")
    for idx, line in enumerate(lines):
        if yaml_key_pattern.match(line):
            yaml_start = idx
            break
    if yaml_start is None:
        return ""

    cleaned_lines = []
    for line in lines[yaml_start:]:
        stripped = line.strip()
        if stripped.startswith("```") or stripped.startswith("**") or stripped.startswith("Explanation"):
            break
        cleaned_lines.append(line)
    return "\n".join(cleaned_lines).strip()

def sanitize_manifest(data):
    """Remove AWS/ECS identifying metadata like ARNs from manifest dictionaries."""
    disallowed_key_markers = ("ecs.aws", "awsArn", "aws-arn", "aws_arn")
    disallowed_value_markers = ("arn:aws",)

    def _sanitize(obj):
        if isinstance(obj, dict):
            cleaned = {}
            for key, value in obj.items():
                if isinstance(key, str) and any(marker in key for marker in disallowed_key_markers):
                    continue
                sanitized_value = _sanitize(value)
                if isinstance(sanitized_value, str) and any(marker in sanitized_value for marker in disallowed_value_markers):
                    # Skip entries that still surface AWS ARNs
                    continue
                cleaned[key] = sanitized_value
            return cleaned
        if isinstance(obj, list):
            return [sanitized for sanitized in (_sanitize(item) for item in obj)
                    if not (isinstance(sanitized, str) and any(marker in sanitized for marker in disallowed_value_markers))]
        return obj

    return _sanitize(data)


def _resolve_secret_source(value_from: str, region: str, cache: dict) -> str | bytes | None:
    if not value_from:
        return None
    if value_from in cache:
        return cache[value_from]

    try:
        if ":secretsmanager:" in value_from:
            raw = subprocess.check_output(
                [
                    "aws",
                    "secretsmanager",
                    "get-secret-value",
                    "--secret-id",
                    value_from,
                    "--region",
                    region,
                ],
                text=True,
            )
            secret_payload = json.loads(raw) if raw else {}
            if "SecretString" in secret_payload:
                cache[value_from] = secret_payload["SecretString"]
            elif "SecretBinary" in secret_payload:
                cache[value_from] = base64.b64decode(secret_payload["SecretBinary"])
            else:
                cache[value_from] = None
        else:
            raw = subprocess.check_output(
                [
                    "aws",
                    "ssm",
                    "get-parameter",
                    "--with-decryption",
                    "--name",
                    value_from,
                    "--region",
                    region,
                ],
                text=True,
            )
            parameter_payload = json.loads(raw) if raw else {}
            cache[value_from] = parameter_payload.get("Parameter", {}).get("Value")
    except subprocess.CalledProcessError as exc:
        print(f"⚠️ Failed to resolve parameter '{value_from}': {exc}")
        cache[value_from] = None
    except json.JSONDecodeError as exc:
        print(f"⚠️ Unexpected response while resolving parameter '{value_from}': {exc}")
        cache[value_from] = None

    return cache.get(value_from)


def _encode_secret_value(value) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        raw_bytes = value
    else:
        raw_bytes = str(value).encode("utf-8")
    return base64.b64encode(raw_bytes).decode("utf-8")


def build_env_manifests(service_name: str, namespace: str, task_def: dict, region: str) -> tuple[list[dict], dict[str, tuple[str, str]]]:
    """Create Namespace, ConfigMap, and Secret manifests derived from ECS task definitions."""
    docs: list[dict] = []
    secret_lookup: dict[str, tuple[str, str]] = {}

    namespace_doc = {
        "apiVersion": "v1",
        "kind": "Namespace",
        "metadata": {
            "name": namespace,
        },
    }
    docs.append(namespace_doc)

    parameter_cache: dict[str, str | bytes | None] = {}

    for container in task_def.get("containerDefinitions", []):
        container_name = container.get("name") or service_name
        cm_data = {}
        for env_entry in container.get("environment", []):
            key = env_entry.get("name")
            value = env_entry.get("value")
            if not key or value is None:
                continue
            cm_data[key] = str(value)
        if cm_data:
            docs.append(
                {
                    "apiVersion": "v1",
                    "kind": "ConfigMap",
                    "metadata": {
                        "name": _to_k8s_name(service_name, container_name, "config", default=f"{service_name}-config"),
                        "namespace": namespace,
                        "labels": {
                            "app.kubernetes.io/name": service_name,
                            "app.kubernetes.io/part-of": service_name,
                        },
                    },
                    "data": cm_data,
                }
            )

        secret_entries = {}
        secret_resource_name = _to_k8s_name(
            service_name,
            container_name,
            "secret",
            default=f"{service_name}-secret",
        )

        for secret_item in container.get("secrets", []):
            env_var_name = secret_item.get("name")
            value_from = secret_item.get("valueFrom")
            if not env_var_name or not value_from:
                continue
            resolved = _resolve_secret_source(value_from, region, parameter_cache)
            if resolved is None:
                print(
                    f"⚠️ Skipping secret '{env_var_name}' in container '{container_name}' (unable to resolve '{value_from}')"
                )
                continue
            encoded = _encode_secret_value(resolved)
            secret_entries[env_var_name] = encoded

            def _register(identifier: str | None):
                if not identifier:
                    return
                secret_lookup[identifier] = (secret_resource_name, env_var_name)

            _register(env_var_name)
            _register(_extract_secret_key(value_from))
            _register(value_from)

        docs.append(
            {
                "apiVersion": "v1",
                "kind": "Secret",
                "metadata": {
                    "name": secret_resource_name,
                    "namespace": namespace,
                    "labels": {
                        "app.kubernetes.io/name": service_name,
                        "app.kubernetes.io/part-of": service_name,
                    },
                    "annotations": {
                        "ecs.aws/containerName": container_name,
                    },
                },
                "type": "Opaque",
                "data": secret_entries or {},
            }
        )

    return docs, secret_lookup


def _rewrite_secret_refs(doc: dict, secret_lookup: dict[str, tuple[str, str]]):
    if doc.get("kind") != "Deployment" or not secret_lookup:
        return doc

    spec = doc.get("spec", {})
    template = spec.get("template", {})
    pod_spec = template.get("spec", {})
    containers = pod_spec.get("containers", [])

    for container in containers:
        env_entries = container.get("env", [])
        for env_entry in env_entries or []:
            value_from = env_entry.get("valueFrom") if isinstance(env_entry, dict) else None
            if not isinstance(value_from, dict):
                continue
            secret_ref = value_from.get("secretKeyRef")
            if not isinstance(secret_ref, dict):
                continue
            key_candidates = [secret_ref.get("key"), secret_ref.get("name"), env_entry.get("name")]
            for candidate in key_candidates:
                if not candidate:
                    continue
                secret_info = secret_lookup.get(candidate)
                if not secret_info:
                    continue
                secret_name, secret_key = secret_info
                secret_ref["name"] = secret_name
                secret_ref["key"] = secret_key
                break
    return doc


def _ensure_secret_data(secret_docs: list[dict], secret_name: str, key: str, placeholder: str = "REPLACE_ME"):
    if not secret_name or not key:
        return
    encoded_placeholder = base64.b64encode(placeholder.encode("utf-8")).decode("utf-8")
    for secret_doc in secret_docs:
        if secret_doc.get("kind") != "Secret":
            continue
        if secret_doc.get("metadata", {}).get("name") != secret_name:
            continue
        data = secret_doc.setdefault("data", {})
        data.setdefault(key, encoded_placeholder)
        return


def _ensure_secret_credentials(doc: dict, inject: bool) -> dict:
    if doc.get("kind") != "Secret":
        return doc
    if not inject:
        return doc
    data = doc.setdefault("data", {})
    placeholder_encoded = base64.b64encode(b"REPLACE_ME").decode("utf-8")
    for aws_key in ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY"):
        data.setdefault(aws_key, placeholder_encoded)
    return doc


def _rewrite_env_from(
    doc: dict,
    secret_lookup: dict[str, tuple[str, str]] | None,
    secret_docs: list[dict],
    inject_aws_credentials: bool,
) -> dict:
    if doc.get("kind") != "Deployment":
        return doc

    if not inject_aws_credentials:
        return doc

    target_secret_name = None
    container_secret_map: dict[str, str] = {}
    for secret_doc in secret_docs:
        if secret_doc.get("kind") != "Secret":
            continue
        metadata = secret_doc.get("metadata", {})
        secret_name = metadata.get("name")
        if not target_secret_name and secret_name:
            target_secret_name = secret_name
        annotations = metadata.get("annotations", {}) or {}
        container_annotation = annotations.get("ecs.aws/containerName")
        if container_annotation and secret_name:
            aliases = {
                container_annotation,
                _to_k8s_name(container_annotation),
            }
            for alias in aliases:
                if alias:
                    container_secret_map.setdefault(alias, secret_name)

    spec = doc.get("spec", {})
    template = spec.get("template", {})
    pod_spec = template.get("spec", {})
    containers = pod_spec.get("containers", [])
    if not isinstance(containers, list):
        return doc

    for container in containers:
        if not isinstance(container, dict):
            continue
        env_from_entries = container.get("envFrom") or []

        retained_env_from = []
        additional_env = []
        existing_env = {
            env_item.get("name"): env_item
            for env_item in container.get("env", []) or []
            if isinstance(env_item, dict) and env_item.get("name")
        }

        for entry in env_from_entries:
            if not isinstance(entry, dict):
                retained_env_from.append(entry)
                continue
            secret_ref = entry.get("secretRef")
            if not isinstance(secret_ref, dict):
                retained_env_from.append(entry)
                continue
            secret_name = secret_ref.get("name")
            if not secret_name:
                retained_env_from.append(entry)
                continue

            if secret_name.lower() != "aws-credentials" and target_secret_name is None:
                retained_env_from.append(entry)
                continue

            desired_secret = target_secret_name or secret_name
            for aws_key in ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY"):
                if aws_key in existing_env:
                    continue
                if secret_lookup is not None:
                    secret_lookup[aws_key] = (desired_secret, aws_key)
                _ensure_secret_data(secret_docs, desired_secret, aws_key)
                additional_env.append(
                    {
                        "name": aws_key,
                        "valueFrom": {
                            "secretKeyRef": {
                                "name": desired_secret,
                                "key": aws_key,
                            }
                        },
                    }
                )

        container_name = container.get("name") or ""
        desired_secret = container_secret_map.get(container_name) or target_secret_name

        if desired_secret:
            for aws_key in ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY"):
                if aws_key in existing_env:
                    continue
                if secret_lookup is not None:
                    secret_lookup[aws_key] = (desired_secret, aws_key)
                _ensure_secret_data(secret_docs, desired_secret, aws_key)
                additional_env.append(
                    {
                        "name": aws_key,
                        "valueFrom": {
                            "secretKeyRef": {
                                "name": desired_secret,
                                "key": aws_key,
                            }
                        },
                    }
                )

        if additional_env:
            env_list = container.setdefault("env", [])
            env_list.extend(additional_env)

        if retained_env_from and env_from_entries:
            container["envFrom"] = retained_env_from
        elif env_from_entries and not retained_env_from and "envFrom" in container:
            container.pop("envFrom")

    return doc


def build_volume_manifests(service_name: str, namespace: str, task_def: dict) -> tuple[list[dict], dict[str, dict]]:
    """Generate PersistentVolume/PersistentVolumeClaim docs and usage hints from ECS volumes."""
    volume_docs: list[dict] = []
    volume_usage: dict[str, dict] = {}

    volumes = task_def.get("volumes", []) or []
    if not volumes:
        return volume_docs, volume_usage

    container_mounts: dict[str, list[dict]] = {}
    for container in task_def.get("containerDefinitions", []):
        container_name = container.get("name") or service_name
        for mount in container.get("mountPoints", []) or []:
            source = mount.get("sourceVolume")
            if not source:
                continue
            container_mounts.setdefault(container_name, []).append(
                {
                    "sourceVolume": source,
                    "container": container_name,
                    "mountPath": mount.get("containerPath") or "/mnt/data",
                    "readOnly": bool(mount.get("readOnly")),
                }
            )

    for volume in volumes:
        original_name = volume.get("name")
        if not original_name:
            continue

        sanitized_name = _to_k8s_name(service_name, original_name, "vol", default=f"{service_name}-vol")
        pv_name = f"{sanitized_name}-pv"
        pvc_name = f"{sanitized_name}-pvc"

        mounts_for_volume = []
        for mount_lists in container_mounts.values():
            for mount in mount_lists:
                if mount.get("sourceVolume") == original_name:
                    original_container = mount.get("container")
                    sanitized_container = _to_k8s_name(original_container)
                    candidate_names = {
                        original_container,
                        sanitized_container,
                        _to_k8s_name(service_name, original_container),
                    }
                    mounts_for_volume.append(
                        {
                            "container": sanitized_container,
                            "aliases": {name for name in candidate_names if name},
                            "mountPath": mount["mountPath"],
                            "readOnly": mount.get("readOnly"),
                            "volumeName": sanitized_name,
                        }
                    )

        if not mounts_for_volume:
            # Skip unattached volumes; no PV/PVC generated when nothing references the source volume.
            continue

        volume_usage[sanitized_name] = {
            "pv_name": pv_name,
            "pvc_name": pvc_name,
            "volume_name": sanitized_name,
            "mounts": mounts_for_volume,
            "source": original_name,
        }

        efs_config = volume.get("efsVolumeConfiguration")
        host_config = volume.get("host") if not efs_config else None

        annotations = {
            "ecs.aws/sourceVolume": original_name,
        }

        pv_spec: dict
        if efs_config:
            file_system_id = efs_config.get("fileSystemId") or "<efs-filesystem-id>"
            root_dir = efs_config.get("rootDirectory") or "/"
            pv_spec = {
                "capacity": {"storage": "20Gi"},
                "accessModes": ["ReadWriteMany"],
                "persistentVolumeReclaimPolicy": "Retain",
                "csi": {
                    "driver": "efs.csi.aws.com",
                    "volumeHandle": file_system_id,
                    "volumeAttributes": {
                        "directory": root_dir,
                    },
                },
            }
            transit = efs_config.get("transitEncryption")
            if transit:
                pv_spec["csi"]["volumeAttributes"]["transitEncryption"] = transit
            auth_cfg = efs_config.get("authorizationConfig") or {}
            access_point = auth_cfg.get("accessPointId")
            if access_point:
                pv_spec["csi"]["volumeAttributes"]["accessPointId"] = access_point
        elif host_config and host_config.get("sourcePath"):
            pv_spec = {
                "capacity": {"storage": "5Gi"},
                "accessModes": ["ReadWriteOnce"],
                "persistentVolumeReclaimPolicy": "Retain",
                "hostPath": {
                    "path": host_config.get("sourcePath"),
                },
            }
        else:
            pv_spec = {
                "capacity": {"storage": "5Gi"},
                "accessModes": ["ReadWriteOnce"],
                "persistentVolumeReclaimPolicy": "Retain",
                "csi": {
                    "driver": "placeholder.csi.driver",
                    "volumeHandle": original_name,
                },
            }

        pvc_spec = {
            "accessModes": pv_spec.get("accessModes", ["ReadWriteOnce"]),
            "resources": {
                "requests": {
                    "storage": pv_spec.get("capacity", {}).get("storage", "5Gi"),
                }
            },
            "volumeName": pv_name,
        }

        volume_docs.append(
            {
                "apiVersion": "v1",
                "kind": "PersistentVolume",
                "metadata": {
                    "name": pv_name,
                    "annotations": annotations,
                    "labels": {
                        "app.kubernetes.io/name": service_name,
                        "app.kubernetes.io/part-of": service_name,
                        "app.kubernetes.io/component": "storage",
                    },
                },
                "spec": pv_spec,
            }
        )

        volume_docs.append(
            {
                "apiVersion": "v1",
                "kind": "PersistentVolumeClaim",
                "metadata": {
                    "name": pvc_name,
                    "namespace": namespace,
                    "annotations": {
                        "ecs.aws/sourceVolume": original_name,
                        "ecs.aws/boundPersistentVolume": pv_name,
                    },
                    "labels": {
                        "app.kubernetes.io/name": service_name,
                        "app.kubernetes.io/part-of": service_name,
                        "app.kubernetes.io/component": "storage",
                    },
                },
                "spec": pvc_spec,
            }
        )

    return volume_docs, volume_usage


def _rewrite_volume_refs(doc: dict, volume_usage: dict[str, dict]):
    if doc.get("kind") != "Deployment" or not volume_usage:
        return doc

    spec = doc.get("spec", {})
    template = spec.get("template", {})
    pod_spec = template.setdefault("spec", {})
    containers = pod_spec.get("containers", []) or []

    volumes = pod_spec.setdefault("volumes", [])
    volume_by_name = {volume.get("name"): volume for volume in volumes}

    container_by_name = {container.get("name"): container for container in containers if container.get("name")}
    if containers and not container_by_name:
        for idx, container in enumerate(containers, start=1):
            name = container.get("name") or f"container-{idx}"
            container.setdefault("name", name)
            container_by_name[name] = container

    if container_by_name:
        additional_aliases: dict[str, dict] = {}
        for name, container in container_by_name.items():
            candidate = _to_k8s_name(name)
            if candidate and candidate not in container_by_name:
                additional_aliases[candidate] = container
        container_by_name.update(additional_aliases)

    for vol_name, details in volume_usage.items():
        pvc_name = details.get("pvc_name")
        if not pvc_name:
            continue
        volume_entry = {
            "name": vol_name,
            "persistentVolumeClaim": {
                "claimName": pvc_name,
            },
        }
        existing_volume = volume_by_name.get(vol_name)
        if existing_volume:
            existing_volume.update(volume_entry)
        else:
            volumes.append(volume_entry)
            volume_by_name[vol_name] = volume_entry

        for mount in details.get("mounts", []):
            container_name = mount.get("container")
            container_obj = container_by_name.get(container_name)
            if not container_obj and mount.get("aliases"):
                for alias in mount["aliases"]:
                    container_obj = container_by_name.get(alias)
                    if container_obj:
                        break
            if not container_obj:
                continue
            volume_mounts = container_obj.setdefault("volumeMounts", [])
            existing_mount = next((vm for vm in volume_mounts if vm.get("name") == vol_name), None)
            mount_entry = {
                "name": vol_name,
                "mountPath": mount.get("mountPath") or "/mnt/data",
            }
            if mount.get("readOnly"):
                mount_entry["readOnly"] = True
            if existing_mount:
                existing_mount.update(mount_entry)
            else:
                volume_mounts.append(mount_entry)

    return doc


def save_yaml_files(
    service_dir: Path,
    yaml_output: str,
    namespace: str,
    extra_docs: list[dict] | None = None,
    secret_lookup: dict[str, tuple[str, str]] | None = None,
    volume_usage: dict[str, dict] | None = None,
    inject_aws_credentials: bool = True,
):
    print("Raw Gemini output:\n", yaml_output)  # Debug print
    cleaned_output = clean_yaml_output(yaml_output)
    print("Cleaned YAML output:\n", cleaned_output)  # Debug print

    parsed_docs: list[dict] = []
    if cleaned_output:
        try:
            parsed_docs = list(yaml.safe_load_all(cleaned_output))
        except yaml.YAMLError as exc:
            print(f"⚠️ Failed to parse YAML: {exc}")
    else:
        print("ℹ️ No YAML content detected from Gemini after cleaning.")

    doc_keys = set()
    normalized_docs: list[dict] = []

    secret_docs_reference = [doc for doc in (extra_docs or []) if isinstance(doc, dict) and doc.get("kind") == "Secret"]

    def _append_doc(doc: dict):
        if not isinstance(doc, dict):
            return
        if "kind" not in doc:
            return
        doc = sanitize_manifest(doc)
        if doc.get("kind") != "Namespace":
            doc = _ensure_namespace(doc, namespace)
        if secret_lookup:
            doc = _rewrite_secret_refs(doc, secret_lookup)
        doc = _rewrite_env_from(doc, secret_lookup, secret_docs_reference, inject_aws_credentials)
        if volume_usage:
            doc = _rewrite_volume_refs(doc, volume_usage)
        doc = _ensure_secret_credentials(doc, inject_aws_credentials)
        if doc.get("kind") == "Secret" and doc not in secret_docs_reference:
            secret_docs_reference.append(doc)
        metadata = doc.get("metadata", {})
        key = (doc.get("kind"), metadata.get("name"))
        if key in doc_keys:
            return
        doc_keys.add(key)
        normalized_docs.append(doc)

    if extra_docs:
        for extra in extra_docs:
            _append_doc(extra)

    for doc in parsed_docs:
        _append_doc(doc)

    if not normalized_docs:
        print("⚠️ No Kubernetes manifests generated after processing, skipping write.")
        return

    files_to_docs: dict[str, list[dict]] = {}
    for doc in normalized_docs:
        kind = doc.get("kind")
        filename_map = {
            "Namespace": "namespace.yaml",
            "Deployment": "deployment.yaml",
            "Service": "service.yaml",
            "ConfigMap": "configmap.yaml",
            "Secret": "secret.yaml",
            "PersistentVolume": "persistent-volume.yaml",
            "PersistentVolumeClaim": "persistent-volume-claim.yaml",
        }
        filename = filename_map.get(kind)
        if not filename:
            resource_name = _to_k8s_name(doc.get("metadata", {}).get("name", kind.lower()), default=kind.lower())
            filename = f"{kind.lower()}-{resource_name}.yaml"
        files_to_docs.setdefault(filename, []).append(doc)

    for filename, doc_list in files_to_docs.items():
        file_path = service_dir / filename
        with open(file_path, "w", encoding="utf-8") as fh:
            for idx, document in enumerate(doc_list):
                yaml.safe_dump(document, fh, sort_keys=False)
                if idx < len(doc_list) - 1:
                    fh.write("\n---\n")
        print(f"📝 Wrote {file_path}")

def main():
    parser = argparse.ArgumentParser(description="ECS Cluster → GKE Migration with Gemini")
    parser.add_argument("--cluster", help="ECS cluster name")
    parser.add_argument("--region", help="AWS region")
    parser.add_argument("--outdir", default="ecs", help="Base output folder (default: ecs/)")
    parser.add_argument("--namespace", help="Kubernetes namespace to use (default: auto-generate per service)")
    parser.add_argument(
        "--service",
        action="append",
        dest="services",
        help="Optional ECS service name to process. Can be repeated. Defaults to all services when run interactively or non-interactively.",
    )
    parser.add_argument(
        "--aws-credentials",
        choices=["auto", "yes", "no"],
        default="auto",
        help="Whether to inject AWS credential placeholders into manifests (default: auto, prompt when interactive).",
    )
    parser.add_argument(
        "--model",
        default=os.environ.get("GEMINI_MODEL", "gemini-1.5-flash"),
        help="Gemini model name to use (default: gemini-1.5-flash or value from GEMINI_MODEL env)",
    )
    parser.add_argument(
        "--fallback-model",
        action="append",
        dest="fallback_models",
        help="Optional fallback Gemini model name. Can be repeated."
             " Also honored: GEMINI_MODEL_FALLBACKS env (comma-separated).",
    )
    args = parser.parse_args()

    region_default = args.region or os.environ.get("AWS_REGION") or os.environ.get("AWS_DEFAULT_REGION")
    args.region = _prompt_for_value(region_default, "Enter AWS region: ")
    args.cluster = _prompt_for_cluster(args.cluster, args.region)
    namespace_input = (args.namespace or "").strip()
    user_namespace = _to_k8s_name(namespace_input, default=namespace_input or "default") if namespace_input else ""
    args.outdir = args.outdir or "ecs"

    fallback_models = list(args.fallback_models or [])
    env_fallbacks = os.environ.get("GEMINI_MODEL_FALLBACKS")
    if env_fallbacks:
        fallback_models.extend(
            [entry.strip() for entry in env_fallbacks.split(",") if entry.strip()]
        )

    default_fallbacks = [
        "gemini-1.5-flash-latest",
        "models/gemini-1.5-flash",
        "models/gemini-1.5-flash-latest",
        "gemini-1.5-pro",
        "models/gemini-1.5-pro",
        "gemini-1.0-pro",
        "models/gemini-1.0-pro",
        "gemini-pro",
        "models/gemini-pro",
    ]
    for default_candidate in default_fallbacks:
        if default_candidate not in fallback_models:
            fallback_models.append(default_candidate)

    gemini_client = configure_gemini(args.model, fallback_models)

    cluster_dir = Path(args.outdir) / args.cluster
    cluster_dir.mkdir(parents=True, exist_ok=True)

    print(f"🔍 Fetching ECS services from cluster {args.cluster}...")
    svc_list = subprocess.check_output(
        ["aws", "ecs", "list-services", "--cluster", args.cluster, "--region", args.region],
        text=True
    )
    service_arns = json.loads(svc_list).get("serviceArns", [])
    if not service_arns:
        print("No services found in cluster.")
        return

    service_names = [arn.split("/")[-1] for arn in service_arns]
    service_map = {name: arn for name, arn in zip(service_names, service_arns)}

    if args.services:
        requested = [entry.strip() for entry in args.services if entry and entry.strip()]
        selected_services = []
        for name in requested:
            if name in service_map and name not in selected_services:
                selected_services.append(name)
            elif name not in service_map:
                print(f"⚠️ Requested service '{name}' not found in cluster {args.cluster}.")
        if not selected_services:
            print("No matching services for provided --service arguments. Nothing to do.")
            return
    elif sys.stdin.isatty():
        selected_services = _prompt_for_services(service_names)
        if not selected_services:
            print("No services selected. Nothing to do.")
            return
    else:
        selected_services = service_names

    print(f"✅ Selected services: {', '.join(selected_services)}")

    aws_pref = args.aws_credentials

    for svc_name in selected_services:
        svc_arn = service_map[svc_name]
        print(f"➡️ Processing service: {svc_name}")

        svc_desc = subprocess.check_output(
            ["aws", "ecs", "describe-services", "--cluster", args.cluster, "--services", svc_name, "--region", args.region],
            text=True
        )
        svc_data_list = json.loads(svc_desc).get("services", [])
        if not svc_data_list:
            print(f"Service {svc_name} not found, skipping.")
            continue
        svc_data = svc_data_list[0]
        task_def_arn = svc_data.get("taskDefinition")
        if not task_def_arn:
            print(f"No task definition for service {svc_name}, skipping.")
            continue

        task_def_resp = subprocess.check_output(
            ["aws", "ecs", "describe-task-definition", "--task-definition", task_def_arn, "--region", args.region],
            text=True
        )
        task_def = json.loads(task_def_resp).get("taskDefinition")
        if not task_def:
            print(f"Task definition {task_def_arn} not found, skipping.")
            continue

        default_namespace = _to_k8s_name(args.cluster, svc_name, "ns", default=f"{svc_name}-ns")
        if user_namespace:
            namespace = user_namespace
        elif sys.stdin.isatty():
            prompt = f"Enter namespace for service {svc_name} (default: {default_namespace}): "
            namespace_choice = _prompt_for_value(None, prompt, allow_empty=True)
            namespace = (
                _to_k8s_name(namespace_choice, default=namespace_choice or default_namespace)
                if namespace_choice
                else default_namespace
            )
        else:
            namespace = default_namespace
        print(f"   • Using namespace: {namespace}")

        if aws_pref == "yes":
            inject_aws_credentials = True
        elif aws_pref == "no":
            inject_aws_credentials = False
        else:
            inject_aws_credentials = _prompt_yes_no(
                f"Add AWS credential placeholders for service {svc_name}?",
                default=True,
            ) if sys.stdin.isatty() else True

        env_docs, secret_lookup = build_env_manifests(svc_name, namespace, task_def, args.region)
        volume_docs, volume_usage = build_volume_manifests(svc_name, namespace, task_def)
        combined_docs = env_docs + volume_docs

        try:
            yaml_output = call_gemini(gemini_client, svc_name, task_def, svc_data, namespace)
        except RuntimeError as exc:
            print(f"❌ Skipping service {svc_name}: {exc}")
            continue
        print("RAW GEMINI OUTPUT:\n", yaml_output)
        service_dir = cluster_dir / svc_name  # <-- Define service_dir here
        service_dir.mkdir(parents=True, exist_ok=True)
        save_yaml_files(
            service_dir,
            yaml_output,
            namespace,
            extra_docs=combined_docs,
            secret_lookup=secret_lookup,
            volume_usage=volume_usage,
            inject_aws_credentials=inject_aws_credentials,
        )

    print(f"✅ Migration complete. All YAMLs stored under {cluster_dir}/")

if __name__ == "__main__":
    main()
