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
    gemini_model = clean_data.get("gemini_model") or os.environ.get("VM2GKE_MANIFEST_GEMINI_MODEL")
    gemini_fallbacks = clean_data.get("gemini_fallbacks") or os.environ.get("VM2GKE_MANIFEST_GEMINI_FALLBACKS")
    # Match ECS pattern: prioritize form data, then override env var, then general env var from .env
    gemini_api_key = clean_data.get("gemini_api_key") or os.environ.get("VM2GKE_MANIFEST_GEMINI_API_KEY_OVERRIDE")

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
    
    if gemini_model:
        cmd.extend(["--model", gemini_model])
    for fb in _split_csv(gemini_fallbacks):
        cmd.extend(["--fallback-model", fb])

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
    
    # Match ECS pattern: check form/override first, then fall back to GEMINI_API_KEY from .env
    gemini_key = gemini_api_key or env.get("GEMINI_API_KEY")
    if gemini_key:
        gemini_key = gemini_key.strip()
        if gemini_key:
            env["GEMINI_API_KEY"] = gemini_key
        else:
            raise TaskExecutionError(
                "GEMINI_API_KEY is empty or invalid. Please set a valid GEMINI_API_KEY in .env file "
                "(located at lens-backend/.env) or provide it in the form. The API key should be a non-empty string."
            )
    elif not env.get("GEMINI_API_KEY"):
        # Check if .env file exists and provide helpful error message
        from pathlib import Path
        env_path = Path(__file__).resolve().parents[3] / ".env"
        env_hint = ""
        if env_path.exists():
            env_hint = f" The .env file exists at {env_path}, but GEMINI_API_KEY is not set in it."
        else:
            env_hint = f" The .env file should be located at {env_path.parent / '.env'}."
        raise TaskExecutionError(
            f"GEMINI_API_KEY is not configured.{env_hint} "
            "Add 'GEMINI_API_KEY=your-api-key' to your .env file or provide it in the form. "
            "You can get an API key from: https://makersuite.google.com/app/apikey"
        )

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
        description="Generate Kubernetes manifests for VM instances (EC2 or GCP Compute Engine) with Gemini assistance.",
        form_class=Vm2GkeManifestForm,
        runner=run_vm2gke_manifest_task,
    )
)

