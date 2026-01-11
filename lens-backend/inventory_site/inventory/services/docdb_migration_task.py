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


def _zip_directory(directory: Path) -> bytes:
    """Zip a directory and return bytes"""
    import zipfile
    from io import BytesIO
    
    buffer = BytesIO()
    with zipfile.ZipFile(buffer, 'w', zipfile.ZIP_DEFLATED) as zipf:
        for file_path in directory.rglob('*'):
            if file_path.is_file():
                zipf.write(file_path, file_path.relative_to(directory))
    buffer.seek(0)
    return buffer.getvalue()


class DocDBMigrationForm(forms.Form):
    """Form for DocumentDB to Atlas Migration"""
    atlas_uri = forms.CharField(
        label="Atlas Connection String",
        widget=forms.TextInput(attrs={'placeholder': 'mongodb+srv://...'}),
        required=True,
        help_text="MongoDB Atlas connection string"
    )
    docdb_uri = forms.CharField(
        label="DocumentDB Connection String",
        widget=forms.TextInput(attrs={'placeholder': 'mongodb://...'}),
        required=True,
        help_text="AWS DocumentDB connection string"
    )
    action = forms.ChoiceField(
        label="Action",
        choices=[
            ('migrate', 'Migrate Data'),
            ('init_last_run', 'Initialize Last Run Timestamp'),
            ('estimate', 'Estimate Migration Size'),
        ],
        initial='migrate',
        required=True,
        help_text="Action to perform"
    )
    mode = forms.ChoiceField(
        label="Migration Mode",
        choices=[
            ('fresh', 'Fresh Migration'),
            ('incremental', 'Incremental Sync'),
        ],
        initial='fresh',
        required=False,
        help_text="Migration mode (only for 'migrate' action)"
    )
    databases = forms.CharField(
        label="Database Filter (optional)",
        widget=forms.Textarea(attrs={'rows': 2, 'placeholder': 'db1,db2,db3'}),
        required=False,
        help_text="Comma-separated list of databases to migrate. Leave empty for all databases."
    )
    num_workers = forms.IntegerField(
        label="Number of Workers",
        initial=8,
        min_value=1,
        max_value=32,
        required=False,
        help_text="Number of parallel workers for migration"
    )
    num_parallel_collections = forms.IntegerField(
        label="Parallel Collections",
        initial=os.cpu_count() or 4,
        min_value=1,
        max_value=16,
        required=False,
        help_text="Number of collections to process in parallel"
    )
    timestamp_field = forms.CharField(
        label="Timestamp Field",
        initial="auto",
        required=False,
        help_text="Field name for incremental sync. Use 'auto' for automatic detection."
    )
    match_index_names = forms.BooleanField(
        label="Match Index Names",
        initial=False,
        required=False,
        help_text="Recreate indexes to match DocumentDB names exactly"
    )
    delete_local_after = forms.BooleanField(
        label="Delete Local Dump After Migration",
        initial=False,
        required=False,
        help_text="Automatically clean up local dump files after successful migration"
    )
    dry_run = forms.BooleanField(
        label="Dry Run",
        initial=False,
        required=False,
        help_text="Validate configuration without executing any operations"
    )
    init_source = forms.ChoiceField(
        label="Initialization Source",
        choices=[
            ('atlas', 'Atlas (Latest Timestamp)'),
            ('docdb', 'DocumentDB (Latest Timestamp)'),
        ],
        initial='atlas',
        required=False,
        help_text="Source for initializing last run timestamp"
    )


def execute_docdb_migration(clean_data: dict, progress_callback=None) -> TaskExecutionResult:
    """Execute the DocumentDB to Atlas migration task"""
    # Extract form data
    atlas_uri = clean_data.get("atlas_uri", "").strip()
    docdb_uri = clean_data.get("docdb_uri", "").strip()
    action = clean_data.get("action", "migrate")
    mode = clean_data.get("mode", "fresh")
    databases_str = clean_data.get("databases", "").strip()
    databases = [db.strip() for db in databases_str.split(",") if db.strip()] if databases_str else []
    num_workers = clean_data.get("num_workers", 8)
    num_parallel_collections = clean_data.get("num_parallel_collections", os.cpu_count() or 4)
    timestamp_field = clean_data.get("timestamp_field", "auto").strip()
    match_index_names = clean_data.get("match_index_names", False)
    delete_local_after = clean_data.get("delete_local_after", False)
    dry_run = clean_data.get("dry_run", False)
    init_source = clean_data.get("init_source", "atlas")
    
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

