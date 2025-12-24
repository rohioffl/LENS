from __future__ import annotations

import importlib.util
import sys
import tempfile
from pathlib import Path

from inventory.forms import TcoReportForm
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
    helper_path = Path(__file__).resolve().parents[3] / "feature" / "tco.py"
    if not helper_path.exists():
        raise TaskExecutionError("Helper script feature/tco.py not found.")
    spec = importlib.util.spec_from_file_location("feature.tco", helper_path)
    if spec is None or spec.loader is None:
        raise TaskExecutionError("Unable to load tco helper module.")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)  # type: ignore[attr-defined]
    _HELPER_MODULE = module
    return _HELPER_MODULE


def run_tco_report_task(clean_data: dict) -> TaskExecutionResult:
    helper = _helper_module()
    csv_content = clean_data.get("csv_content") or ""
    filename = clean_data.get("filename") or "billing.csv"

    if not filename.lower().endswith(".csv"):
        filename = f"{filename}.csv"

    with tempfile.TemporaryDirectory(prefix="tco_report_") as tempdir:
        temp_dir = Path(tempdir)
        csv_path = temp_dir / filename
        csv_path.write_text(csv_content, encoding="utf-8")
        output_dir = temp_dir / "output"
        output_dir.mkdir(parents=True, exist_ok=True)

        argv_backup = sys.argv[:]
        sys.argv = [
            str(getattr(helper, "__file__", "tco.py")),
            "--input",
            str(csv_path),
            "--output-dir",
            str(output_dir),
        ]
        try:
            helper.main()
        except SystemExit as exc:
            raise TaskExecutionError(str(exc)) from exc
        finally:
            sys.argv = argv_backup

        artifacts = []
        for path in sorted(output_dir.glob("*.xlsx")):
            artifacts.append(
                GeneratedArtifact(
                    filename=path.name,
                    content=path.read_bytes(),
                    content_type=XLSX_CONTENT_TYPE,
                )
            )

        if not artifacts:
            raise TaskExecutionError("No XLSX output was generated.")

        return TaskExecutionResult(artifacts=artifacts)


automation_registry.register(
    TaskDefinition(
        task_id="tco_report",
        label="AWS TCO Report",
        description="Generate TCO spreadsheets from an AWS billing CSV.",
        form_class=TcoReportForm,
        runner=run_tco_report_task,
    )
)
