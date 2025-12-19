from __future__ import annotations

import importlib.util
from pathlib import Path

from inventory.forms import AwsSecurityAuditForm
from inventory.services.task_registry import (
    GeneratedArtifact,
    TaskDefinition,
    TaskExecutionError,
    TaskExecutionResult,
    automation_registry,
)

XLSX_CONTENT_TYPE = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
_HELPER_MODULE = None


def _helper_module():
    global _HELPER_MODULE  # noqa: PLW0603
    if _HELPER_MODULE is not None:
        return _HELPER_MODULE
    helper_path = Path(__file__).resolve().parents[3] / "feature" / "aws-standard-security-audit.py"
    if not helper_path.exists():
        raise TaskExecutionError("Helper script feature/aws-standard-security-audit.py not found.")
    spec = importlib.util.spec_from_file_location("feature.aws_standard_security_audit", helper_path)
    if spec is None or spec.loader is None:
        raise TaskExecutionError("Unable to load aws-standard-security-audit helper module.")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)  # type: ignore[attr-defined]
    _HELPER_MODULE = module
    return _HELPER_MODULE


def run_aws_security_audit_task(clean_data: dict) -> TaskExecutionResult:
    helper = _helper_module()
    try:
        result = helper.generate_security_audit_xlsx(
            access_key=clean_data.get("access_key"),
            secret_key=clean_data.get("secret_key"),
            session_token=clean_data.get("session_token"),
        )
    except Exception as exc:  # pragma: no cover - runtime guard
        raise TaskExecutionError(str(exc)) from exc

    content = result.get("content") if isinstance(result, dict) else None
    filename = result.get("filename") if isinstance(result, dict) else None
    if not content:
        raise TaskExecutionError("No audit report was generated.")

    artifact = GeneratedArtifact(
        filename=filename or "aws-security-audit.xlsx",
        content=content,
        content_type=XLSX_CONTENT_TYPE,
    )
    return TaskExecutionResult(artifacts=[artifact])


automation_registry.register(
    TaskDefinition(
        task_id="aws_security_audit",
        label="AWS Standard Security Audit",
        description="Run the AWS standard security audit and export an XLSX report.",
        form_class=AwsSecurityAuditForm,
        runner=run_aws_security_audit_task,
    )
)
