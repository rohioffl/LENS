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
    """Configure boto3 credentials - uses AWS credential chain (environment, ~/.aws/credentials, IAM role, etc.)"""
    import boto3
    
    # If credentials are explicitly provided in the form, use them
    if access_key and access_key.strip() and secret_key and secret_key.strip():
        os.environ["AWS_ACCESS_KEY_ID"] = access_key.strip()
        os.environ["AWS_SECRET_ACCESS_KEY"] = secret_key.strip()
        if session_token and session_token.strip():
            os.environ["AWS_SESSION_TOKEN"] = session_token.strip()
    else:
        # Let boto3 use its default credential chain:
        # 1. Environment variables (AWS_ACCESS_KEY_ID, AWS_SECRET_ACCESS_KEY)
        # 2. ~/.aws/credentials file (configured via `aws configure`)
        # 3. ~/.aws/config file
        # 4. IAM role (if running on EC2)
        # No need to set anything - boto3 will automatically find credentials
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


def _parse_tags(tags_value):
    """Parse tags from JSON string or return as-is if already a dict"""
    if isinstance(tags_value, dict):
        return tags_value
    if isinstance(tags_value, str) and tags_value.strip():
        try:
            return json.loads(tags_value)
        except:
            return {}
    return {}


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
        # Support multiple EC2 instances
        instances_config = service_config.get("instances", {})
        instances = []
        
        # Default values from root config
        default_ami = service_config.get("ami", "")
        default_instance_type = service_config.get("instance_type", "t3.micro")
        default_subnet_id = service_config.get("subnet_id", "")
        default_key_name = service_config.get("key_name", "")
        default_public_key = service_config.get("public_key", "")
        default_root_volume_size = service_config.get("root_volume_size", 8)
        default_root_volume_type = service_config.get("root_volume_type", "gp3")
        
        if instances_config:
            for inst_id, inst_cfg in instances_config.items():
                instances.append({
                    "id": inst_id,
                    "name": inst_cfg.get("name", f"instance-{inst_id}"),
                    "ami": inst_cfg.get("ami", default_ami),
                    "instance_type": inst_cfg.get("instance_type", default_instance_type),
                    "subnet_id": inst_cfg.get("subnet_id", default_subnet_id),
                    # Note: key_name and public_key are handled at root EC2 config level, not per-instance
                    "root_volume_size": inst_cfg.get("root_volume_size", default_root_volume_size),
                    "root_volume_type": inst_cfg.get("root_volume_type", default_root_volume_type),
                    "security_group_name": inst_cfg.get("security_group_name", ""),
                    "iam_role": inst_cfg.get("iam_role", ""),
                    "user_data": inst_cfg.get("user_data", ""),
                    "tags": _parse_tags(inst_cfg.get("tags", "")),
                })
        else:
            # Fallback to single instance for backward compatibility
            instances.append({
                "id": "1",
                "name": service_config.get("name", "box-ec2"),
                "ami": default_ami,
                "instance_type": default_instance_type,
                "subnet_id": default_subnet_id,
                "key_name": default_key_name,
                "public_key": default_public_key,
                "root_volume_size": default_root_volume_size,
                "root_volume_type": default_root_volume_type,
                "security_group_name": service_config.get("security_group_name", ""),
                "iam_role": service_config.get("iam_role", ""),
                "user_data": service_config.get("user_data", ""),
                "tags": _parse_tags(service_config.get("tags", "")),
            })
        
        # Additional EBS volumes (integrated into EC2)
        additional_volumes = []
        volumes_config = service_config.get("additional_volumes", [])
        for vol in volumes_config:
            additional_volumes.append({
                "id": vol.get("id", "1"),
                "name": vol.get("name", f"volume-{vol.get('id', 1)}"),
                "size": vol.get("size", 20),
                "type": vol.get("type", "gp3"),
                "iops": vol.get("iops", 3000),
                "encrypted": vol.get("encrypted", True),
                "linked_ec2": vol.get("linkedEc2", ""),
            })
        
        return {
            "instances": instances,
            "key_name": default_key_name,
            "public_key": default_public_key,
            "additional_volumes": additional_volumes,
        }
    elif service == "s3":
        # Support multiple S3 buckets
        buckets_config = service_config.get("buckets", [])
        buckets = []
        
        if buckets_config:
            for bucket in buckets_config:
                buckets.append({
                    "bucket_name": bucket.get("bucket_name", ""),
                    "versioning": bucket.get("versioning", True),
                    "encryption": bucket.get("encryption", True),
                    "block_public_access": bucket.get("block_public_access", True),
                    "storage_class": bucket.get("storage_class", "STANDARD"),
                    "enable_logging": bucket.get("enable_logging", False),
                    "lifecycle_ia_days": bucket.get("lifecycle_ia_days", None),
                    "lifecycle_glacier_days": bucket.get("lifecycle_glacier_days", None),
                    "lifecycle_expiration_days": bucket.get("lifecycle_expiration_days", None),
                    "enable_cors": bucket.get("enable_cors", False),
                    "tags": _parse_tags(bucket.get("tags", "")),
                })
        else:
            # Fallback to single bucket for backward compatibility
            buckets.append({
                "bucket_name": service_config.get("bucket_name", ""),
                "versioning": service_config.get("versioning", True),
                "encryption": service_config.get("encryption", True),
                "block_public_access": service_config.get("block_public_access", True),
                "storage_class": service_config.get("storage_class", "STANDARD"),
                "enable_logging": service_config.get("enable_logging", False),
                "lifecycle_ia_days": service_config.get("lifecycle_ia_days", None),
                "lifecycle_glacier_days": service_config.get("lifecycle_glacier_days", None),
                "lifecycle_expiration_days": service_config.get("lifecycle_expiration_days", None),
                "enable_cors": service_config.get("enable_cors", False),
                "tags": _parse_tags(service_config.get("tags", "")),
            })
        
        return {"buckets": buckets}
    elif service == "rds":
        # Support multiple RDS databases
        databases_config = service_config.get("databases", [])
        databases = []
        
        if databases_config:
            for db in databases_config:
                databases.append({
                    "identifier": db.get("identifier", "box-rds"),
                    "engine": db.get("engine", "mysql"),
                    "instance_class": db.get("instance_class", "db.t3.micro"),
                    "allocated_storage": db.get("allocated_storage", 20),
                    "storage_type": db.get("storage_type", "gp3"),
                    "db_name": db.get("db_name", ""),
                    "username": db.get("username", "admin"),
                    "password": db.get("password", ""),
                    "backup_retention_period": db.get("backup_retention_period", 7),
                    "security_group_name": db.get("security_group_name", ""),
                    "publicly_accessible": db.get("publicly_accessible", False),
                    "multi_az": db.get("multi_az", False),
                    "backup_window": db.get("backup_window", ""),
                    "maintenance_window": db.get("maintenance_window", ""),
                    "tags": _parse_tags(db.get("tags", "")),
                })
        else:
            # Fallback to single database for backward compatibility
            databases.append({
                "identifier": service_config.get("identifier", "box-rds"),
                "engine": service_config.get("engine", "mysql"),
                "instance_class": service_config.get("instance_class", "db.t3.micro"),
                "allocated_storage": service_config.get("allocated_storage", 20),
                "storage_type": service_config.get("storage_type", "gp3"),
                "db_name": service_config.get("db_name", ""),
                "username": service_config.get("username", "admin"),
                "password": service_config.get("password", ""),
                "backup_retention_period": service_config.get("backup_retention_period", 7),
                "security_group_name": service_config.get("security_group_name", ""),
                "publicly_accessible": service_config.get("publicly_accessible", False),
                "multi_az": service_config.get("multi_az", False),
                "backup_window": service_config.get("backup_window", ""),
                "maintenance_window": service_config.get("maintenance_window", ""),
                "tags": _parse_tags(service_config.get("tags", "")),
            })
        
        return {
            "databases": databases,
            "subnet_ids": service_config.get("subnet_ids", []),
            "vpc_id": service_config.get("vpc_id", ""),
        }
    elif service == "ebs":
        # Support multiple EBS volumes with EC2 linking
        volumes_config = service_config.get("volumes", [])
        volumes = []
        
        if volumes_config:
            for vol in volumes_config:
                volumes.append({
                    "id": vol.get("id", 1),
                    "name": vol.get("name", f"volume-{vol.get('id', 1)}"),
                    "size": vol.get("size", 20),
                    "type": vol.get("type", "gp3"),
                    "iops": vol.get("iops", 3000),
                    "encrypted": vol.get("encrypted", True),
                    "linked_ec2": vol.get("linkedEc2", ""),  # EC2 instance ID to attach to
                })
        else:
            # Fallback to single volume for backward compatibility
            volumes.append({
                "id": 1,
                "name": "data-volume",
                "size": service_config.get("volume_size", 20),
                "type": service_config.get("volume_type", "gp3"),
                "iops": service_config.get("iops", 3000),
                "encrypted": service_config.get("encrypted", True),
                "linked_ec2": "",
            })
        
        return {
            "volumes": volumes,
            "availability_zone": service_config.get("availability_zone", ""),
        }
    elif service == "efs":
        # Support multiple EFS file systems
        filesystems_config = service_config.get("filesystems", [])
        filesystems = []
        
        if filesystems_config:
            for fs in filesystems_config:
                fs_config = {
                    "name": fs.get("name", "box-efs"),
                    "performance_mode": fs.get("performance_mode", "generalPurpose"),
                    "throughput_mode": fs.get("throughput_mode", "bursting"),
                    "storage_class": fs.get("storage_class", "STANDARD"),
                    "encrypted": fs.get("encrypted", True),
                    "enable_backup": fs.get("enable_backup", True),
                    "transition_to_ia": fs.get("transition_to_ia", None),
                    "security_group_name": fs.get("security_group_name", ""),
                    "tags": _parse_tags(fs.get("tags", "")),
                }
                # Only include KMS key ID if it's actually provided (advanced use case)
                kms_key = fs.get("kms_key_id", "")
                if kms_key and kms_key.strip():
                    fs_config["kms_key_id"] = kms_key
                filesystems.append(fs_config)
        else:
            # Fallback to single file system for backward compatibility
            fs_config = {
                "name": service_config.get("name", "box-efs"),
                "performance_mode": service_config.get("performance_mode", "generalPurpose"),
                "throughput_mode": service_config.get("throughput_mode", "bursting"),
                "storage_class": service_config.get("storage_class", "STANDARD"),
                "encrypted": service_config.get("encrypted", True),
                "enable_backup": service_config.get("enable_backup", True),
                "transition_to_ia": service_config.get("transition_to_ia", None),
                "security_group_name": service_config.get("security_group_name", ""),
                "tags": _parse_tags(service_config.get("tags", "")),
            }
            # Only include KMS key ID if it's actually provided (advanced use case)
            kms_key = service_config.get("kms_key_id", "")
            if kms_key and kms_key.strip():
                fs_config["kms_key_id"] = kms_key
            filesystems.append(fs_config)
        
        return {
            "filesystems": filesystems,
            "subnet_ids": service_config.get("subnet_ids", []),
            "vpc_id": service_config.get("vpc_id", ""),
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
    
    # Configure boto3 - uses AWS credential chain (env vars, ~/.aws/credentials, IAM role, etc.)
    import boto3
    from botocore.exceptions import NoCredentialsError, PartialCredentialsError
    
    _configure_boto3(access_key, secret_key, session_token)
    
    # Verify credentials are available using boto3's credential chain
    try:
        session = boto3.Session()
        credentials = session.get_credentials()
        if credentials is None:
            raise TaskExecutionError(
                "AWS credentials not found. Please configure using 'aws configure' or provide credentials in the form."
            )
        # Access the credentials to ensure they're valid
        _ = credentials.access_key
        _ = credentials.secret_key
    except (NoCredentialsError, PartialCredentialsError) as e:
        raise TaskExecutionError(
            f"AWS credentials are invalid or incomplete: {str(e)}"
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
            elif svc == "efs" and vpc_id_var:
                if not inputs.get("vpc_id"):
                    inputs["vpc_id"] = vpc_id_var
                if not inputs.get("subnet_ids") or not config.get("subnet_ids"):
                    # Use private subnets for EFS (recommended for security)
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

