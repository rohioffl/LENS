import json
import sys
import shutil
import uuid
from contextlib import redirect_stderr, redirect_stdout
from io import BytesIO, StringIO
from pathlib import Path
from zipfile import ZipFile

from inventory.forms import TerraformVpcForm
from inventory.services.task_registry import (
    GeneratedArtifact,
    TaskDefinition,
    TaskExecutionError,
    TaskExecutionResult,
    automation_registry,
)

ROOT_DIR = Path(__file__).resolve().parents[3]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from feature import terraform_vpc  # noqa: E402  pylint: disable=wrong-import-position


def _zip_directory(directory: Path, extra_files: dict[str, bytes] | None = None) -> bytes:
    buffer = BytesIO()
    with ZipFile(buffer, "w") as zip_file:
        for file_path in directory.rglob("*"):
            if file_path.is_dir():
                continue
            zip_file.write(file_path, arcname=str(file_path.relative_to(directory)))
        if extra_files:
            for name, content in extra_files.items():
                zip_file.writestr(name, content)
    buffer.seek(0)
    return buffer.getvalue()


def _tee_output(func, *args, **kwargs) -> str:
    buffer = StringIO()
    with redirect_stdout(buffer), redirect_stderr(buffer):
        func(*args, **kwargs)
    text = buffer.getvalue()
    if text:
        print(text, end="")
    return text


def _cleanup_directory(path: Path) -> None:
    shutil.rmtree(path, ignore_errors=True)
    parent = path.parent
    try:
        parent.rmdir()
    except OSError:
        pass


def _parse_mapping(raw_value: str | None, label: str) -> dict | None:
    if not raw_value:
        return None
    try:
        parsed = json.loads(raw_value)
    except json.JSONDecodeError as exc:  # pragma: no cover - validation guard
        raise TaskExecutionError(f"{label} must be a JSON object mapping subnet IDs to values.") from exc
    if not isinstance(parsed, dict):
        raise TaskExecutionError(f"{label} must be a JSON object mapping subnet IDs to values.")
    return parsed


def _configure_aws_credentials(data: dict) -> None:
    terraform_vpc.configure_boto3_session(
        access_key=data.get("access_key"),
        secret_key=data.get("secret_key"),
        session_token=None,
        profile_name=None,
    )


def _run_generate_bundle(data: dict) -> TaskExecutionResult:
    _configure_aws_credentials(data)
    print(f"Discovering AWS VPC {data['aws_vpc_id']} in {data['aws_region']}...")
    aws_vpc = terraform_vpc.discover_aws_vpc(data["aws_vpc_id"], data["aws_region"])
    print(f"Loaded AWS VPC {aws_vpc.id} ({aws_vpc.cidr}) with {len(aws_vpc.subnets)} subnets.")
    output_root = f"api_{uuid.uuid4().hex[:8]}"
    subnet_cidr_overrides = _parse_mapping(data.get("subnet_cidr_map"), "Subnet CIDR overrides")
    subnet_name_overrides = _parse_mapping(data.get("subnet_name_map"), "Subnet name overrides")

    print(
        "Generating Terraform bundle targeting "
        f"GCP project {data['gcp_project']} / network {data['gcp_network']} (fallback region {data['gcp_region_fallback']})..."
    )
    target_dir, structure_checks, validation_messages = terraform_vpc.generate_terraform_from_aws_vpc(
        aws_vpc,
        data["gcp_project"],
        data["gcp_network"],
        data["gcp_region_fallback"],
        output_root,
        overwrite=False,
        subnet_cidr_overrides=subnet_cidr_overrides,
        subnet_name_overrides=subnet_name_overrides,
        preview_architecture=False,
    )

    directory = Path(target_dir)
    print(f"Terraform module files staged under {directory}.")

    summary_lines: list[str] = []
    if structure_checks:
        summary_lines.append("Bundle Checks:")
        summary_lines.extend(structure_checks)
    if validation_messages:
        if summary_lines:
            summary_lines.append("")
        summary_lines.append("Terraform Validation:")
        summary_lines.extend(validation_messages)

    extra_files = {}
    summary_text = "\n".join(summary_lines).strip()
    if summary_text:
        extra_files[f"{directory.name}_summary.txt"] = summary_text.encode("utf-8")
        print("Validation summary:\n" + summary_text)

    print("Packaging Terraform bundle into ZIP archive...")
    archive_bytes = _zip_directory(directory, extra_files=extra_files or None)
    artifact = GeneratedArtifact(
        filename=f"{directory.name}.zip",
        content=archive_bytes,
        content_type="application/zip",
    )

    print(f"Bundle {directory.name}.zip ready for download.")
    _cleanup_directory(directory)
    print("Temporary workspace cleaned up.")
    return TaskExecutionResult([artifact], archive_name=f"{directory.name}.zip")


def run_terraform_task(clean_data: dict) -> TaskExecutionResult:
    try:
        return _run_generate_bundle(clean_data)
    except TaskExecutionError:
        raise
    except Exception as exc:  # pragma: no cover - safety net for API surface
        raise TaskExecutionError(str(exc)) from exc


automation_registry.register(
    TaskDefinition(
        task_id="terraform_vpc",
        label="VPC Migration Toolkit",
        description="Plan AWS→GCP migrations or generate Terraform bundles for a VPC.",
        form_class=TerraformVpcForm,
        runner=run_terraform_task,
    )
)
