"""
DocumentDB to MongoDB Atlas Migration Task
Wraps the docdb_to_atlas_migration.py script for the task registry system.
"""
from __future__ import annotations

import base64
import json
import logging
import os
import shutil
import sys
import tempfile
from pathlib import Path
from typing import Dict, Optional

from django import forms

from inventory.services.task_registry import (
    GeneratedArtifact,
    TaskDefinition,
    TaskExecutionError,
    TaskExecutionResult,
    automation_registry,
)

# Lazy import configuration - will import migration module only when needed
REPO_ROOT = Path(__file__).resolve().parents[4]
FEATURE_DIR = REPO_ROOT / "lens-backend" / "feature"

def _get_migration_module():
    """Lazy import of migration module to avoid dataclass issues during Django startup"""
    import sys
    
    # Add feature directory to sys.path if not already there
    if str(FEATURE_DIR) not in sys.path:
        sys.path.insert(0, str(FEATURE_DIR))
    
    try:
        # Import the module
        import docdb_to_atlas_migration
        return docdb_to_atlas_migration
    except Exception as e:
        raise ImportError(f"Failed to import docdb_to_atlas_migration module: {e}")


class DocDBMigrationForm(forms.Form):
    """Form for DocumentDB to Atlas migration parameters"""
    
    # Connection URIs
    atlas_uri = forms.CharField(
        required=True,
        widget=forms.TextInput(attrs={"placeholder": "mongodb+srv://..."}),
        help_text="MongoDB Atlas connection string"
    )
    docdb_uri = forms.CharField(
        required=True,
        widget=forms.TextInput(attrs={"placeholder": "mongodb://..."}),
        help_text="DocumentDB read-only connection string"
    )
    
    # Migration mode
    mode = forms.ChoiceField(
        choices=[
            ("fresh", "Fresh Migration (drop & reload everything)"),
            ("incremental", "Incremental Migration (non-destructive)"),
        ],
        required=True,
        initial="fresh",
        help_text="Migration strategy"
    )
    
    # Database selection
    databases = forms.CharField(
        required=False,
        widget=forms.Textarea(attrs={"rows": 3, "placeholder": "db1\ndb2\ndb3"}),
        help_text="Limit migration to specific databases (one per line, leave empty for all)"
    )
    
    # Worker configuration
    num_workers = forms.IntegerField(
        initial=8,
        min_value=1,
        max_value=32,
        help_text="Number of insertion workers per collection"
    )
    num_parallel_collections = forms.IntegerField(
        initial=4,
        min_value=1,
        max_value=16,
        help_text="Number of collections to restore in parallel"
    )
    
    # Timestamp configuration
    timestamp_field = forms.CharField(
        required=False,
        initial="auto",
        help_text="Field used for incremental sync (default: auto-detect)"
    )
    
    # Advanced options
    match_index_names = forms.BooleanField(
        required=False,
        initial=False,
        help_text="Ensure Atlas index names match DocumentDB metadata"
    )
    delete_local_after = forms.BooleanField(
        required=False,
        initial=False,
        help_text="Delete local dump after successful migration"
    )
    dry_run = forms.BooleanField(
        required=False,
        initial=False,
        help_text="Run validation without executing destructive operations"
    )
    
    # Action type
    action = forms.ChoiceField(
        choices=[
            ("migrate", "Run Migration"),
            ("init_last_run", "Initialize Last Run Timestamp"),
            ("estimate", "Estimate Migration Size & Time"),
        ],
        required=True,
        initial="migrate",
        help_text="Action to perform"
    )
    init_source = forms.ChoiceField(
        choices=[
            ("atlas", "Atlas"),
            ("docdb", "DocumentDB"),
        ],
        required=False,
        initial="atlas",
        help_text="Source cluster for timestamp initialization"
    )


def _zip_directory(path: Path) -> bytes:
    """Zip a directory and return bytes"""
    from io import BytesIO
    from zipfile import ZipFile
    
    buffer = BytesIO()
    with ZipFile(buffer, "w") as archive:
        for file_path in path.rglob("*"):
            if file_path.is_dir():
                continue
            archive.write(file_path, arcname=str(file_path.relative_to(path)))
    buffer.seek(0)
    return buffer.getvalue()


def execute_docdb_migration(
    payload: dict,
    *,
    progress_callback=None,
) -> TaskExecutionResult:
    """
    Execute DocumentDB to Atlas migration
    
    Args:
        payload: Migration configuration from the form
        progress_callback: Optional callback for progress updates
    
    Returns:
        TaskExecutionResult with artifacts and logs
    """
    # Parse form data
    atlas_uri = payload.get("atlas_uri", "").strip()
    docdb_uri = payload.get("docdb_uri", "").strip()
    mode = payload.get("mode", "fresh")
    action = payload.get("action", "migrate")
    databases_raw = payload.get("databases", "").strip()
    databases = [db.strip() for db in databases_raw.split("\n") if db.strip()] if databases_raw else None
    
    num_workers = int(payload.get("num_workers", 8))
    num_parallel_collections = int(payload.get("num_parallel_collections", 4))
    timestamp_field = payload.get("timestamp_field", "auto").strip() or "auto"
    match_index_names = payload.get("match_index_names", False)
    delete_local_after = payload.get("delete_local_after", False)
    dry_run = payload.get("dry_run", False)
    init_source = payload.get("init_source", "atlas")
    
    # Validation
    if not atlas_uri:
        raise TaskExecutionError("Atlas URI is required")
    if not docdb_uri:
        raise TaskExecutionError("DocumentDB URI is required")
    
    # Create temporary work directory
    temp_dir = Path(tempfile.mkdtemp(prefix="docdb_migration_"))
    
    # Lazy load the migration module
    try:
        migration_module = _get_migration_module()
    except Exception as e:
        raise TaskExecutionError(f"Failed to load migration module: {str(e)}")
    
    try:
        # Build arguments for the migration script
        args = [
            f"--atlas-uri={atlas_uri}",
            f"--docdb-uri={docdb_uri}",
            f"--dir={temp_dir}",
            f"--num-workers={num_workers}",
            f"--num-parallel-collections={num_parallel_collections}",
            f"--mode={mode}",
        ]
        
        if databases:
            for db in databases:
                args.append(f"--database={db}")
        
        if timestamp_field and timestamp_field.lower() != "auto":
            args.append(f"--timestamp-field={timestamp_field}")
        
        if match_index_names:
            args.append("--match-index-names")
        
        if delete_local_after:
            args.append("--delete-local-after")
        
        if dry_run:
            args.append("--dry-run")
        
        args.append("--force")  # Skip confirmation prompts
        
        # Handle different actions
        if action == "init_last_run":
            args.append("--init-last-run")
            args.append(f"--init-source={init_source}")
            if progress_callback:
                progress_callback({"status": "running", "message": f"Initializing last run timestamp from {init_source}..."})
        elif action == "estimate":
            args.append("--dry-run")
            if progress_callback:
                progress_callback({"status": "running", "message": "Estimating migration size and time..."})
        else:
            if progress_callback:
                progress_callback({"status": "running", "message": f"Starting {mode} migration..."})
        
        # Parse arguments and run migration
        parsed_args = migration_module.parse_args(args)
        
        # Configure logging to capture output
        log_file = temp_dir / "migration_output.log"
        migration_module.configure_logging(parsed_args.log_dir)
        
        if progress_callback:
            progress_callback({"status": "running", "message": "Migration in progress... This may take a while."})
        
        # Run the migration
        exit_code = migration_module.main(args)
        
        if exit_code != 0:
            raise TaskExecutionError(f"Migration failed with exit code {exit_code}")
        
        if progress_callback:
            progress_callback({"status": "success", "message": "Migration completed successfully!"})
        
        # Collect artifacts
        artifacts = []
        
        # Collect log files
        log_dirs = list(temp_dir.glob("logs_*"))
        if log_dirs:
            latest_log_dir = max(log_dirs, key=os.path.getmtime)
            logs_zip = _zip_directory(latest_log_dir)
            artifacts.append(
                GeneratedArtifact(
                    name=f"migration_logs_{latest_log_dir.name}.zip",
                    content=base64.b64encode(logs_zip).decode("utf-8"),
                    description="Migration logs and detailed reports",
                )
            )
        
        # Collect last_run state file
        last_run_file = temp_dir / "last_run.json"
        if last_run_file.exists():
            artifacts.append(
                GeneratedArtifact(
                    name="last_run.json",
                    content=base64.b64encode(last_run_file.read_bytes()).decode("utf-8"),
                    description="Last run timestamp state for incremental sync",
                )
            )
        
        # Collect dump directory if it exists
        dump_dirs = list(temp_dir.glob("dump_*"))
        if dump_dirs and not delete_local_after:
            latest_dump_dir = max(dump_dirs, key=os.path.getmtime)
            # Only zip if it's reasonably sized (< 1GB)
            dump_size = sum(f.stat().st_size for f in latest_dump_dir.rglob("*") if f.is_file())
            if dump_size < 1024 * 1024 * 1024:  # 1GB
                dump_zip = _zip_directory(latest_dump_dir)
                artifacts.append(
                    GeneratedArtifact(
                        name=f"dump_{latest_dump_dir.name}.zip",
                        content=base64.b64encode(dump_zip).decode("utf-8"),
                        description="MongoDB dump files",
                    )
                )
        
        # Read summary from logs
        summary_lines = []
        if log_dirs:
            latest_log_dir = max(log_dirs, key=os.path.getmtime)
            
            # Find the most recent cycle log
            cycle_dirs = list(latest_log_dir.glob("cycle_*"))
            if cycle_dirs:
                latest_cycle = max(cycle_dirs, key=os.path.getmtime)
                
                # Read success/error files
                success_file = latest_cycle / "restore.success.txt"
                error_file = latest_cycle / "restore.errors.txt"
                zero_file = latest_cycle / "restore.zero_byte_skips.txt"
                
                if success_file.exists():
                    success_count = len(success_file.read_text().strip().split("\n"))
                    summary_lines.append(f"✅ Successfully migrated: {success_count} collections")
                
                if error_file.exists():
                    errors = error_file.read_text().strip().split("\n")
                    error_count = len([e for e in errors if e])
                    if error_count > 0:
                        summary_lines.append(f"❌ Failed: {error_count} collections")
                
                if zero_file.exists():
                    zero_count = len(zero_file.read_text().strip().split("\n"))
                    summary_lines.append(f"⚠️ Empty collections: {zero_count}")
        
        message = "\n".join(summary_lines) if summary_lines else "Migration completed successfully!"
        
        return TaskExecutionResult(
            success=True,
            message=message,
            artifacts=artifacts,
        )
    
    except Exception as e:
        # Check if it's a MigrationError
        if type(e).__name__ == 'MigrationError':
            raise TaskExecutionError(f"Migration error: {str(e)}")
        raise TaskExecutionError(f"Unexpected error: {str(e)}")
    finally:
        # Cleanup temporary directory
        try:
            shutil.rmtree(temp_dir, ignore_errors=True)
        except:
            pass


# Register the task
automation_registry.register(
    TaskDefinition(
        task_id="docdb_to_atlas_migration",
        label="DocumentDB to Atlas Migration",
        description="Migrate data from AWS DocumentDB to MongoDB Atlas with support for fresh and incremental modes",
        form_class=DocDBMigrationForm,
        runner=execute_docdb_migration,
    )
)

