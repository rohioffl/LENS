from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
from io import BytesIO
from pathlib import Path
from typing import Dict, List, Optional
from zipfile import ZipFile

# Import the box-project-aws module
REPO_ROOT = Path(__file__).resolve().parents[4]
BOX_PROJECT_AWS_SCRIPT = REPO_ROOT / "lens-backend" / "feature" / "box-project-aws.py"

# Import functions from box-project-aws
try:
    import importlib.util
    spec = importlib.util.spec_from_file_location("box_project_aws", BOX_PROJECT_AWS_SCRIPT)
    box_aws = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(box_aws)
except Exception as e:
    raise ImportError(f"Failed to import box-project-aws module: {e}")

from inventory.forms import BoxProjectAwsForm
from inventory.services.task_registry import (
    GeneratedArtifact,
    TaskDefinition,
    TaskExecutionError,
    TaskExecutionResult,
    automation_registry,
)

# Configure boto3 with credentials
def _configure_boto3(access_key: Optional[str], secret_key: Optional[str], session_token: Optional[str]):
    """Configure boto3 credentials from form data or environment variables"""
    # Use form data if provided, otherwise fall back to environment variables
    final_access_key = (access_key or "").strip() or os.environ.get("AWS_ACCESS_KEY_ID", "").strip()
    final_secret_key = (secret_key or "").strip() or os.environ.get("AWS_SECRET_ACCESS_KEY", "").strip()
    final_session_token = (session_token or "").strip() or os.environ.get("AWS_SESSION_TOKEN", "").strip()
    
    if final_access_key and final_secret_key:
        os.environ["AWS_ACCESS_KEY_ID"] = final_access_key
        os.environ["AWS_SECRET_ACCESS_KEY"] = final_secret_key
        if final_session_token:
            os.environ["AWS_SESSION_TOKEN"] = final_session_token
        elif "AWS_SESSION_TOKEN" in os.environ:
            # Keep existing session token if no new one provided
            pass


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


def _get_service_config(service: str, configs: Dict, region: str) -> Dict:
    """Get configuration for a service using boto3 functions"""
    service_config = configs.get(service, {})
    
    # Use boto3 functions to get real AWS data if not provided
    if service == "vpc":
        subnets = service_config.get("subnets", [])
        # Count public and private subnets
        num_public = sum(1 for s in subnets if s.get("type") == "public")
        num_private = sum(1 for s in subnets if s.get("type") == "private")
        return {
            "cidr": service_config.get("cidr", "10.0.0.0/16"),
            "enable_dns_hostnames": service_config.get("enable_dns_hostnames", True),
            "enable_dns_support": service_config.get("enable_dns_support", True),
            "subnets": subnets,  # New format: list of {cidr, type}
            "num_public_subnets": num_public if num_public > 0 else service_config.get("num_public_subnets", 2),
            "num_private_subnets": num_private if num_private > 0 else service_config.get("num_private_subnets", 2),
            "enable_internet_gateway": service_config.get("enable_internet_gateway", True),
            "enable_nat_gateway": service_config.get("enable_nat_gateway", True),
        }
    elif service == "ec2":
        ami = service_config.get("ami", "")
        instance_type = service_config.get("instance_type", "t3.micro")
        return {
            "ami": ami,
            "instance_type": instance_type,
            "subnet_id": service_config.get("subnet_id", ""),
            "key_name": service_config.get("key_name", ""),
        }
    elif service == "s3":
        return {
            "bucket_name": service_config.get("bucket_name", ""),
            "versioning": service_config.get("versioning", False),
            "encryption": service_config.get("encryption", True),
            "public_access": service_config.get("public_access", False),
        }
    elif service == "rds":
        return {
            "identifier": service_config.get("identifier", "box-rds"),
            "engine": service_config.get("engine", "mysql"),
            "instance_class": service_config.get("instance_class", "db.t3.micro"),
            "allocated_storage": service_config.get("allocated_storage", 20),
            "storage_type": service_config.get("storage_type", "gp3"),
            "db_name": service_config.get("db_name", ""),
            "username": service_config.get("username", "admin"),
            "password": service_config.get("password", ""),
            "subnet_ids": service_config.get("subnet_ids", []),
            "vpc_id": service_config.get("vpc_id", ""),
            "backup_retention_period": service_config.get("backup_retention_period", 7),
        }
    elif service == "ebs":
        return {
            "volume_size": service_config.get("volume_size", 20),
            "volume_type": service_config.get("volume_type", "gp3"),
            "availability_zone": service_config.get("availability_zone", ""),
            "encrypted": service_config.get("encrypted", True),
            "iops": service_config.get("iops"),
            "throughput": service_config.get("throughput"),
        }
    
    return service_config


def run_box_project_aws_task(clean_data: dict) -> TaskExecutionResult:
    """Run the AWS Box Project task with boto3 integration"""
    access_key = clean_data.get("access_key", "").strip()
    secret_key = clean_data.get("secret_key", "").strip()
    session_token = clean_data.get("session_token", "").strip()
    aws_region = clean_data.get("aws_region", "us-east-1")
    services: List[str] = clean_data.get("services", [])
    service_configs: Dict = clean_data.get("service_configs", {})
    
    if not services:
        raise TaskExecutionError("At least one service must be selected.")
    
    # Configure boto3 - will use environment variables if form data not provided
    _configure_boto3(access_key, secret_key, session_token)
    
    # Verify credentials are available (from form or environment)
    if not os.environ.get("AWS_ACCESS_KEY_ID") or not os.environ.get("AWS_SECRET_ACCESS_KEY"):
        raise TaskExecutionError(
            "AWS credentials are required. Provide them via form data or set AWS_ACCESS_KEY_ID and AWS_SECRET_ACCESS_KEY environment variables."
        )
    
    # Validate services
    valid_services = {svc for svc, _ in box_aws.TOP_5_SERVICES}
    missing = [svc for svc in services if svc not in valid_services]
    if missing:
        raise TaskExecutionError(f"Unsupported services: {', '.join(missing)}")
    
    temp_root = Path(tempfile.mkdtemp(prefix="box_project_aws_"))
    project_root = temp_root / "box-project"
    modules_root = project_root / "modules"
    modules_root.mkdir(parents=True, exist_ok=True)
    
    try:
        # Write provider.tf
        _write(project_root / "provider.tf", box_aws.provider_tf())
        
        module_blocks = []
        ordered_variables = []
        seen_variables = set()
        tfvars_values = {}
        
        def register_variable(name: str):
            if name not in seen_variables:
                seen_variables.add(name)
                ordered_variables.append(name)
        
        register_variable("region")
        tfvars_values["region"] = aws_region
        
        # Track VPC outputs for dependencies
        vpc_id_var = None
        subnet_ids_var = None
        
        # Process each service
        for svc in services:
            if svc not in box_aws.SERVICE_TEMPLATES:
                continue
            
            # Write module files
            mod_path = modules_root / svc
            template_files = box_aws.SERVICE_TEMPLATES[svc]()
            for fname, content in template_files.items():
                _write(mod_path / fname, content)
            
            # Get service configuration
            config = _get_service_config(svc, service_configs, aws_region)
            
            # Build module inputs
            inputs = {}
            for key, value in config.items():
                root_var_name = f"{svc}_{key}"
                register_variable(root_var_name)
                # Determine value type for coercion
                if isinstance(value, bool):
                    value_type = "bool"
                elif isinstance(value, (int, float)):
                    value_type = "number"
                elif isinstance(value, list):
                    value_type = "list"
                else:
                    value_type = "string"
                tfvars_values[root_var_name] = box_aws.coerce_tfvars_value(value, value_type)
                inputs[key] = f"var.{root_var_name}"
            
            # Handle dependencies
            if svc == "vpc":
                vpc_id_var = "module.vpc.vpc_id"
                subnet_ids_var = "module.vpc.public_subnet_ids"
            elif svc == "ec2" and vpc_id_var:
                inputs["vpc_id"] = vpc_id_var
                if not inputs.get("subnet_id"):
                    # Use public subnet if VPC module is selected
                    inputs["subnet_id"] = "module.vpc.public_subnet_ids[0]"
            elif svc == "rds" and vpc_id_var:
                if not inputs.get("vpc_id"):
                    inputs["vpc_id"] = vpc_id_var
                if not inputs.get("subnet_ids") or not config.get("subnet_ids"):
                    # Use private subnets for RDS
                    inputs["subnet_ids"] = "module.vpc.private_subnet_ids"
            
            module_blocks.append(box_aws.root_module_call(svc, inputs))
        
        # Write main.tf
        if module_blocks:
            main_tf_body = "\n\n".join(block.strip() for block in module_blocks)
        else:
            main_tf_body = "# No modules were selected."
        
        _write(project_root / "main.tf", main_tf_body)
        _write(project_root / "variables.tf", box_aws.render_variables_tf(ordered_variables))
        _write(project_root / "terraform.tfvars", box_aws.render_tfvars(tfvars_values))
        
        # Create zip archive
        archive_bytes = _zip_directory(project_root)
        summary = {
            "cloud": "aws",
            "region": aws_region,
            "services": services,
        }
        summary_bytes = json.dumps(summary, indent=2).encode("utf-8")
        
        artifacts = [
            GeneratedArtifact(
                filename="box-project-aws-terraform.zip",
                content=archive_bytes,
                content_type="application/zip",
            ),
            GeneratedArtifact(
                filename="box-project-aws-summary.json",
                content=summary_bytes,
                content_type="application/json",
            ),
        ]
        return TaskExecutionResult(artifacts)
    finally:
        shutil.rmtree(temp_root, ignore_errors=True)


automation_registry.register(
    TaskDefinition(
        task_id="box_project_aws",
        label="Box AWS Terraform Generator",
        description="Generate Terraform modules for AWS services using boto3 to fetch real AWS data (AMIs, instance types, etc.).",
        form_class=BoxProjectAwsForm,
        runner=run_box_project_aws_task,
    )
)

