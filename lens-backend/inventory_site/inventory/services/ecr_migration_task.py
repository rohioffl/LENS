from __future__ import annotations

from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
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


def _capture_output(func, *args, **kwargs) -> str:
    buffer = StringIO()
    with redirect_stdout(buffer), redirect_stderr(buffer):
        func(*args, **kwargs)
    return buffer.getvalue()


def _run_ecr_migration(clean_data: dict) -> TaskExecutionResult:
    try:
        migrator.configure_boto3_session(
            access_key=clean_data.get("access_key"),
            secret_key=clean_data.get("secret_key"),
            session_token=None,
            profile_name=None,
        )
        migrator.ensure_cli_tool("docker", "Install Docker and ensure the 'docker' CLI is available.")
        migrator.ensure_cli_tool("gcloud", "Install the Google Cloud CLI and ensure 'gcloud' is available.")
        repo_names = clean_data.get("aws_repos") or None
        logs = _capture_output(
            migrator.migrate_all,
            clean_data["aws_region"],
            clean_data["gcp_project"],
            clean_data["gcp_region"],
            True,  # yes flag to skip prompt
            clean_data.get("workers") or 4,
            clean_data.get("workers") or 4,
            repo_names,
        )
    except migrator.CommandExecutionError as exc:
        raise TaskExecutionError(str(exc))
    except SystemExit as exc:  # handles sys.exit inside helper functions
        raise TaskExecutionError(str(exc))
    except Exception as exc:  # safety net
        raise TaskExecutionError(f"Unexpected error during ECR migration: {exc}") from exc

    if not logs:
        logs = "Migration completed with no additional output."

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
