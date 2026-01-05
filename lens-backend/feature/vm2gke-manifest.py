#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
VM to GKE Migration Script
Discovers VMs (EC2 instances or GCP Compute Engine VMs) and generates Kubernetes manifests
for GKE deployment using Gemini API.
"""
import os
import sys
import json

# Set UTF-8 encoding for stdout/stderr on Windows
if sys.platform == "win32":
    if hasattr(sys.stdout, 'reconfigure'):
        sys.stdout.reconfigure(encoding='utf-8')
    if hasattr(sys.stderr, 'reconfigure'):
        sys.stderr.reconfigure(encoding='utf-8')
import base64
import argparse
import subprocess
import re
import time
import platform
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


def _prompt_for_instances(instance_names: list[str], all_instances: list[str] = None) -> list[str]:
    if not instance_names:
        return []
    if not sys.stdin.isatty():
        return instance_names

    while True:
        print("\nAvailable VM instances:")
        for idx, name in enumerate(instance_names, start=1):
            print(f"  {idx}) {name}")
        if all_instances and len(all_instances) > len(instance_names):
            print(f"  (Note: {len(all_instances) - len(instance_names)} stopped instance(s) not shown)")
        print("Enter a comma-separated list of numbers or names, or press Enter for all")
        selection = input("Instance selection: ").strip()
        if not selection or selection.lower() == "all":
            return instance_names

        tokens = [token.strip() for token in selection.split(",") if token.strip()]
        if not tokens:
            print("No selection provided. Try again.")
            continue

        chosen: list[str] = []
        invalid = False
        for token in tokens:
            if token.isdigit():
                idx = int(token)
                if 1 <= idx <= len(instance_names):
                    chosen.append(instance_names[idx - 1])
                else:
                    print(f"Selection '{token}' is out of range.")
                    invalid = True
                    break
            else:
                if token in instance_names:
                    chosen.append(token)
                else:
                    print(f"Instance '{token}' not found in list.")
                    invalid = True
                    break
        if invalid:
            continue
        if not chosen:
            print("No valid instances selected. Try again.")
            continue

        seen = set()
        unique = []
        for name in chosen:
            if name not in seen:
                seen.add(name)
                unique.append(name)
        return unique


def _list_ec2_instances(region: str) -> list[dict]:
    """List EC2 instances in the specified region."""
    instances: list[dict] = []
    try:
        cmd = [
            "aws",
            "ec2",
            "describe-instances",
            "--region",
            region,
            "--output",
            "json",
        ]
        raw = subprocess.check_output(cmd, text=True)
        data = json.loads(raw) if raw else {}
        for reservation in data.get("Reservations", []):
            for instance in reservation.get("Instances", []):
                # Extract instance name from tags
                instance_name = None
                tags = instance.get("Tags", [])
                for tag in tags:
                    if tag.get("Key") == "Name":
                        instance_name = tag.get("Value")
                        break
                
                instance_id = instance.get("InstanceId", "")
                instances.append({
                    "instance_id": instance_id,
                    "name": instance_name or instance_id,
                    "instance_type": instance.get("InstanceType", ""),
                    "state": instance.get("State", {}).get("Name", ""),
                    "private_ip": instance.get("PrivateIpAddress", ""),
                    "public_ip": instance.get("PublicIpAddress", ""),
                    "subnet_id": instance.get("SubnetId", ""),
                    "security_groups": [sg.get("GroupName", "") for sg in instance.get("SecurityGroups", [])],
                    "tags": {tag.get("Key"): tag.get("Value") for tag in tags},
                    "raw": instance,
                })
    except subprocess.CalledProcessError as exc:
        print(f"[!] Failed to list EC2 instances in region '{region}': {exc}")
    except json.JSONDecodeError as exc:
        print(f"[!] Could not parse describe-instances response: {exc}")
    
    return instances


def _get_gcloud_command() -> str:
    """Get the correct gcloud command for the current platform."""
    return "gcloud.cmd" if platform.system() == "Windows" else "gcloud"


def _check_gcloud_available() -> bool:
    """Check if gcloud CLI is available in PATH."""
    gcloud_cmd = _get_gcloud_command()
    try:
        subprocess.run([gcloud_cmd, "--version"], capture_output=True, check=True, timeout=5)
        return True
    except (FileNotFoundError, subprocess.CalledProcessError, subprocess.TimeoutExpired):
        # Try without .cmd extension as fallback on Windows
        if platform.system() == "Windows" and gcloud_cmd == "gcloud.cmd":
            try:
                subprocess.run(["gcloud", "--version"], capture_output=True, check=True, timeout=5)
                return True
            except (FileNotFoundError, subprocess.CalledProcessError, subprocess.TimeoutExpired):
                pass
        return False


def _list_gcp_instances(project: str, zone: str = None) -> list[dict]:
    """List GCP Compute Engine instances."""
    instances: list[dict] = []
    
    # Check if gcloud is available
    if not _check_gcloud_available():
        print("[!] gcloud CLI not found in PATH.")
        print("   Please install Google Cloud SDK: https://cloud.google.com/sdk/docs/install")
        print("   Or ensure gcloud is in your system PATH.")
        return instances
    
    try:
        gcloud_cmd = _get_gcloud_command()
        cmd = [
            gcloud_cmd,
            "compute",
            "instances",
            "list",
            "--project",
            project,
            "--format",
            "json",
        ]
        if zone:
            # Check if it's a zone (contains a dash and letter at the end like us-central1-a)
            # or a region (just region name like us-central1)
            # Zone format: us-central1-a, us-west1-b, etc. (ends with -a, -b, -c, etc.)
            if "-" in zone and len(zone.split("-")) >= 3 and zone[-1].isalpha() and zone[-2] == "-":
                # It's a zone (e.g., us-central1-a)
                cmd.extend(["--zones", zone])
            else:
                # It's a region (e.g., us-central1), filter by zone pattern
                cmd.extend(["--filter", f"zone:{zone}*"])
        
        # Use GOOGLE_APPLICATION_CREDENTIALS if set
        env = os.environ.copy()
        creds_file = os.environ.get("GOOGLE_APPLICATION_CREDENTIALS")
        service_account_email = None
        if creds_file and os.path.exists(creds_file):
            # Read the service account email from the JSON file
            try:
                with open(creds_file, 'r') as f:
                    creds_data = json.load(f)
                    service_account_email = creds_data.get("client_email", "")
                    if service_account_email:
                        print(f"   [INFO]  Using service account: {service_account_email}")
            except Exception:
                pass
            
            # Activate the service account explicitly for gcloud
            try:
                activate_cmd = [
                    gcloud_cmd,
                    "auth",
                    "activate-service-account",
                    "--key-file",
                    creds_file,
                    "--quiet",
                ]
                result = subprocess.run(activate_cmd, capture_output=True, env=env, timeout=10)
                if result.returncode != 0 and result.stderr:
                    print(f"   [!]  Warning: Could not activate service account: {result.stderr.decode('utf-8', errors='ignore')[:200]}")
            except Exception as exc:
                pass  # Continue even if activation fails, gcloud might still work
        
        # Run command and capture both stdout and stderr
        result = subprocess.run(cmd, text=True, capture_output=True, env=env)
        
        # Check for errors in stderr
        if result.stderr:
            stderr_lower = result.stderr.lower()
            if "permission" in stderr_lower or "required" in stderr_lower:
                print(f"[!] Permission error: {result.stderr.strip()}")
                if service_account_email:
                    print(f"   Service account being used: {service_account_email}")
                    print(f"   Verify this service account has 'Owner' or 'Compute Viewer' role in project '{project}'.")
                print(f"   The service account needs 'compute.instances.list' permission.")
                return instances
            elif "error" in stderr_lower or "failed" in stderr_lower:
                print(f"[!] Error listing instances: {result.stderr.strip()}")
                # Continue to try parsing stdout anyway
        
        raw = result.stdout
        if not raw or raw.strip() == "[]":
            # Check if this is due to an error or actually no instances
            if result.stderr and ("permission" in result.stderr.lower() or "required" in result.stderr.lower()):
                return instances  # Already printed error above
            # Otherwise, might be empty list (no instances)
            return instances
        
        data = json.loads(raw) if raw else []
        for instance in data:
            instances.append({
                "instance_id": instance.get("id", ""),
                "name": instance.get("name", ""),
                "machine_type": instance.get("machineType", "").split("/")[-1] if instance.get("machineType") else "",
                "status": instance.get("status", ""),
                "zone": instance.get("zone", "").split("/")[-1] if instance.get("zone") else "",
                "network_interfaces": instance.get("networkInterfaces", []),
                "tags": instance.get("tags", {}).get("items", []),
                "raw": instance,
            })
    except FileNotFoundError:
        print("[!] gcloud command not found. Please install Google Cloud SDK.")
        print("   Download from: https://cloud.google.com/sdk/docs/install")
    except subprocess.CalledProcessError as exc:
        stderr = exc.stderr.decode("utf-8", errors="ignore") if exc.stderr else str(exc)
        stdout = exc.stdout.decode("utf-8", errors="ignore") if exc.stdout else ""
        print(f"[!] Failed to list GCP instances:")
        if stderr:
            print(f"   stderr: {stderr.strip()}")
        if stdout:
            print(f"   stdout: {stdout.strip()}")
        if "permission" in stderr.lower() or "required" in stderr.lower():
            print("   The service account needs 'compute.instances.list' permission.")
        elif "not found" in stderr.lower() or "does not exist" in stderr.lower():
            print("   Hint: Verify the project ID and your GCP authentication.")
    except json.JSONDecodeError as exc:
        print(f"[!] Could not parse gcloud instances list: {exc}")
        print(f"   Raw output: {raw[:200] if 'raw' in locals() else 'N/A'}")
    except Exception as exc:
        print(f"[!] Unexpected error listing GCP instances: {exc}")
        import traceback
        traceback.print_exc()
    
    return instances


def _discover_docker_containers_ec2(instance_id: str, region: str, selected_containers: list[str] = None) -> dict:
    """Discover Docker containers running on EC2 instance using SSM.
    
    Args:
        instance_id: EC2 instance ID
        region: AWS region
        selected_containers: Optional list of container names/IDs to include. If None, all containers are included.
    
    Returns:
        Dictionary with containers, images, env_vars, and error info
    """
    docker_info = {
        "containers": [],
        "images": [],
        "env_vars": {},
        "error": None,
    }
    
    try:
        # Check if Docker is installed and get running containers
        docker_ps_cmd = [
            "aws",
            "ssm",
            "send-command",
            "--instance-ids",
            instance_id,
            "--region",
            region,
            "--document-name",
            "AWS-RunShellScript",
            "--parameters",
            "commands=['docker ps --format \"{{.ID}}|{{.Image}}|{{.Names}}|{{.Status}}\"']",
            "--output",
            "json",
        ]
        
        result = subprocess.check_output(docker_ps_cmd, text=True, stderr=subprocess.DEVNULL)
        command_data = json.loads(result)
        command_id = command_data.get("Command", {}).get("CommandId", "")
        
        if not command_id:
            docker_info["error"] = "Could not send SSM command"
            return docker_info
        
        # Wait a moment for command to execute
        time.sleep(2)
        
        # Get command output
        output_cmd = [
            "aws",
            "ssm",
            "get-command-invocation",
            "--command-id",
            command_id,
            "--instance-id",
            instance_id,
            "--region",
            region,
            "--output",
            "json",
        ]
        
        output_result = subprocess.check_output(output_cmd, text=True, stderr=subprocess.DEVNULL)
        invocation = json.loads(output_result)
        
        status = invocation.get("Status", "")
        if status == "Success":
            stdout = invocation.get("StandardOutputContent", "").strip()
            if stdout:
                # Parse container list
                all_containers = []
                for line in stdout.splitlines():
                    if "|" in line:
                        parts = line.split("|")
                        if len(parts) >= 4:
                            all_containers.append({
                                "id": parts[0][:12],  # Short ID
                                "image": parts[1],
                                "name": parts[2],
                                "status": parts[3],
                            })
                
                # Filter containers if selection was provided
                if selected_containers:
                    # Match by name or ID
                    selected_set = set(selected_containers)
                    docker_info["containers"] = [
                        c for c in all_containers
                        if c["name"] in selected_set or c["id"] in selected_set or c["id"][:12] in selected_set
                    ]
                else:
                    docker_info["containers"] = all_containers
        else:
            docker_info["error"] = f"SSM command failed: {status}"
            
        # Get Docker images
        docker_images_cmd = [
            "aws",
            "ssm",
            "send-command",
            "--instance-ids",
            instance_id,
            "--region",
            region,
            "--document-name",
            "AWS-RunShellScript",
            "--parameters",
            "commands=['docker images --format \"{{.Repository}}:{{.Tag}}|{{.ID}}\"']",
            "--output",
            "json",
        ]
        
        result = subprocess.check_output(docker_images_cmd, text=True, stderr=subprocess.DEVNULL)
        command_data = json.loads(result)
        command_id = command_data.get("Command", {}).get("CommandId", "")
        
        if command_id:
            time.sleep(2)
            output_cmd[3] = command_id  # Update command ID
            output_result = subprocess.check_output(output_cmd, text=True, stderr=subprocess.DEVNULL)
            invocation = json.loads(output_result)
            
            if invocation.get("Status") == "Success":
                stdout = invocation.get("StandardOutputContent", "").strip()
                if stdout:
                    for line in stdout.splitlines():
                        if "|" in line:
                            parts = line.split("|")
                            if len(parts) >= 2:
                                docker_info["images"].append({
                                    "image": parts[0],
                                    "id": parts[1][:12],
                                })
        
        # Get detailed information (ports, env vars) from running containers
        for container in docker_info["containers"]:
            # Get environment variables
            env_cmd = [
                "aws",
                "ssm",
                "send-command",
                "--instance-ids",
                instance_id,
                "--region",
                region,
                "--document-name",
                "AWS-RunShellScript",
                "--parameters",
                f"commands=['docker inspect {container['id']} --format \"{{{{range .Config.Env}}}}{{{{.}}}}\\n{{{{end}}}}\"']",
                "--output",
                "json",
            ]
            
            # Get ports (exposed and published)
            ports_cmd = [
                "aws",
                "ssm",
                "send-command",
                "--instance-ids",
                instance_id,
                "--region",
                region,
                "--document-name",
                "AWS-RunShellScript",
                "--parameters",
                f"commands=['docker inspect {container['id']} --format \"{{{{json .}}}}\"']",
                "--output",
                "json",
            ]
            
            try:
                # Get env vars
                result = subprocess.check_output(env_cmd, text=True, stderr=subprocess.DEVNULL, timeout=10)
                command_data = json.loads(result)
                command_id = command_data.get("Command", {}).get("CommandId", "")
                
                if command_id:
                    time.sleep(2)
                    output_cmd[3] = command_id
                    output_result = subprocess.check_output(output_cmd, text=True, stderr=subprocess.DEVNULL)
                    invocation = json.loads(output_result)
                    
                    if invocation.get("Status") == "Success":
                        stdout = invocation.get("StandardOutputContent", "").strip()
                        if stdout:
                            env_vars = {}
                            for line in stdout.splitlines():
                                if "=" in line:
                                    key, value = line.split("=", 1)
                                    env_vars[key] = value
                            if env_vars:
                                docker_info["env_vars"][container["name"]] = env_vars
                
                # Get ports from full inspect
                result = subprocess.check_output(ports_cmd, text=True, stderr=subprocess.DEVNULL, timeout=10)
                command_data = json.loads(result)
                command_id = command_data.get("Command", {}).get("CommandId", "")
                
                if command_id:
                    time.sleep(2)
                    output_cmd[3] = command_id
                    output_result = subprocess.check_output(output_cmd, text=True, stderr=subprocess.DEVNULL)
                    invocation = json.loads(output_result)
                    
                    if invocation.get("Status") == "Success":
                        stdout = invocation.get("StandardOutputContent", "").strip()
                        if stdout:
                            try:
                                inspect_data = json.loads(stdout)
                                # Extract exposed ports
                                exposed_ports = []
                                config_ports = inspect_data.get("Config", {}).get("ExposedPorts", {})
                                if config_ports:
                                    for port_spec in config_ports.keys():
                                        # Format: "3000/tcp" or "3000"
                                        if "/" in port_spec:
                                            port, protocol = port_spec.split("/", 1)
                                        else:
                                            port, protocol = port_spec, "tcp"
                                        try:
                                            exposed_ports.append({"port": int(port), "protocol": protocol.upper()})
                                        except ValueError:
                                            pass
                                
                                # Extract published ports (host:container mappings)
                                published_ports = []
                                network_settings = inspect_data.get("NetworkSettings", {}).get("Ports", {})
                                if network_settings:
                                    for port_spec, bindings in network_settings.items():
                                        if "/" in port_spec:
                                            port, protocol = port_spec.split("/", 1)
                                        else:
                                            port, protocol = port_spec, "tcp"
                                        try:
                                            port_num = int(port)
                                            if bindings and isinstance(bindings, list) and len(bindings) > 0:
                                                host_port = bindings[0].get("HostPort")
                                                published_ports.append({
                                                    "container_port": port_num,
                                                    "host_port": int(host_port) if host_port else None,
                                                    "protocol": protocol.upper()
                                                })
                                            else:
                                                published_ports.append({
                                                    "container_port": port_num,
                                                    "host_port": None,
                                                    "protocol": protocol.upper()
                                                })
                                        except (ValueError, TypeError):
                                            pass
                                
                                # Use published ports if available, otherwise exposed ports
                                if published_ports:
                                    container["ports"] = published_ports
                                elif exposed_ports:
                                    container["ports"] = exposed_ports
                                else:
                                    container["ports"] = []
                            except json.JSONDecodeError:
                                pass
            except Exception:
                pass  # Skip if we can't get details for this container
                
    except subprocess.CalledProcessError as exc:
        # Check the error output to provide more specific feedback
        error_msg = str(exc)
        if "InvalidInstanceId" in error_msg or "not registered" in error_msg.lower():
            docker_info["error"] = "SSM agent not installed or instance not registered with SSM"
        elif "AccessDenied" in error_msg or "UnauthorizedOperation" in error_msg:
            docker_info["error"] = "Insufficient IAM permissions for SSM (requires ssm:SendCommand)"
        else:
            docker_info["error"] = f"SSM command failed: {error_msg}"
    except subprocess.TimeoutExpired:
        docker_info["error"] = "SSM command timed out"
    except json.JSONDecodeError:
        docker_info["error"] = "Could not parse SSM response"
    except Exception as exc:
        docker_info["error"] = f"Error discovering Docker: {str(exc)}"
    
    return docker_info


def _discover_docker_containers_gcp(instance_name: str, project: str, zone: str, selected_containers: list[str] = None) -> dict:
    """Discover Docker containers running on GCP instance using gcloud.
    
    Args:
        instance_name: Name of the GCP instance
        project: GCP project ID
        zone: GCP zone
        selected_containers: Optional list of container names/IDs to include. If None, all containers are included.
    
    Returns:
        Dictionary with containers, images, env_vars, and error info
    """
    docker_info = {
        "containers": [],
        "images": [],
        "env_vars": {},
        "error": None,
    }
    
    # Check if gcloud is available
    if not _check_gcloud_available():
        docker_info["error"] = "gcloud CLI not found in PATH"
        return docker_info
    
    try:
        gcloud_cmd = _get_gcloud_command()
        # Get running containers with sudo
        docker_ps_cmd = [
            gcloud_cmd,
            "compute",
            "ssh",
            instance_name,
            "--project",
            project,
            "--zone",
            zone,
            "--command",
            "sudo docker ps --format '{{.ID}}|{{.Image}}|{{.Names}}|{{.Status}}'",
            "--quiet",
        ]
        
        # Use GOOGLE_APPLICATION_CREDENTIALS if set
        env = os.environ.copy()
        creds_file = os.environ.get("GOOGLE_APPLICATION_CREDENTIALS")
        if creds_file and os.path.exists(creds_file):
            # Activate the service account explicitly for gcloud
            try:
                activate_cmd = [
                    gcloud_cmd,
                    "auth",
                    "activate-service-account",
                    "--key-file",
                    creds_file,
                    "--quiet",
                ]
                subprocess.run(activate_cmd, capture_output=True, env=env, timeout=10)
            except Exception:
                pass
        
        result = subprocess.run(docker_ps_cmd, text=True, capture_output=True, timeout=30, env=env)
        if result.returncode != 0:
            # Check if it's a permission error
            if result.stderr and ("permission denied" in result.stderr.lower() or "permission" in result.stderr.lower()):
                docker_info["error"] = "Permission denied accessing Docker (may need sudo or Docker group membership)"
            return docker_info
        
        all_containers = []
        if result.stdout and result.stdout.strip():
            for line in result.stdout.strip().splitlines():
                if "|" in line:
                    parts = line.split("|")
                    if len(parts) >= 4:
                        all_containers.append({
                            "id": parts[0][:12],
                            "image": parts[1],
                            "name": parts[2],
                            "status": parts[3],
                        })
        
        # Filter containers if selection was provided
        if selected_containers:
            # Match by name or ID
            selected_set = set(selected_containers)
            docker_info["containers"] = [
                c for c in all_containers
                if c["name"] in selected_set or c["id"] in selected_set or c["id"][:12] in selected_set
            ]
        else:
            docker_info["containers"] = all_containers
        
        # Get Docker images with sudo
        docker_images_cmd = [
            gcloud_cmd,
            "compute",
            "ssh",
            instance_name,
            "--project",
            project,
            "--zone",
            zone,
            "--command",
            "sudo docker images --format '{{.Repository}}:{{.Tag}}|{{.ID}}'",
            "--quiet",
        ]
        
        result = subprocess.run(docker_images_cmd, text=True, capture_output=True, timeout=30, env=env)
        if result.returncode == 0 and result.stdout and result.stdout.strip():
            for line in result.stdout.strip().splitlines():
                if "|" in line:
                    parts = line.split("|")
                    if len(parts) >= 2:
                        docker_info["images"].append({
                            "image": parts[0],
                            "id": parts[1][:12],
                        })
        
        # Get detailed information (ports, env vars) from selected containers only
        for container in docker_info["containers"]:
            # Get environment variables
            env_cmd = [
                gcloud_cmd,
                "compute",
                "ssh",
                instance_name,
                "--project",
                project,
                "--zone",
                zone,
                "--command",
                f"sudo docker inspect {container['id']} --format '{{{{range .Config.Env}}}}{{{{.}}}}\\n{{{{end}}}}'",
                "--quiet",
            ]
            
            # Get full container inspect for ports
            inspect_cmd = [
                gcloud_cmd,
                "compute",
                "ssh",
                instance_name,
                "--project",
                project,
                "--zone",
                zone,
                "--command",
                f"sudo docker inspect {container['id']} --format '{{{{json .}}}}'",
                "--quiet",
            ]
            
            try:
                # Get env vars
                result = subprocess.run(env_cmd, text=True, capture_output=True, timeout=30, env=env)
                if result.returncode == 0 and result.stdout and result.stdout.strip():
                    env_vars = {}
                    for line in result.stdout.strip().splitlines():
                        if "=" in line:
                            key, value = line.split("=", 1)
                            env_vars[key] = value
                    if env_vars:
                        docker_info["env_vars"][container["name"]] = env_vars
                
                # Get ports from full inspect
                result = subprocess.run(inspect_cmd, text=True, capture_output=True, timeout=30, env=env)
                if result.returncode == 0 and result.stdout and result.stdout.strip():
                    try:
                        inspect_data = json.loads(result.stdout.strip())
                        # Extract exposed ports
                        exposed_ports = []
                        config_ports = inspect_data.get("Config", {}).get("ExposedPorts", {})
                        if config_ports:
                            for port_spec in config_ports.keys():
                                # Format: "3000/tcp" or "3000"
                                if "/" in port_spec:
                                    port, protocol = port_spec.split("/", 1)
                                else:
                                    port, protocol = port_spec, "tcp"
                                try:
                                    exposed_ports.append({"port": int(port), "protocol": protocol.upper()})
                                except ValueError:
                                    pass
                        
                        # Extract published ports (host:container mappings)
                        published_ports = []
                        network_settings = inspect_data.get("NetworkSettings", {}).get("Ports", {})
                        if network_settings:
                            for port_spec, bindings in network_settings.items():
                                if "/" in port_spec:
                                    port, protocol = port_spec.split("/", 1)
                                else:
                                    port, protocol = port_spec, "tcp"
                                try:
                                    port_num = int(port)
                                    if bindings and isinstance(bindings, list) and len(bindings) > 0:
                                        host_port = bindings[0].get("HostPort")
                                        published_ports.append({
                                            "container_port": port_num,
                                            "host_port": int(host_port) if host_port else None,
                                            "protocol": protocol.upper()
                                        })
                                    else:
                                        published_ports.append({
                                            "container_port": port_num,
                                            "host_port": None,
                                            "protocol": protocol.upper()
                                        })
                                except (ValueError, TypeError):
                                    pass
                        
                        # Use published ports if available, otherwise exposed ports
                        if published_ports:
                            container["ports"] = published_ports
                        elif exposed_ports:
                            container["ports"] = exposed_ports
                        else:
                            container["ports"] = []
                    except json.JSONDecodeError:
                        pass
            except Exception:
                pass
                
    except FileNotFoundError:
        docker_info["error"] = "gcloud command not found"
    except subprocess.TimeoutExpired:
        docker_info["error"] = "SSH connection timeout"
    except subprocess.CalledProcessError as exc:
        stderr = exc.stderr.decode("utf-8", errors="ignore") if exc.stderr else str(exc)
        if "not found" in stderr.lower() or "does not exist" in stderr.lower():
            docker_info["error"] = "Instance not found or SSH access denied"
        else:
            docker_info["error"] = f"Could not SSH to instance or Docker not installed: {stderr}"
    except Exception as exc:
        docker_info["error"] = f"Error discovering Docker: {str(exc)}"
    
    return docker_info


def _get_instance_details_ec2(instance_id: str, region: str, skip_docker: bool = False) -> dict:
    """Get detailed information about an EC2 instance."""
    try:
        cmd = [
            "aws",
            "ec2",
            "describe-instances",
            "--instance-ids",
            instance_id,
            "--region",
            region,
            "--output",
            "json",
        ]
        raw = subprocess.check_output(cmd, text=True)
        data = json.loads(raw) if raw else {}
        reservations = data.get("Reservations", [])
        if not reservations:
            return {}
        instance = reservations[0].get("Instances", [{}])[0]
        
        # Get user data if available
        user_data = ""
        try:
            user_data_cmd = [
                "aws",
                "ec2",
                "describe-instance-attribute",
                "--instance-id",
                instance_id,
                "--attribute",
                "userData",
                "--region",
                region,
                "--output",
                "json",
            ]
            user_data_raw = subprocess.check_output(user_data_cmd, text=True)
            user_data_json = json.loads(user_data_raw) if user_data_raw else {}
            user_data_encoded = user_data_json.get("UserData", {}).get("Value", "")
            if user_data_encoded:
                user_data = base64.b64decode(user_data_encoded).decode("utf-8", errors="ignore")
        except Exception:
            pass
        
        # Get block device mappings
        block_devices = []
        for bdm in instance.get("BlockDeviceMappings", []):
            block_devices.append({
                "device_name": bdm.get("DeviceName", ""),
                "volume_id": bdm.get("Ebs", {}).get("VolumeId", ""),
            })
        
        # Discover Docker containers and environment variables
        docker_info = {
            "containers": [],
            "images": [],
            "env_vars": {},
            "error": None,
        }
        if not skip_docker and instance.get("State", {}).get("Name") == "running":
            print(f"   [*] Discovering Docker containers on {instance_id}...")
            docker_info = _discover_docker_containers_ec2(instance_id, region)
            if docker_info.get("error"):
                print(f"   [!] {docker_info['error']}")
                print(f"   [INFO]  Continuing without Docker discovery - will use VM metadata instead")
            elif docker_info.get("containers"):
                print(f"   [OK] Found {len(docker_info['containers'])} running container(s)")
                if docker_info.get("images"):
                    print(f"   [OK] Found {len(docker_info['images'])} Docker image(s)")
            else:
                print(f"   [INFO]  No Docker containers found (Docker may not be installed)")
        
        return {
            "instance": instance,
            "user_data": user_data,
            "block_devices": block_devices,
            "docker": docker_info,
        }
    except subprocess.CalledProcessError as exc:
        print(f"[!] Failed to get EC2 instance details: {exc}")
        return {}


def _show_docker_containers_for_instance(provider: str, instance_name: str, instance_id: str, region: str, project: str = None, zone: str = None, selected_containers: list[str] = None) -> dict:
    """Show Docker containers running on an instance and return Docker info."""
    print(f"\n[DOCKER] Discovering Docker containers on {instance_name}...")
    
    if provider == "aws":
        docker_info = _discover_docker_containers_ec2(instance_id, region, selected_containers)
    else:  # GCP
        docker_info = _discover_docker_containers_gcp(instance_name, project, zone, selected_containers)
    
    if docker_info.get("error"):
        print(f"   [!] {docker_info['error']}")
        print(f"   [INFO] Will use VM metadata instead for manifest generation")
        return docker_info
    
    containers = docker_info.get("containers", [])
    images = docker_info.get("images", [])
    
    if containers:
        print(f"\n   [OK] Found {len(containers)} running container(s):")
        for idx, container in enumerate(containers, start=1):
            print(f"      {idx}) {container.get('name', 'unnamed')}")
            print(f"         Image: {container.get('image', 'unknown')}")
            print(f"         Status: {container.get('status', 'unknown')}")
        
        # Show environment variables if available
        env_vars = docker_info.get("env_vars", {})
        if env_vars:
            print(f"\n   [ENV] Environment Variables:")
            for container_name, envs in env_vars.items():
                print(f"      {container_name}:")
                for key, value in list(envs.items())[:5]:  # Show first 5
                    # Truncate long values
                    display_value = value if len(str(value)) <= 50 else str(value)[:47] + "..."
                    print(f"         {key}={display_value}")
                if len(envs) > 5:
                    print(f"         ... and {len(envs) - 5} more")
    else:
        print(f"   [INFO] No Docker containers found (Docker may not be installed or no containers running)")
    
    if images:
        print(f"\n   [IMAGES] Found {len(images)} Docker image(s) on instance")
        for idx, image in enumerate(images[:5], start=1):  # Show first 5
            print(f"      {idx}) {image.get('image', 'unknown')}")
        if len(images) > 5:
            print(f"      ... and {len(images) - 5} more")
    
    return docker_info


def _get_instance_details_gcp(instance_name: str, project: str, zone: str, skip_docker: bool = False) -> dict:
    """Get detailed information about a GCP instance."""
    # Check if gcloud is available
    if not _check_gcloud_available():
        print("[!] gcloud CLI not found. Cannot get instance details.")
        return {}
    
    try:
        gcloud_cmd = _get_gcloud_command()
        cmd = [
            gcloud_cmd,
            "compute",
            "instances",
            "describe",
            instance_name,
            "--project",
            project,
            "--zone",
            zone,
            "--format",
            "json",
        ]
        
        # Use GOOGLE_APPLICATION_CREDENTIALS if set
        env = os.environ.copy()
        creds_file = os.environ.get("GOOGLE_APPLICATION_CREDENTIALS")
        if creds_file and os.path.exists(creds_file):
            # Activate the service account explicitly for gcloud
            try:
                activate_cmd = [
                    gcloud_cmd,
                    "auth",
                    "activate-service-account",
                    "--key-file",
                    creds_file,
                    "--quiet",
                ]
                subprocess.run(activate_cmd, capture_output=True, env=env, timeout=10)
            except Exception:
                pass
        
        result = subprocess.run(cmd, text=True, capture_output=True, env=env)
        if result.returncode != 0:
            stderr = result.stderr.strip() if result.stderr else ""
            raise subprocess.CalledProcessError(result.returncode, cmd, result.stdout, result.stderr)
        raw = result.stdout
        instance = json.loads(raw) if raw else {}
        
        # Extract metadata
        metadata = {}
        for item in instance.get("metadata", {}).get("items", []):
            metadata[item.get("key", "")] = item.get("value", "")
        
        # Discover Docker containers and environment variables
        docker_info = {
            "containers": [],
            "images": [],
            "env_vars": {},
            "error": None,
        }
        if not skip_docker and instance.get("status") == "RUNNING":
            print(f"   [*] Discovering Docker containers on {instance_name}...")
            # First, discover all containers (without selection)
            all_containers_info = _discover_docker_containers_gcp(instance_name, project, zone)
            if all_containers_info.get("error"):
                print(f"   [!] {all_containers_info['error']}")
                print(f"   [INFO]  Continuing without Docker discovery - will use VM metadata instead")
                docker_info = all_containers_info
            elif all_containers_info.get("containers"):
                # Show containers and let user select which ones to include
                selected_container_names = _prompt_for_docker_containers(all_containers_info["containers"])
                if selected_container_names:
                    # Re-discover with only selected containers
                    docker_info = _discover_docker_containers_gcp(instance_name, project, zone, selected_container_names)
                    print(f"   [OK] Selected {len(docker_info['containers'])} container(s) for migration")
                    if docker_info.get("images"):
                        print(f"   [OK] Found {len(docker_info['images'])} Docker image(s)")
                else:
                    print(f"   [INFO]  No containers selected - will use VM metadata instead")
                    docker_info = all_containers_info
            else:
                print(f"   [INFO]  No Docker containers found (Docker may not be installed)")
        
        return {
            "instance": instance,
            "metadata": metadata,
            "docker": docker_info,
        }
    except FileNotFoundError:
        print("[!] gcloud command not found. Please install Google Cloud SDK.")
        print("   Download from: https://cloud.google.com/sdk/docs/install")
        return {}
    except subprocess.CalledProcessError as exc:
        stderr = exc.stderr.decode("utf-8", errors="ignore") if exc.stderr else str(exc)
        print(f"[!] Failed to get GCP instance details: {stderr}")
        return {}
    except json.JSONDecodeError as exc:
        print(f"[!] Could not parse gcloud instance details: {exc}")
        return {}
    except json.JSONDecodeError as exc:
        print(f"[!] Could not parse gcloud instance details: {exc}")
        return {}


def _describe_instance_type(instance_type: str, region: str) -> dict:
    """Get CPU and memory information for an EC2 instance type."""
    try:
        cmd = [
            "aws",
            "ec2",
            "describe-instance-types",
            "--instance-types",
            instance_type,
            "--region",
            region,
            "--query",
            "InstanceTypes[0].{Vcpu:VCpuInfo.DefaultVCpus,Memory:MemoryInfo.SizeInMiB}",
            "--output",
            "json",
        ]
        raw = subprocess.check_output(cmd, text=True)
        data = json.loads(raw) if raw else {}
        return {
            "cpu_vcpu": float(data.get("Vcpu", 0)),
            "memory_mb": int(data.get("Memory", 0)),
        }
    except Exception:
        return {"cpu_vcpu": 0.0, "memory_mb": 0}


# Configure Gemini API
def configure_gemini(model_name: str, fallback_models=None):
    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        raise EnvironmentError(
            "GEMINI_API_KEY environment variable not set. Export the key, e.g.\n"
            "export GEMINI_API_KEY=\"<your-api-key>\"\n"
            "You can get an API key from: https://makersuite.google.com/app/apikey"
        )
    api_key = api_key.strip()
    if not api_key:
        raise EnvironmentError(
            "GEMINI_API_KEY environment variable is empty. Please set a valid API key.\n"
            "You can get an API key from: https://makersuite.google.com/app/apikey"
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
            base = name[len("models/"):]
            add_alias(base)

        for alias in list(aliases):
            if alias.endswith("-latest"):
                base = alias[:-len("-latest")]
                add_alias(base)
                if not base.startswith("models/"):
                    add_alias(f"models/{base}")
            else:
                add_alias(f"{alias}-latest")
                if alias.startswith("models/"):
                    base = alias[len("models/"):]
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
            base = name[len("models/"):]
            add_alias(base)

        for alias in list(aliases):
            if alias.endswith("-latest"):
                base = alias[:-len("-latest")]
                add_alias(base)
                if not base.startswith("models/"):
                    add_alias(f"models/{base}")
            else:
                add_alias(f"{alias}-latest")
                if alias.startswith("models/"):
                    base = alias[len("models/"):]
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
            setattr(model, "_vm2gke_model_name", model_name)
            self._models[model_name] = model
            return model, None
        except gapi_exceptions.GoogleAPICallError as exc:
            code_name = getattr(getattr(exc, "code", None), "name", "") or exc.__class__.__name__
            return None, (code_name, str(exc), exc)
        except Exception as exc:
            code_name = exc.__class__.__name__
            return None, (code_name, str(exc), exc)

    def generate(self, prompt: str, instance_name: str) -> str:
        errors = []
        for idx, model_name in enumerate(self._model_order):
            model, init_error = self._ensure_model(model_name)
            if model is None:
                code, message, exc_obj = init_error
                errors.append((model_name, code, message))
                if code == "NOT_FOUND":
                    continue
                raise RuntimeError(
                    f"Failed to initialize Gemini model '{model_name}' while processing instance '{instance_name}'."
                    f" Original error: {message}"
                ) from exc_obj

            try:
                response = model.generate_content(prompt)
                if idx > 0:
                    print(
                        f"[INFO] Instance {instance_name}: using fallback Gemini model '{model_name}'"
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
                    f"Gemini API call failed for instance '{instance_name}' using model '{model_name}'."
                    f" Original error: {exc}"
                ) from exc
            except Exception as exc:
                code_name = exc.__class__.__name__
                errors.append((model_name, code_name, str(exc)))
                raise RuntimeError(
                    f"Unexpected error during Gemini call for instance '{instance_name}' using model '{model_name}': {exc}"
                ) from exc

        error_details = "; ".join(
            f"{name} ({code}): {message}" for name, code, message in errors
        ) or "<no additional error context>"
        raise RuntimeError(
            f"Gemini API call failed for instance '{instance_name}' after trying models"
            f" {', '.join(self._model_order)}. Errors: {error_details}"
        )


def call_gemini(client: GeminiModelClient, instance_name: str, vm_data: dict, namespace: str, provider: str):
    """Call Gemini API to convert VM configuration into Kubernetes manifests."""
    # Build Docker information summary
    docker_summary = ""
    docker_containers = vm_data.get("docker_containers", [])
    # Check if docker info exists and has an error
    docker_data = vm_data.get("docker", {})
    if isinstance(docker_data, dict):
        docker_error = docker_data.get("error")
    else:
        docker_error = None
    
    if docker_containers:
        docker_summary = "\n\nDocker Containers Found:\n"
        for container in docker_containers:
            container_name = container.get('name', 'unnamed')
            docker_summary += f"- {container_name}: {container.get('image', 'unknown')} ({container.get('status', 'unknown')})\n"
            
            # Add ports information
            ports = container.get("ports", [])
            if ports:
                port_list = []
                for p in ports:
                    if isinstance(p, dict):
                        if "container_port" in p:
                            port_str = f"{p['container_port']}/{p.get('protocol', 'TCP').lower()}"
                            if p.get("host_port"):
                                port_str += f" (host: {p['host_port']})"
                        else:
                            port_str = f"{p.get('port', 'unknown')}/{p.get('protocol', 'TCP').lower()}"
                        port_list.append(port_str)
                    else:
                        port_list.append(str(p))
                docker_summary += f"  Ports: {', '.join(port_list)}\n"
            
            # Add environment variables if available
            env_vars = vm_data.get("docker_env_vars", {}).get(container_name, {})
            if env_vars:
                docker_summary += f"  Environment Variables: {', '.join(list(env_vars.keys())[:10])}"
                if len(env_vars) > 10:
                    docker_summary += f" ... and {len(env_vars) - 10} more"
                docker_summary += "\n"
    elif docker_error:
        docker_summary = f"\n\nNote: Could not discover Docker containers ({docker_error}). "
        docker_summary += "Will infer container configuration from VM metadata, user data, instance tags, and instance name.\n"
    
    prompt = f"""
    You are an expert in VM to GKE migration.
    Convert the following {provider} VM instance configuration into Kubernetes manifests.
    
    CRITICAL REQUIREMENTS:
    1. Use the EXACT ports discovered from Docker containers. If a container exposes port 3000, use port 3000 in the Deployment and Service, NOT port 80.
    2. Use the EXACT Docker images found on the VM - do not change or infer different images.
    3. Use ALL environment variables discovered from Docker containers - include them in ConfigMaps (non-sensitive) or Secrets (sensitive).
    4. For each Docker container, create a separate Deployment with the exact container configuration.
    5. Service ports must match the container ports exactly (e.g., if container uses 3000, Service must use 3000).
    6. Include proper health checks (livenessProbe, readinessProbe) based on the discovered ports.
    
    Manifest Requirements:
    - Deployment (replicas: 1, can be adjusted based on workload requirements)
    - Service (use type LoadBalancer if the VM has a public IP, otherwise use ClusterIP)
    - ConfigMap for non-sensitive environment variables
    - Secret for sensitive environment variables (passwords, keys, tokens, etc.)
    - All objects (except Namespace) must be scoped to namespace '{namespace}'.
    - Do not generate PersistentVolume or PersistentVolumeClaim resources unless explicitly needed.
    - Do NOT generate NetworkPolicy resources.
    - Output must be valid YAML, separated by '---'.
    - Use the exact ports, images, and environment variables from the Docker container discovery.
    - If the VM runs a database, consider using StatefulSet instead of Deployment.
    - For each Docker container found, create a separate Deployment with matching Service.
    
    {provider} VM Instance Data:
    {json.dumps(vm_data, indent=2)}
    {docker_summary}
    
    IMPORTANT: Pay special attention to the ports field in each container. Use those exact port numbers in your Kubernetes manifests.
    """
    try:
        return client.generate(prompt, instance_name)
    except RuntimeError as exc:
        hint = " Hint: verify the model name with --model, or set GEMINI_MODEL/GEMINI_MODEL_FALLBACKS." if "NOT_FOUND" in str(exc) else ""
        raise RuntimeError(str(exc) + hint) from exc


def _is_secret_value(value: str) -> bool:
    """Check if a value looks like a secret (password, key, token, etc.)."""
    secret_keywords = ["password", "secret", "key", "token", "credential", "auth"]
    value_lower = value.lower()
    return any(keyword in value_lower for keyword in secret_keywords) or len(value) > 50


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
    """Remove AWS/GCP identifying metadata like ARNs from manifest dictionaries."""
    disallowed_key_markers = ("aws.arn", "aws-arn", "aws_arn", "gcp.selfLink", "gcp-selfLink")
    disallowed_value_markers = ("arn:aws", "https://www.googleapis.com/compute")

    def _sanitize(obj):
        if isinstance(obj, dict):
            cleaned = {}
            for key, value in obj.items():
                if isinstance(key, str) and any(marker in key for marker in disallowed_key_markers):
                    continue
                sanitized_value = _sanitize(value)
                if isinstance(sanitized_value, str) and any(marker in sanitized_value for marker in disallowed_value_markers):
                    continue
                cleaned[key] = sanitized_value
            return cleaned
        if isinstance(obj, list):
            return [sanitized for sanitized in (_sanitize(item) for item in obj)
                    if not (isinstance(sanitized, str) and any(marker in sanitized for marker in disallowed_value_markers))]
        return obj

    return _sanitize(data)


def save_yaml_files(
    instance_dir: Path,
    yaml_output: str,
    namespace: str,
    extra_docs: list[dict] | None = None,
):
    cleaned_output = clean_yaml_output(yaml_output)

    parsed_docs: list[dict] = []
    if cleaned_output:
        try:
            parsed_docs = list(yaml.safe_load_all(cleaned_output))
        except yaml.YAMLError as exc:
            print(f"[!] Failed to parse YAML: {exc}")
    else:
        print("[INFO] No YAML content detected from Gemini after cleaning.")

    doc_keys = set()
    normalized_docs: list[dict] = []

    def _append_doc(doc: dict):
        if not isinstance(doc, dict):
            return
        if "kind" not in doc:
            return
        # Skip NetworkPolicy resources
        if doc.get("kind") == "NetworkPolicy":
            return
        doc = sanitize_manifest(doc)
        if doc.get("kind") != "Namespace":
            doc = _ensure_namespace(doc, namespace)
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
        print("[!] No Kubernetes manifests generated after processing, skipping write.")
        return

    files_to_docs: dict[str, list[dict]] = {}
    for doc in normalized_docs:
        kind = doc.get("kind")
        filename_map = {
            "Namespace": "namespace.yaml",
            "Deployment": "deployment.yaml",
            "StatefulSet": "statefulset.yaml",
            "Service": "service.yaml",
            "ConfigMap": "configmap.yaml",
            "Secret": "secret.yaml",
            "NetworkPolicy": "networkpolicy.yaml",
            "PersistentVolume": "persistent-volume.yaml",
            "PersistentVolumeClaim": "persistent-volume-claim.yaml",
        }
        filename = filename_map.get(kind)
        if not filename:
            resource_name = _to_k8s_name(doc.get("metadata", {}).get("name", kind.lower()), default=kind.lower())
            filename = f"{kind.lower()}-{resource_name}.yaml"
        files_to_docs.setdefault(filename, []).append(doc)

    for filename, doc_list in files_to_docs.items():
        file_path = instance_dir / filename
        with open(file_path, "w", encoding="utf-8") as fh:
            for idx, document in enumerate(doc_list):
                yaml.safe_dump(document, fh, sort_keys=False)
                if idx < len(doc_list) - 1:
                    fh.write("\n---\n")
        print(f"[NOTE] Wrote {file_path}")


def build_namespace_manifest(namespace: str) -> dict:
    """Create a Namespace manifest."""
    return {
        "apiVersion": "v1",
        "kind": "Namespace",
        "metadata": {
            "name": namespace,
        },
    }


def _prompt_for_provider(initial: str | None) -> str:
    """Prompt user to select cloud provider."""
    if initial and initial.lower() in ["aws", "gcp"]:
        return initial.lower()
    if not sys.stdin.isatty():
        raise RuntimeError("Provider required when running non-interactively. Use --provider aws or --provider gcp")
    
    while True:
        print("Select cloud provider:")
        print("  1) AWS (EC2)")
        print("  2) GCP (Compute Engine)")
        choice = input("Choose an option [1-2]: ").strip() or "1"
        if choice == "1":
            return "aws"
        elif choice == "2":
            return "gcp"
        else:
            print("Invalid option. Choose 1 or 2.")


def _prompt_for_aws_credentials() -> dict:
    """Prompt user for AWS credentials."""
    credentials = {}
    
    # Check environment variables first
    aws_access_key = os.environ.get("AWS_ACCESS_KEY_ID")
    aws_secret_key = os.environ.get("AWS_SECRET_ACCESS_KEY")
    
    if aws_access_key and aws_secret_key:
        print("[INFO]  Using AWS credentials from environment variables")
        credentials["access_key"] = aws_access_key
        credentials["secret_key"] = aws_secret_key
        return credentials
    
    if not sys.stdin.isatty():
        raise RuntimeError("AWS credentials required. Set AWS_ACCESS_KEY_ID and AWS_SECRET_ACCESS_KEY environment variables.")
    
    print("\n[NOTE] AWS Credentials")
    print("   (Leave blank to use environment variables or AWS CLI default profile)")
    access_key = input("AWS Access Key ID: ").strip()
    secret_key = input("AWS Secret Access Key: ").strip()
    
    if access_key and secret_key:
        credentials["access_key"] = access_key
        credentials["secret_key"] = secret_key
        # Set environment variables for subsequent AWS CLI calls
        os.environ["AWS_ACCESS_KEY_ID"] = access_key
        os.environ["AWS_SECRET_ACCESS_KEY"] = secret_key
    elif not access_key and not secret_key:
        print("   [INFO]  Using environment variables or AWS CLI default profile")
    else:
        raise RuntimeError("Both access key and secret key are required.")
    
    return credentials


def _prompt_for_gcp_credentials() -> dict:
    """Prompt user for GCP service account credentials."""
    credentials = {}
    
    # Check for GOOGLE_APPLICATION_CREDENTIALS first
    gcp_creds_file = os.environ.get("GOOGLE_APPLICATION_CREDENTIALS")
    if gcp_creds_file and os.path.exists(gcp_creds_file):
        print(f"[INFO]  Using GCP service account from: {gcp_creds_file}")
        try:
            with open(gcp_creds_file, 'r') as f:
                creds_data = json.load(f)
                service_account_email = creds_data.get("client_email", "")
                if service_account_email:
                    print(f"   Service account: {service_account_email}")
        except Exception:
            pass
        credentials["service_key_file"] = gcp_creds_file
        return credentials
    
    if not sys.stdin.isatty():
        raise RuntimeError("GCP credentials required. Set GOOGLE_APPLICATION_CREDENTIALS environment variable or provide service account JSON.")
    
    print("\n[NOTE] GCP Service Account Credentials")
    print("   Option 1: Path to service account JSON file")
    print("   Option 2: Paste service account JSON content")
    print("   Option 3: Paste base64-encoded service account JSON")
    print("   (Leave blank to use GOOGLE_APPLICATION_CREDENTIALS environment variable)")
    
    choice = input("Choose option [1-3] or press Enter to use env var: ").strip()
    
    if not choice:
        if gcp_creds_file:
            credentials["service_key_file"] = gcp_creds_file
            return credentials
        raise RuntimeError("No GCP credentials found. Set GOOGLE_APPLICATION_CREDENTIALS or provide credentials.")
    
    if choice == "1":
        file_path = input("Enter path to service account JSON file: ").strip()
        if file_path and os.path.exists(file_path):
            credentials["service_key_file"] = file_path
            os.environ["GOOGLE_APPLICATION_CREDENTIALS"] = file_path
        else:
            raise RuntimeError(f"File not found: {file_path}")
    elif choice == "2":
        print("Paste service account JSON (press Enter twice when done):")
        lines = []
        while True:
            line = input()
            if not line and lines:
                break
            lines.append(line)
        json_content = "\n".join(lines)
        try:
            json.loads(json_content)  # Validate JSON
            # Write to temporary file
            import tempfile
            with tempfile.NamedTemporaryFile(mode='w', suffix='.json', delete=False) as f:
                f.write(json_content)
                credentials["service_key_file"] = f.name
                os.environ["GOOGLE_APPLICATION_CREDENTIALS"] = f.name
        except json.JSONDecodeError as e:
            raise RuntimeError(f"Invalid JSON: {e}")
    elif choice == "3":
        b64_content = input("Paste base64-encoded service account JSON: ").strip()
        try:
            decoded = base64.b64decode(b64_content).decode("utf-8")
            json.loads(decoded)  # Validate JSON
            # Write to temporary file
            import tempfile
            with tempfile.NamedTemporaryFile(mode='w', suffix='.json', delete=False) as f:
                f.write(decoded)
                credentials["service_key_file"] = f.name
                os.environ["GOOGLE_APPLICATION_CREDENTIALS"] = f.name
        except Exception as e:
            raise RuntimeError(f"Invalid base64 or JSON: {e}")
    else:
        raise RuntimeError("Invalid option selected.")
    
    return credentials


def _list_aws_regions() -> list[str]:
    """List available AWS regions."""
    try:
        cmd = [
            "aws",
            "ec2",
            "describe-regions",
            "--all-regions",
            "--output",
            "json",
        ]
        raw = subprocess.check_output(cmd, text=True)
        data = json.loads(raw) if raw else {}
        regions = [entry.get("RegionName") for entry in data.get("Regions", []) if entry.get("RegionName")]
        if regions:
            return sorted(set(regions))
    except Exception:
        pass
    # Fallback to common regions
    return ["us-east-1", "us-west-2", "eu-west-1", "ap-south-1", "eu-central-1"]


def _prompt_for_aws_region(initial: str | None) -> str:
    """Prompt user to select AWS region."""
    if initial:
        return initial
    if not sys.stdin.isatty():
        region_default = os.environ.get("AWS_REGION") or os.environ.get("AWS_DEFAULT_REGION")
        if region_default:
            return region_default
        raise RuntimeError("AWS region required when running non-interactively.")
    
    while True:
        regions = _list_aws_regions()
        if regions:
            print("Available AWS regions:")
            for idx, name in enumerate(regions, start=1):
                print(f"  {idx}) {name}")
            print("  m) Enter region manually")
            selection = input("Select region by number or enter name: ").strip()
            if not selection:
                continue
            if selection.isdigit():
                idx = int(selection)
                if 1 <= idx <= len(regions):
                    return regions[idx - 1]
                print("Invalid selection number.")
            elif selection.lower() == "m":
                manual = input("Enter AWS region: ").strip()
                if manual:
                    return manual
            elif selection:
                return selection
        else:
            manual = input("Enter AWS region: ").strip()
            if manual:
                return manual
        print("Please try again.")


def _list_gcp_projects() -> list[dict]:
    """List available GCP projects."""
    projects = []
    
    if not _check_gcloud_available():
        return projects
    
    try:
        gcloud_cmd = _get_gcloud_command()
        cmd = [
            gcloud_cmd,
            "projects",
            "list",
            "--format",
            "json",
        ]
        
        # Use GOOGLE_APPLICATION_CREDENTIALS if set
        env = os.environ.copy()
        creds_file = os.environ.get("GOOGLE_APPLICATION_CREDENTIALS")
        if creds_file and os.path.exists(creds_file):
            # Activate the service account explicitly for gcloud
            try:
                activate_cmd = [
                    gcloud_cmd,
                    "auth",
                    "activate-service-account",
                    "--key-file",
                    creds_file,
                    "--quiet",
                ]
                subprocess.run(activate_cmd, capture_output=True, env=env, timeout=10)
            except Exception:
                pass
        
        result = subprocess.run(cmd, text=True, capture_output=True, env=env, timeout=30)
        if result.returncode == 0 and result.stdout:
            data = json.loads(result.stdout) if result.stdout.strip() else []
            for project in data:
                projects.append({
                    "project_id": project.get("projectId", ""),
                    "name": project.get("name", ""),
                })
    except Exception:
        pass
    
    return projects


def _prompt_for_gcp_project(initial: str | None) -> str:
    """Prompt user to select GCP project."""
    if initial:
        return initial
    if not sys.stdin.isatty():
        return _prompt_for_value(None, "Enter GCP project ID: ")
    
    projects = _list_gcp_projects()
    if projects:
        print("Available GCP projects:")
        for idx, proj in enumerate(projects, start=1):
            name = proj.get("name", "")
            project_id = proj.get("project_id", "")
            display = f"{project_id}" + (f" ({name})" if name else "")
            print(f"  {idx}) {display}")
        print("  m) Enter project ID manually")
        
        while True:
            selection = input("Select project by number or enter ID: ").strip()
            if not selection:
                continue
            if selection.isdigit():
                idx = int(selection)
                if 1 <= idx <= len(projects):
                    return projects[idx - 1]["project_id"]
                print("Invalid selection number.")
            elif selection.lower() == "m":
                manual = input("Enter GCP project ID: ").strip()
                if manual:
                    return manual
            elif selection:
                return selection
            print("Please try again.")
    else:
        return _prompt_for_value(None, "Enter GCP project ID: ")


def _prompt_for_gcp_region(initial: str | None) -> str:
    """Prompt user to enter GCP zone or region."""
    if initial:
        return initial
    if not sys.stdin.isatty():
        return _prompt_for_value(None, "Enter GCP zone (e.g., us-central1-a) or region (e.g., us-central1): ", allow_empty=True) or ""
    
    return _prompt_for_value(None, "Enter GCP zone (e.g., us-central1-a) or region (e.g., us-central1), or leave blank for all zones: ", allow_empty=True) or ""


def _prompt_for_docker_containers(containers: list[dict]) -> list[str]:
    """Prompt user to select which Docker containers to include."""
    if not containers:
        return []
    if not sys.stdin.isatty():
        return [c["name"] for c in containers]
    
    print("\n[IMAGES] Found Docker containers:")
    for idx, container in enumerate(containers, start=1):
        print(f"  {idx}) {container.get('name', 'unnamed')} - {container.get('image', 'unknown')} ({container.get('status', 'unknown')})")
    print("Enter a comma-separated list of numbers or names, or press Enter for all")
    
    while True:
        selection = input("Container selection: ").strip()
        if not selection or selection.lower() == "all":
            return [c["name"] for c in containers]
        
        tokens = [token.strip() for token in selection.split(",") if token.strip()]
        if not tokens:
            print("No selection provided. Try again.")
            continue
        
        chosen = []
        invalid = False
        for token in tokens:
            if token.isdigit():
                idx = int(token)
                if 1 <= idx <= len(containers):
                    chosen.append(containers[idx - 1]["name"])
                else:
                    print(f"Selection '{token}' is out of range.")
                    invalid = True
                    break
            else:
                # Try to match by name or ID
                matched = False
                for container in containers:
                    if container["name"] == token or container["id"] == token or container["id"][:12] == token:
                        chosen.append(container["name"])
                        matched = True
                        break
                if not matched:
                    print(f"Container '{token}' not found in list.")
                    invalid = True
                    break
        
        if invalid:
            continue
        if not chosen:
            print("No valid containers selected. Try again.")
            continue
        
        # Remove duplicates while preserving order
        seen = set()
        unique = []
        for name in chosen:
            if name not in seen:
                seen.add(name)
                unique.append(name)
        return unique


def main():
    parser = argparse.ArgumentParser(description="VM Instance -> GKE Migration with Gemini")
    parser.add_argument("--provider", choices=["aws", "gcp"], help="Cloud provider (aws or gcp)")
    parser.add_argument("--region", help="AWS region or GCP zone")
    parser.add_argument("--project", help="GCP project ID (required for GCP)")
    parser.add_argument("--instance", help="Instance ID or name (optional, will prompt if not provided)")
    parser.add_argument("--outdir", default="vm", help="Base output folder (default: vm/)")
    parser.add_argument("--namespace", help="Kubernetes namespace to use (default: auto-generate per instance)")
    parser.add_argument(
        "--container",
        action="append",
        dest="selected_containers",
        help="Docker container name/ID to include. Can be repeated. If not provided, all containers are included.",
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

    # Step 1: Prompt for provider
    print("=" * 60)
    print("VM -> GKE Migration Tool")
    print("=" * 60)
    provider = _prompt_for_provider(args.provider)
    print(f"[OK] Selected provider: {provider.upper()}\n")

    # Step 2: Prompt for credentials
    print("=" * 60)
    print("Step 2: Credentials")
    print("=" * 60)
    if provider == "aws":
        aws_creds = _prompt_for_aws_credentials()
        print("[OK] AWS credentials configured\n")
    else:  # GCP
        gcp_creds = _prompt_for_gcp_credentials()
        print("[OK] GCP credentials configured\n")
        
        # Prompt for project selection (after credentials are set up)
        if not args.project:
            args.project = _prompt_for_gcp_project(None)
        print(f"[OK] Selected GCP project: {args.project}\n")

    # Step 3: Prompt for region
    print("=" * 60)
    print("Step 3: Region Selection")
    print("=" * 60)
    if provider == "aws":
        region = _prompt_for_aws_region(args.region)
        print(f"[OK] Selected AWS region: {region}\n")
    else:  # GCP
        region = _prompt_for_gcp_region(args.region)
        if region:
            print(f"[OK] Selected GCP zone/region: {region}\n")
        else:
            print("[OK] Will list instances from all zones\n")

    # Step 4: Discover and list all instances
    print("=" * 60)
    print("Step 4: Instance Selection")
    print("=" * 60)
    print(f"[*] Discovering {provider.upper()} instances in {region or 'all zones'}...")
    if provider == "aws":
        instances = _list_ec2_instances(region)
        instance_map = {inst["name"]: inst for inst in instances}
        # List all instances (running and stopped)
        all_instance_names = [inst["name"] for inst in instances]
        running_instance_names = [inst["name"] for inst in instances if inst["state"] == "running"]
    else:  # GCP
        instances = _list_gcp_instances(args.project, region)
        instance_map = {inst["name"]: inst for inst in instances}
        # List all instances (running and stopped)
        all_instance_names = [inst["name"] for inst in instances]
        running_instance_names = [inst["name"] for inst in instances if inst["status"] == "RUNNING"]

    if not all_instance_names:
        print(f"[!] No instances found in {provider.upper()}.")
        return

    # Display all instances with their status
    print(f"\n[INFO] Found {len(all_instance_names)} instance(s):")
    for idx, inst_name in enumerate(all_instance_names, start=1):
        inst_info = instance_map[inst_name]
        if provider == "aws":
            state = inst_info.get("state", "unknown")
            inst_type = inst_info.get("instance_type", "unknown")
            status_icon = "[RUNNING]" if state == "running" else "[STOPPED]"
            print(f"  {idx}) {status_icon} {inst_name} ({inst_type}) - {state}")
        else:  # GCP
            status = inst_info.get("status", "unknown")
            machine_type = inst_info.get("machine_type", "unknown")
            status_icon = "[RUNNING]" if status == "RUNNING" else "[STOPPED]"
            print(f"  {idx}) {status_icon} {inst_name} ({machine_type}) - {status}")
    
    # Use running instances for selection by default, but allow all
    instance_names = running_instance_names if running_instance_names else all_instance_names

    # Select instances
    if args.instance:
        selected_instances = [args.instance] if args.instance in instance_map else []
        if not selected_instances:
            print(f"[!] Instance '{args.instance}' not found.")
            return
    elif sys.stdin.isatty():
        print(f"\n[TIP] Showing {len(instance_names)} running instance(s) for selection.")
        selected_instances = _prompt_for_instances(instance_names, all_instance_names)
        if not selected_instances:
            print("No instances selected. Nothing to do.")
            return
    else:
        selected_instances = instance_names

    print(f"\n[OK] Selected {len(selected_instances)} instance(s): {', '.join(selected_instances)}\n")

    # Step 5: Show Docker containers for each selected instance
    print("=" * 60)
    print("Step 5: Docker Container Discovery")
    print("=" * 60)
    
    instance_docker_info = {}
    for instance_name in selected_instances:
        instance_info = instance_map[instance_name]
        instance_id = instance_info["instance_id"]
        
        # Check if instance is running
        is_running = False
        if provider == "aws":
            is_running = instance_info.get("state") == "running"
            zone = None
        else:  # GCP
            is_running = instance_info.get("status") == "RUNNING"
            zone = instance_info.get("zone", region or "")
        
        if is_running:
            docker_info = _show_docker_containers_for_instance(
                provider, instance_name, instance_id, region, args.project, zone, args.selected_containers
            )
            instance_docker_info[instance_name] = docker_info
        else:
            print(f"\n[!] Instance {instance_name} is not running. Skipping Docker discovery.")
            instance_docker_info[instance_name] = {
                "containers": [],
                "images": [],
                "env_vars": {},
                "error": "Instance is not running"
            }
    
    # Step 6: Prompt to generate manifests
    print("\n" + "=" * 60)
    print("Step 6: Generate Kubernetes Manifests")
    print("=" * 60)
    
    if not sys.stdin.isatty() or os.environ.get("VM2GKE_AUTO_APPROVE"):
        should_generate = True
    else:
        should_generate = _prompt_yes_no("Generate Kubernetes manifests for selected instances?", default=True)
    
    if not should_generate:
        print("[X] Manifest generation cancelled.")
        return
    
    # Initialize Gemini client
    args.outdir = args.outdir or "vm"
    namespace_input = (args.namespace or "").strip()
    user_namespace = _to_k8s_name(namespace_input, default=namespace_input or "default") if namespace_input else ""

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

    base_dir = Path(args.outdir)
    base_dir.mkdir(parents=True, exist_ok=True)

    # Step 7: Generate manifests
    print("\n[GENERATING] Generating Kubernetes manifests...\n")
    for instance_name in selected_instances:
        instance_info = instance_map[instance_name]
        print(f"-> Processing instance: {instance_name}")

        # Get detailed instance information (skip Docker discovery since we already have it)
        if provider == "aws":
            instance_id = instance_info["instance_id"]
            details = _get_instance_details_ec2(instance_id, region, skip_docker=True)
            if not details:
                print(f"[!] Could not get details for instance {instance_name}, skipping.")
                continue
            
            instance_data = details["instance"]
            instance_type = instance_data.get("InstanceType", "")
            type_info = _describe_instance_type(instance_type, region)
            
            # Use Docker info we already discovered
            docker_info = instance_docker_info.get(instance_name, details.get("docker", {}))
            
            vm_data = {
                "provider": "aws",
                "instance_id": instance_id,
                "name": instance_name,
                "instance_type": instance_type,
                "cpu_vcpu": type_info.get("cpu_vcpu", 0),
                "memory_mb": type_info.get("memory_mb", 0),
                "private_ip": instance_data.get("PrivateIpAddress", ""),
                "public_ip": instance_data.get("PublicIpAddress", ""),
                "subnet_id": instance_data.get("SubnetId", ""),
                "security_groups": instance_info["security_groups"],
                "tags": instance_info["tags"],
                "user_data": details.get("user_data", ""),
                "block_devices": details.get("block_devices", []),
                "docker_containers": docker_info.get("containers", []),
                "docker_images": docker_info.get("images", []),
                "docker_env_vars": docker_info.get("env_vars", {}),
                "raw_instance": instance_data,
            }
        else:  # GCP
            instance_id = instance_info["instance_id"]
            zone = instance_info.get("zone", region or "")
            details = _get_instance_details_gcp(instance_name, args.project, zone, skip_docker=True)
            if not details:
                print(f"[!] Could not get details for instance {instance_name}, skipping.")
                continue
            
            instance_data = details["instance"]
            machine_type = instance_data.get("machineType", "").split("/")[-1] if instance_data.get("machineType") else ""
            
            # Use Docker info we already discovered
            docker_info = instance_docker_info.get(instance_name, details.get("docker", {}))
            
            vm_data = {
                "provider": "gcp",
                "instance_id": instance_id,
                "name": instance_name,
                "machine_type": machine_type,
                "zone": zone,
                "status": instance_data.get("status", ""),
                "network_interfaces": instance_data.get("networkInterfaces", []),
                "tags": instance_data.get("tags", {}).get("items", []),
                "metadata": details.get("metadata", {}),
                "docker_containers": docker_info.get("containers", []),
                "docker_images": docker_info.get("images", []),
                "docker_env_vars": docker_info.get("env_vars", {}),
                "raw_instance": instance_data,
            }

        default_namespace = _to_k8s_name(instance_name, "ns", default=f"{instance_name}-ns")
        if user_namespace:
            namespace = user_namespace
        elif sys.stdin.isatty():
            prompt = f"Enter namespace for instance {instance_name} (default: {default_namespace}): "
            namespace_choice = _prompt_for_value(None, prompt, allow_empty=True)
            namespace = (
                _to_k8s_name(namespace_choice, default=namespace_choice or default_namespace)
                if namespace_choice
                else default_namespace
            )
        else:
            namespace = default_namespace
        print(f"   • Using namespace: {namespace}")

        try:
            yaml_output = call_gemini(gemini_client, instance_name, vm_data, namespace, provider)
        except RuntimeError as exc:
            print(f"[X] Skipping instance {instance_name}: {exc}")
            continue

        instance_dir = base_dir / instance_name
        instance_dir.mkdir(parents=True, exist_ok=True)
        
        namespace_doc = build_namespace_manifest(namespace)
        save_yaml_files(
            instance_dir,
            yaml_output,
            namespace,
            extra_docs=[namespace_doc],
        )

    print(f"\n[OK] Migration complete. All YAMLs stored under {base_dir}/")


if __name__ == "__main__":
    main()

