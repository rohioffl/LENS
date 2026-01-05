from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
from io import BytesIO
from pathlib import Path
from typing import List
from zipfile import ZipFile

from inventory.forms import Vm2GkeManifestForm
from inventory.services.task_registry import (
    GeneratedArtifact,
    TaskDefinition,
    TaskExecutionError,
    TaskExecutionResult,
    automation_registry,
)

REPO_ROOT = Path(__file__).resolve().parents[4]
SCRIPT_PATH = REPO_ROOT / "lens-backend" / "feature" / "vm2gke-manifest.py"


def _split_csv(raw: str | List[str] | None) -> List[str]:
    if not raw:
        return []
    if isinstance(raw, list):
        items = raw
    else:
        items = raw.split(",")
    return [item.strip() for item in items if str(item).strip()]


def _zip_directory(path: Path) -> bytes:
    buffer = BytesIO()
    with ZipFile(buffer, "w") as archive:
        for file_path in path.rglob("*"):
            if file_path.is_dir():
                continue
            archive.write(file_path, arcname=str(file_path.relative_to(path)))
    buffer.seek(0)
    return buffer.getvalue()


def _run_manifest_cli(clean_data: dict, output_dir: Path) -> str:
    if not SCRIPT_PATH.exists():
        raise TaskExecutionError("Helper script feature/vm2gke-manifest.py not found.")
    
    provider = clean_data.get("provider", "aws").lower()
    # Gemini-related parameters removed - AI functionality no longer used

    cmd = [
        sys.executable,
        str(SCRIPT_PATH),
        "--provider",
        provider,
        "--outdir",
        str(output_dir),
    ]
    
    if provider == "aws":
        cmd.extend(["--region", clean_data["aws_region"]])
        if clean_data.get("instance"):
            cmd.extend(["--instance", clean_data["instance"]])
    else:  # GCP
        cmd.extend(["--project", clean_data["gcp_project"]])
        if clean_data.get("gcp_region"):
            cmd.extend(["--region", clean_data["gcp_region"]])
        if clean_data.get("instance"):
            cmd.extend(["--instance", clean_data["instance"]])
    
    namespace = clean_data.get("namespace")
    if namespace:
        cmd.extend(["--namespace", namespace])
    
    selected_containers = clean_data.get("selected_containers")
    if selected_containers:
        if isinstance(selected_containers, list):
            for container in selected_containers:
                cmd.extend(["--container", container])
        elif isinstance(selected_containers, str):
            cmd.extend(["--container", selected_containers])
    
    # Gemini model arguments removed - AI functionality no longer used

    env = os.environ.copy()
    env.update(
        {
            "PYTHONUNBUFFERED": "1",
            "VM2GKE_AUTO_APPROVE": "1",
        }
    )
    
    if provider == "aws":
        env.update(
            {
                "AWS_ACCESS_KEY_ID": clean_data["access_key"],
                "AWS_SECRET_ACCESS_KEY": clean_data["secret_key"],
                "AWS_DEFAULT_REGION": clean_data["aws_region"],
                "AWS_REGION": clean_data["aws_region"],
                "AWS_PAGER": "",
            }
        )
    else:  # GCP
        # Handle GCP service account key
        gcp_service_key = clean_data.get("gcp_service_key", "").strip()
        if gcp_service_key:
            # Try to decode if base64, otherwise use as-is
            try:
                decoded = gcp_service_key
                try:
                    import base64
                    decoded = base64.b64decode(gcp_service_key).decode("utf-8")
                except Exception:
                    pass
                # Write to temporary file
                with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
                    f.write(decoded)
                    env["GOOGLE_APPLICATION_CREDENTIALS"] = f.name
            except Exception as exc:
                raise TaskExecutionError(f"Failed to process GCP service account key: {exc}")
    
    # Gemini API key no longer required - AI functionality removed

    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        cwd=str(SCRIPT_PATH.parent),
        stdin=subprocess.DEVNULL,
        env=env,
    )
    logs: List[str] = []
    assert proc.stdout is not None
    for line in proc.stdout:
        logs.append(line)
        print(line.rstrip())
    exit_code = proc.wait()
    combined_logs = "".join(logs)
    
    # Clean up temporary GCP credentials file if created
    if provider == "gcp" and "GOOGLE_APPLICATION_CREDENTIALS" in env:
        creds_file = env["GOOGLE_APPLICATION_CREDENTIALS"]
        if creds_file and os.path.exists(creds_file) and creds_file.startswith(tempfile.gettempdir()):
            try:
                os.unlink(creds_file)
            except Exception:
                pass
    
    if exit_code != 0:
        raise TaskExecutionError(f"vm2gke-manifest failed:\n{combined_logs.strip() or 'No additional output.'}")
    return combined_logs


def run_vm2gke_manifest_task(clean_data: dict) -> TaskExecutionResult:
    output_root = Path(tempfile.mkdtemp(prefix="vm2gke_manifest_"))
    log_text = ""
    try:
        log_text = _run_manifest_cli(clean_data, output_root)
        
        # Check if any YAML files were generated
        yaml_files = list(output_root.rglob("*.yaml")) + list(output_root.rglob("*.yml"))
        if not yaml_files:
            raise TaskExecutionError("Manifest generator did not produce any Kubernetes YAMLs.")
        
        # Find instance directories (directories containing YAML files)
        instance_dirs = []
        for item in output_root.iterdir():
            if item.is_dir():
                # Check if this directory contains YAML files
                if any(item.rglob("*.yaml")) or any(item.rglob("*.yml")):
                    instance_dirs.append(item)
        
        # Create zip file with all generated manifests
        # If there's only one instance directory, zip just that directory
        # Otherwise, zip the entire output structure
        if len(instance_dirs) == 1:
            instance_dir = instance_dirs[0]
            archive_bytes = _zip_directory(instance_dir)
            archive_name = f"{instance_dir.name}_manifests.zip"
        else:
            # Zip the entire output directory structure
            archive_bytes = _zip_directory(output_root)
            archive_name = "vm2gke_manifests.zip"
    finally:
        shutil.rmtree(output_root, ignore_errors=True)

    artifacts = [
        GeneratedArtifact(
            filename=archive_name,
            content=archive_bytes,
            content_type="application/zip",
        )
    ]
    if log_text.strip():
        artifacts.append(
            GeneratedArtifact(
                filename="vm2gke_generation.log",
                content=log_text.encode("utf-8"),
                content_type="text/plain",
            )
        )
    return TaskExecutionResult(artifacts)


automation_registry.register(
    TaskDefinition(
        task_id="vm2gke_manifests",
        label="VM → GKE Manifests",
        description="Generate Kubernetes manifests for VM instances (EC2 or GCP Compute Engine) based on discovered Docker containers.",
        form_class=Vm2GkeManifestForm,
        runner=run_vm2gke_manifest_task,
    )
)

