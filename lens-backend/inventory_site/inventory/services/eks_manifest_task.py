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

from inventory.forms import EksManifestForm
from inventory.services.task_registry import (
    GeneratedArtifact,
    TaskDefinition,
    TaskExecutionError,
    TaskExecutionResult,
    automation_registry,
)

REPO_ROOT = Path(__file__).resolve().parents[4]
SCRIPT_PATH = REPO_ROOT / "lens-backend" / "feature" / "eks2gke-manifest-local.py"


def _zip_namespace_collection(cluster_dir: Path, child_name: str) -> bytes | None:
    buffer = BytesIO()
    wrote_file = False
    with ZipFile(buffer, "w") as archive:
        for namespace_dir in cluster_dir.iterdir():
            if not namespace_dir.is_dir():
                continue
            child_dir = namespace_dir / child_name
            if not child_dir.exists():
                continue
            for file_path in child_dir.rglob("*"):
                if file_path.is_dir():
                    continue
                arcname = Path(namespace_dir.name) / file_path.relative_to(child_dir)
                archive.write(file_path, arcname=str(arcname))
                wrote_file = True
    if not wrote_file:
        return None
    buffer.seek(0)
    return buffer.getvalue()


def _run_manifest_cli(clean_data: dict, output_dir: Path) -> str:
    if not SCRIPT_PATH.exists():
        raise TaskExecutionError("Helper script feature/eks2gke-manifest-local.py not found.")

    cmd: List[str] = [
        sys.executable,
        str(SCRIPT_PATH),
        "--cluster",
        clean_data["cluster_name"],
        "--region",
        clean_data["aws_region"],
        "--outdir",
        str(output_dir),
    ]
    for namespace in clean_data.get("namespaces") or []:
        cmd.extend(["--namespace", namespace])
    if clean_data.get("resource_types"):
        cmd.extend(["--resources", clean_data["resource_types"]])
    env = os.environ.copy()
    env.update(
        {
            "AWS_ACCESS_KEY_ID": clean_data["access_key"],
            "AWS_SECRET_ACCESS_KEY": clean_data["secret_key"],
            "AWS_DEFAULT_REGION": clean_data["aws_region"],
            "AWS_REGION": clean_data["aws_region"],
            "AWS_PAGER": "",
            "PYTHONUNBUFFERED": "1",
        }
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
    if exit_code != 0:
        raise TaskExecutionError(f"eks2gke-manifest-local failed:\n{combined_logs.strip() or 'No additional output.'}")
    return combined_logs


def run_eks_manifest_task(clean_data: dict) -> TaskExecutionResult:
    output_root = Path(tempfile.mkdtemp(prefix="eks_manifest_"))
    log_text = ""
    try:
        log_text = _run_manifest_cli(clean_data, output_root)
        cluster_dir = output_root / clean_data["cluster_name"]
        if not cluster_dir.exists():
            directories = [item for item in output_root.iterdir() if item.is_dir()]
            if directories:
                cluster_dir = directories[0]
        if not cluster_dir.exists():
            raise TaskExecutionError("Helper script did not produce any manifests.")
        aws_zip = _zip_namespace_collection(cluster_dir, "eks-export")
        gke_zip = _zip_namespace_collection(cluster_dir, "gke")
    finally:
        shutil.rmtree(output_root, ignore_errors=True)

    artifacts = []
    if aws_zip:
        artifacts.append(
            GeneratedArtifact(
                filename=f"{cluster_dir.name}_aws_exports.zip",
                content=aws_zip,
                content_type="application/zip",
            )
        )
    if gke_zip:
        artifacts.append(
            GeneratedArtifact(
                filename=f"{cluster_dir.name}_gke_manifests.zip",
                content=gke_zip,
                content_type="application/zip",
            )
        )
    if not artifacts:
        raise TaskExecutionError("Manifest generator did not produce any namespace exports.")

    if log_text.strip():
        artifacts.append(
            GeneratedArtifact(
                filename=f"{cluster_dir.name}_eks_manifest.log",
                content=log_text.encode("utf-8"),
                content_type="text/plain",
            )
        )
    return TaskExecutionResult(artifacts)


automation_registry.register(
    TaskDefinition(
        task_id="eks_manifests",
        label="EKS → GKE Manifests (local)",
        description="Export live EKS workloads via kubectl and clean them for GKE without Gemini.",
        form_class=EksManifestForm,
        runner=run_eks_manifest_task,
    )
)
