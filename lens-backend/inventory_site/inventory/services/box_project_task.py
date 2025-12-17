from __future__ import annotations

import json
import shutil
import tempfile
from io import BytesIO
from pathlib import Path
from typing import Dict, List
from zipfile import ZipFile

from feature import box_project as box_cli
from inventory.forms import BoxProjectForm
from inventory.services.task_registry import (
    GeneratedArtifact,
    TaskDefinition,
    TaskExecutionError,
    TaskExecutionResult,
    automation_registry,
)


def _zip_directory(path: Path) -> bytes:
    buffer = BytesIO()
    with ZipFile(buffer, "w") as archive:
        for file_path in path.rglob("*"):
            if file_path.is_dir():
                continue
            archive.write(file_path, arcname=str(file_path.relative_to(path)))
    buffer.seek(0)
    return buffer.getvalue()


def _write(path: Path, content: str):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text((content or "").strip() + "\n", encoding="utf-8")


def _prefill_service_inputs(cloud: str, service: str, provided: Dict) -> Dict[str, str]:
    template_inputs = box_cli.MODULE_INPUTS.get(cloud, {}).get(service, [])
    values: Dict[str, str] = {}
    supplied = provided or {}
    for meta in template_inputs:
        default = meta.get("default")
        name = meta["name"]
        values[name] = supplied.get(name, default)
    return values


def _register_variable(name: str, ordered: List[str], seen: set[str]):
    if name not in seen:
        seen.add(name)
        ordered.append(name)


def run_box_project_task(clean_data: dict) -> TaskExecutionResult:
    cloud = clean_data["cloud_provider"]
    services: List[str] = clean_data["services"] or []
    service_inputs: Dict[str, Dict] = clean_data.get("service_inputs") or {}
    aws_region = clean_data.get("aws_region")
    gcp_region = clean_data.get("gcp_region")
    gcp_project = clean_data.get("gcp_project")

    valid_services = {svc for svc, _ in box_cli.TOP_SERVICES.get(cloud, [])}
    missing = [svc for svc in services if svc not in valid_services]
    if missing:
        raise TaskExecutionError(f"Unsupported services for {cloud.upper()}: {', '.join(missing)}")

    temp_root = Path(tempfile.mkdtemp(prefix="box_project_"))
    project_root = temp_root / "box-project"
    modules_root = project_root / "modules"
    modules_root.mkdir(parents=True, exist_ok=True)
    if cloud == "gcp":
        (modules_root / "gcp").mkdir(parents=True, exist_ok=True)

    try:
        _write(project_root / "provider.tf", box_cli.provider_tf(cloud))

        module_blocks: List[str] = []
        ordered_variables: List[str] = []
        seen_variables: set[str] = set()
        tfvars_values: Dict[str, object] = {}

        if cloud == "aws":
            _register_variable("region", ordered_variables, seen_variables)
            tfvars_values["region"] = aws_region
        else:
            _register_variable("project", ordered_variables, seen_variables)
            _register_variable("region", ordered_variables, seen_variables)
            tfvars_values["project"] = gcp_project
            tfvars_values["region"] = gcp_region

        for service in services:
            factory = box_cli.SERVICE_TEMPLATES.get(cloud, {}).get(service)
            if not factory:
                raise TaskExecutionError(f"No module template found for service '{service}'.")
            template_files = factory()
            target_dir = (
                modules_root / service
                if cloud == "aws"
                else modules_root / "gcp" / service
            )
            for fname, content in template_files.items():
                _write(target_dir / fname, content)

            inputs_for_service = _prefill_service_inputs(cloud, service, service_inputs.get(service))
            module_input_map: Dict[str, str] = {}
            for meta in box_cli.MODULE_INPUTS.get(cloud, {}).get(service, []):
                root_name = f"{service}_{meta['name']}"
                _register_variable(root_name, ordered_variables, seen_variables)
                value = inputs_for_service.get(meta["name"], meta.get("default"))
                tfvars_values[root_name] = box_cli.coerce_tfvars_value(value, meta)
                module_input_map[meta["name"]] = f"var.{root_name}"

            module_blocks.append(box_cli.root_module_call(cloud, service, module_input_map))

        if module_blocks:
            _write(project_root / "main.tf", "\n\n".join(block.strip() for block in module_blocks))
        else:
            _write(project_root / "main.tf", "# No modules selected.")

        _write(project_root / "variables.tf", box_cli.render_variables_tf(ordered_variables))
        _write(project_root / "terraform.tfvars", box_cli.render_tfvars(tfvars_values))

        archive_bytes = _zip_directory(project_root)
        summary = {
            "cloud": cloud,
            "services": services,
            "aws_region": aws_region,
            "gcp_project": gcp_project,
            "gcp_region": gcp_region,
        }
        summary_bytes = json.dumps(summary, indent=2).encode("utf-8")

        artifacts = [
            GeneratedArtifact(
                filename="box-project-terraform.zip",
                content=archive_bytes,
                content_type="application/zip",
            ),
            GeneratedArtifact(
                filename="box-project-summary.json",
                content=summary_bytes,
                content_type="application/json",
            ),
        ]
        return TaskExecutionResult(artifacts)
    finally:
        shutil.rmtree(temp_root, ignore_errors=True)


automation_registry.register(
    TaskDefinition(
        task_id="box_project",
        label="Box Terraform Generator",
        description="Generate Terraform modules for selected AWS or GCP services using box-project presets.",
        form_class=BoxProjectForm,
        runner=run_box_project_task,
    )
)
