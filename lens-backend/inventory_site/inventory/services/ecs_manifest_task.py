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

from inventory.forms import EcsManifestForm
from inventory.services.task_registry import (
    GeneratedArtifact,
    TaskDefinition,
    TaskExecutionError,
    TaskExecutionResult,
    automation_registry,
)

REPO_ROOT = Path(__file__).resolve().parents[4]
SCRIPT_PATH = REPO_ROOT / "lens-backend" / "feature" / "ecs2gke-manifest.py"


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
        raise TaskExecutionError("Helper script feature/ecs2gke-manifest.py not found.")
    aws_mode = (
        clean_data.get("aws_credentials_mode")
        or os.environ.get("ECS_MANIFEST_AWS_CREDENTIAL_MODE")
        or "yes"
    )
    aws_mode = aws_mode.strip().lower()
    if aws_mode not in {"auto", "yes", "no"}:
        aws_mode = "yes"
    gemini_model = clean_data.get("gemini_model") or os.environ.get("ECS_MANIFEST_GEMINI_MODEL")
    gemini_fallbacks = clean_data.get("gemini_fallbacks") or os.environ.get("ECS_MANIFEST_GEMINI_FALLBACKS")
    gemini_api_key = clean_data.get("gemini_api_key") or os.environ.get("ECS_MANIFEST_GEMINI_API_KEY_OVERRIDE")

    cmd = [
        sys.executable,
        str(SCRIPT_PATH),
        "--cluster",
        clean_data["cluster_name"],
        "--region",
        clean_data["aws_region"],
        "--outdir",
        str(output_dir),
        "--aws-credentials",
        aws_mode,
    ]
    if gemini_model:
        cmd.extend(["--model", gemini_model])
    namespace = clean_data.get("namespace")
    if namespace:
        cmd.extend(["--namespace", namespace])
    for svc in clean_data.get("services") or []:
        cmd.extend(["--service", svc])
    for fb in _split_csv(gemini_fallbacks):
        cmd.extend(["--fallback-model", fb])

    env = os.environ.copy()
    env.update(
        {
            "AWS_ACCESS_KEY_ID": clean_data["access_key"],
            "AWS_SECRET_ACCESS_KEY": clean_data["secret_key"],
            "AWS_DEFAULT_REGION": clean_data["aws_region"],
            "AWS_REGION": clean_data["aws_region"],
            "AWS_PAGER": "",
            "PYTHONUNBUFFERED": "1",
            "ECS2GKE_AUTO_APPROVE": "1",
        }
    )
    gemini_key = gemini_api_key or env.get("GEMINI_API_KEY")
    if gemini_key:
        env["GEMINI_API_KEY"] = gemini_key
    elif not env.get("GEMINI_API_KEY"):
        raise TaskExecutionError("GEMINI_API_KEY is not configured. Provide it in the form or set it on the server.")

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
    if exit_code != 0:
        raise TaskExecutionError(f"ecs2gke-manifest failed:\n{combined_logs.strip() or 'No additional output.'}")
    return combined_logs


def run_ecs_manifest_task(clean_data: dict) -> TaskExecutionResult:
    output_root = Path(tempfile.mkdtemp(prefix="ecs_manifest_"))
    log_text = ""
    try:
        log_text = _run_manifest_cli(clean_data, output_root)
        cluster_dir = output_root / clean_data["cluster_name"]
        if not cluster_dir.exists():
            directories = [item for item in output_root.iterdir() if item.is_dir()]
            if directories:
                cluster_dir = directories[0]
        if not cluster_dir.exists():
            raise TaskExecutionError("Manifest generator did not produce any Kubernetes YAMLs.")
        archive_bytes = _zip_directory(cluster_dir)
    finally:
        shutil.rmtree(output_root, ignore_errors=True)

    artifacts = [
        GeneratedArtifact(
            filename=f"{cluster_dir.name}_manifests.zip",
            content=archive_bytes,
            content_type="application/zip",
        )
    ]
    if log_text.strip():
        artifacts.append(
            GeneratedArtifact(
                filename=f"{cluster_dir.name}_generation.log",
                content=log_text.encode("utf-8"),
                content_type="text/plain",
            )
        )
    return TaskExecutionResult(artifacts)


automation_registry.register(
    TaskDefinition(
        task_id="ecs_manifests",
        label="ECS → GKE Manifests",
        description="Generate Kubernetes manifests for ECS services with Gemini assistance.",
        form_class=EcsManifestForm,
        runner=run_ecs_manifest_task,
    )
)
