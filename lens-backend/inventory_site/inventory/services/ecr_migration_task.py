from __future__ import annotations

import os
import subprocess
import tempfile
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO, TextIOBase
import sys
from pathlib import Path

from inventory.forms import EcrMigrationForm
from inventory.services.task_registry import (
    GeneratedArtifact,
    TaskDefinition,
    TaskExecutionError,
    TaskExecutionResult,
    automation_registry,
)

# Ensure the repository root (where ecr2artifact.py lives) is importable.
REPO_ROOT = Path(__file__).resolve().parents[4]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import ecr2artifact as migrator


class _StreamTee(TextIOBase):
    """Duplicate writes to the current stdout/stderr and an in-memory buffer."""

    def __init__(self, target, buffer: StringIO):
        super().__init__()
        self._target = target
        self._buffer = buffer

    def write(self, data):
        if not data:
            return 0
        written = self._target.write(data)
        if hasattr(self._target, "flush"):
            self._target.flush()
        self._buffer.write(data)
        return written

    def flush(self):
        if hasattr(self._target, "flush"):
            self._target.flush()
        self._buffer.flush()


def _activate_service_account(service_key: str, project: str) -> tuple[str, str]:
    """Write service key to disk, activate it with gcloud, and return (path, logs)."""
    tmp = tempfile.NamedTemporaryFile("w", delete=False, suffix=".json")
    tmp.write(service_key)
    tmp.flush()
    tmp.close()
    cmd = [
        "gcloud",
        "auth",
        "activate-service-account",
        f"--key-file={tmp.name}",
        f"--project={project}",
    ]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, check=True)
    except FileNotFoundError as exc:
        os.unlink(tmp.name)
        raise TaskExecutionError("gcloud CLI not found. Install Google Cloud SDK on the server.") from exc
    except subprocess.CalledProcessError as exc:
        os.unlink(tmp.name)
        output = (exc.stdout or "") + (exc.stderr or "")
        raise TaskExecutionError(f"Failed to activate GCP service account:\n{output.strip()}") from exc
    logs = (proc.stdout or "") + (proc.stderr or "")
    return tmp.name, logs


def _run_ecr_migration(clean_data: dict) -> TaskExecutionResult:
    service_key = clean_data.get("gcp_service_key")
    key_path = None
    prev_gac = os.environ.get("GOOGLE_APPLICATION_CREDENTIALS")
    prev_project = os.environ.get("CLOUDSDK_CORE_PROJECT")
    log_buffer = StringIO()
    stdout_tee = _StreamTee(sys.stdout, log_buffer)
    stderr_tee = _StreamTee(sys.stderr, log_buffer)
    try:
        migrator.configure_boto3_session(
            access_key=clean_data.get("access_key"),
            secret_key=clean_data.get("secret_key"),
            session_token=None,
            profile_name=None,
        )
        migrator.ensure_cli_tool("docker", "Install Docker and ensure the 'docker' CLI is available.")
        migrator.ensure_cli_tool("gcloud", "Install the Google Cloud CLI and ensure 'gcloud' is available.")
        activation_logs = ""
        if service_key:
            key_path, activation_logs = _activate_service_account(service_key, clean_data["gcp_project"])
            os.environ["GOOGLE_APPLICATION_CREDENTIALS"] = key_path
            os.environ["CLOUDSDK_CORE_PROJECT"] = clean_data["gcp_project"]
        repo_names = clean_data.get("aws_repos") or None
        planned = "all repositories" if not repo_names else f"{len(repo_names)} selected repositories"
        with redirect_stdout(stdout_tee), redirect_stderr(stderr_tee):
            if activation_logs.strip():
                print(activation_logs.strip())
            print(f"Starting ECR → Artifact Registry migration for {planned} in {clean_data['aws_region']} -> {clean_data['gcp_region']}...")
            migrator.migrate_all(
                clean_data["aws_region"],
                clean_data["gcp_project"],
                clean_data["gcp_region"],
                True,  # yes flag to skip prompt
                clean_data.get("workers") or 4,
                clean_data.get("workers") or 4,
                repo_names,
            )
            print("Migration complete. Review the logs for repository-level status.")
    except migrator.CommandExecutionError as exc:
        raise TaskExecutionError(str(exc))
    except SystemExit as exc:  # handles sys.exit inside helper functions
        raise TaskExecutionError(str(exc))
    except Exception as exc:  # safety net
        raise TaskExecutionError(f"Unexpected error during ECR migration: {exc}") from exc
    finally:
        if key_path and os.path.exists(key_path):
            os.unlink(key_path)
        if prev_gac is None:
            os.environ.pop("GOOGLE_APPLICATION_CREDENTIALS", None)
        else:
            os.environ["GOOGLE_APPLICATION_CREDENTIALS"] = prev_gac
        if prev_project is None:
            os.environ.pop("CLOUDSDK_CORE_PROJECT", None)
        else:
            os.environ["CLOUDSDK_CORE_PROJECT"] = prev_project

    logs = log_buffer.getvalue().strip() or "Migration completed with no additional output."

    artifact = GeneratedArtifact(
        filename="ecr_migration_log.txt",
        content=logs.encode("utf-8"),
        content_type="text/plain",
    )
    return TaskExecutionResult([artifact], archive_name=None)


automation_registry.register(
    TaskDefinition(
        task_id="ecr_migration",
        label="ECR -> Artifact Registry",
        description="Migrate all ECR repos to GCP Artifact Registry (preserve repo names).",
        form_class=EcrMigrationForm,
        runner=_run_ecr_migration,
    )
)
