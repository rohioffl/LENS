from __future__ import annotations

from inventory.forms import AwsInventoryForm
from inventory.services.aws_inventory import (
    create_boto3_session,
    generate_workbooks,
    resolve_cost_period,
    resolve_cost_period_inputs,
)
from inventory.services.task_registry import (
    GeneratedArtifact,
    TaskDefinition,
    TaskExecutionError,
    TaskExecutionResult,
    automation_registry,
)

XLSX_CONTENT_TYPE = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


def _friendly_inventory_name(region_label: str, timestamp: str) -> str:
    safe_region = region_label.replace(" ", "_").replace("/", "-") or "global"
    return f"{safe_region}_AWS_Inventory_{timestamp}.xlsx"


def _run_aws_inventory(form_data) -> TaskExecutionResult:
    session = create_boto3_session(
        access_key=form_data.get("access_key"),
        secret_key=form_data.get("secret_key"),
        session_token=form_data.get("session_token"),
        profile_name=form_data.get("profile_name"),
    )

    regions = form_data["regions"]
    resources = form_data["resources"]
    print(f"[AWS Inventory] Starting inventory for regions: {', '.join(regions)}")
    print(f"[AWS Inventory] Selected resources: {', '.join(resources)}")

    from_date = form_data.get("from_date") or ""
    to_date = form_data.get("to_date") or ""

    cost_period = None
    if any(resource.lower() == "cost" for resource in resources):
        cost_period = resolve_cost_period_inputs(from_date, to_date)
        if not cost_period:
            cost_period = resolve_cost_period(from_date, to_date)
        if not cost_period:
            raise TaskExecutionError("Enter valid cost analysis dates.")

    workbooks, timestamp = generate_workbooks(
        session=session,
        regions=regions,
        selected_resources=resources,
        cost_period=cost_period,
        interactive=False,
    )

    artifacts = []
    for workbook in workbooks:
        if not workbook["content"]:
            continue
        friendly_name = _friendly_inventory_name(workbook["region"], timestamp)
        artifacts.append(
            GeneratedArtifact(
                filename=friendly_name,
                content=workbook["content"],
                content_type=XLSX_CONTENT_TYPE,
            )
        )
        print(f"[AWS Inventory] Finished {workbook['region']} -> {friendly_name}")

    if not artifacts:
        raise TaskExecutionError("No resources found for the requested regions and resources.")

    return TaskExecutionResult(
        artifacts=artifacts,
        archive_name=None,
    )


automation_registry.register(
    TaskDefinition(
        task_id="aws_inventory",
        label="AWS Inventory Export",
        description="Collect AWS resources into Excel workbooks.",
        form_class=AwsInventoryForm,
        runner=_run_aws_inventory,
    )
)
