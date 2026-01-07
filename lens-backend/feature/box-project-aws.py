#!/usr/bin/env python3
"""
AWS Terraform Generator with boto3 integration
Generates accurate Terraform code using real AWS data
"""
import json
import textwrap
from pathlib import Path
from typing import List, Dict, Any, Optional, Tuple
import sys

try:
    import boto3
    from botocore.exceptions import ClientError, BotoCoreError
except ImportError:
    print("ERROR: boto3 is required. Install with: pip install boto3")
    sys.exit(1)

ROOT = Path("box-project")
MODULES_DIR = ROOT / "modules"

# Top 5 AWS Services
TOP_5_SERVICES = [
    ("vpc", "Amazon VPC"),
    ("ec2", "Amazon EC2"),
    ("s3", "Amazon S3"),
    ("rds", "Amazon RDS"),
    ("ebs", "Amazon EBS"),
]

# AWS Regions (common ones)
COMMON_REGIONS = [
    "us-east-1", "us-east-2", "us-west-1", "us-west-2",
    "eu-west-1", "eu-central-1", "ap-south-1", "ap-southeast-1",
    "ap-northeast-1", "sa-east-1"
]


def ask(q: str, default: Optional[str] = None, choices: Optional[List[str]] = None) -> str:
    """Interactive prompt with default and choices"""
    prompt = f"{q}"
    if default:
        prompt += f" [{default}]"
    if choices:
        prompt += f"\n  Options: {', '.join(choices[:10])}{'...' if len(choices) > 10 else ''}"
    prompt += ": "
    
    while True:
        v = input(prompt).strip()
        if not v and default:
            return default
        if not v:
            continue
        if choices and v not in choices:
            print(f"Invalid choice. Please select from: {', '.join(choices)}")
            continue
        return v


def ask_numbered(q: str, items: List[Tuple[str, str]], allow_multiple: bool = False) -> List[str]:
    """Show numbered list and return selected items"""
    print(f"\n{q}:")
    for idx, (key, label) in enumerate(items, start=1):
        print(f"  {idx:2}. {label}")
    
    if allow_multiple:
        choices = input("\nEnter choice numbers (e.g. 1,3,5) or 'all': ").strip()
        if choices.lower() == "all":
            return [key for key, _ in items]
        selected = []
        for item in choices.split(","):
            item = item.strip()
            if not item.isdigit():
                continue
            idx = int(item)
            if 1 <= idx <= len(items):
                selected.append(items[idx - 1][0])
        return selected
    else:
        while True:
            choice = input(f"\nEnter choice number (1-{len(items)}): ").strip()
            if not choice.isdigit():
                continue
            idx = int(choice)
            if 1 <= idx <= len(items):
                return items[idx - 1][0]
            print(f"Please enter a number between 1 and {len(items)}")


def get_aws_regions() -> List[str]:
    """Get available AWS regions using boto3"""
    try:
        ec2 = boto3.client("ec2", region_name="us-east-1")
        response = ec2.describe_regions()
        regions = [r["RegionName"] for r in response["Regions"]]
        return sorted(regions)
    except (ClientError, BotoCoreError) as e:
        print(f"⚠️  Could not fetch regions from AWS: {e}")
        print("Using common regions list instead.")
        return COMMON_REGIONS


def get_ec2_instance_types(region: str) -> List[str]:
    """Get available EC2 instance types for a region"""
    try:
        ec2 = boto3.client("ec2", region_name=region)
        paginator = ec2.get_paginator("describe_instance_type_offerings")
        instance_types = []
        for page in paginator.paginate(LocationType="region", Filters=[{"Name": "location", "Values": [region]}]):
            for offering in page["InstanceTypeOfferings"]:
                instance_types.append(offering["InstanceType"])
        return sorted(set(instance_types))
    except (ClientError, BotoCoreError) as e:
        print(f"⚠️  Could not fetch instance types: {e}")
        return ["t3.micro", "t3.small", "t3.medium", "t3.large", "m5.large", "m5.xlarge"]


def get_ec2_instance_type_details(region: str, instance_types: List[str]) -> List[Dict[str, Any]]:
    """Get detailed information for EC2 instance types (vCPU, memory, pricing)"""
    try:
        ec2 = boto3.client("ec2", region_name=region)
        # Get instance type details in batches
        details = []
        for i in range(0, len(instance_types), 100):
            batch = instance_types[i:i+100]
            try:
                response = ec2.describe_instance_types(InstanceTypes=batch)
                for itype in response.get("InstanceTypes", []):
                    vcpu_info = itype.get("VCpuInfo", {})
                    memory_info = itype.get("MemoryInfo", {})
                    processor_info = itype.get("ProcessorInfo", {})
                    
                    details.append({
                        "instance_type": itype.get("InstanceType", ""),
                        "vcpu": vcpu_info.get("DefaultVCpus", 0),
                        "memory_gib": round(memory_info.get("SizeInMiB", 0) / 1024.0, 1) if memory_info.get("SizeInMiB") else 0,
                        "memory_mib": memory_info.get("SizeInMiB", 0),
                        "architecture": processor_info.get("SupportedArchitectures", ["x86_64"])[0] if processor_info.get("SupportedArchitectures") else "x86_64",
                        "current_generation": itype.get("CurrentGeneration", True),
                        "free_tier_eligible": itype.get("FreeTierEligible", False),
                        "family": itype.get("InstanceType", "").split(".")[0] if "." in itype.get("InstanceType", "") else "",
                    })
            except Exception as e:
                print(f"⚠️  Could not fetch details for batch: {e}")
                continue
        
        return details
    except (ClientError, BotoCoreError) as e:
        print(f"⚠️  Could not fetch instance type details: {e}")
        return []


def get_ec2_amis(region: str, owner: str = "amazon", os_type: Optional[str] = None, os_version: Optional[str] = None) -> List[Dict[str, str]]:
    """Get latest/fresh AMIs for a region, filtered by OS type and version (like AWS console)"""
    try:
        ec2 = boto3.client("ec2", region_name=region)
        
        # Define OS-specific owner IDs and version patterns (for latest/fresh AMIs)
        os_configs = {
            "amazon-linux": {
                "owners": ["137112412989"],  # Amazon
                "versions": {
                    "2023": ["al2023-ami-*", "amzn2-ami-*", "amzn-ami-*"],  # Amazon Linux 2023 uses al2023 prefix
                    "2022": ["al2022-ami-*", "amzn2-ami-*"],
                    "latest": ["al2023-ami-*", "al2022-ami-*", "amzn2-ami-*", "amzn-ami-*"],
                },
            },
            "ubuntu": {
                "owners": ["099720109477"],  # Canonical
                "versions": {
                    "24.04": ["ubuntu/images/hvm-ssd/ubuntu-jammy-22.04*", "ubuntu/images/hvm-ssd/ubuntu-noble-24.04*"],
                    "22.04": ["ubuntu/images/hvm-ssd/ubuntu-jammy-22.04*"],
                    "20.04": ["ubuntu/images/hvm-ssd/ubuntu-focal-20.04*"],
                    "latest": ["ubuntu/images/hvm-ssd/ubuntu-*"],
                },
            },
            "windows": {
                "owners": ["801119661308"],  # Amazon Windows
                "versions": {
                    "2022": ["Windows_Server-2022-English-*"],
                    "2019": ["Windows_Server-2019-English-*"],
                    "2016": ["Windows_Server-2016-English-*"],
                    "latest": ["Windows_Server-*"],
                },
            },
            "rhel": {
                "owners": ["309956199498"],  # Red Hat
                "versions": {
                    "9": ["RHEL-9.*"],
                    "8": ["RHEL-8.*"],
                    "7": ["RHEL-7.*"],
                    "latest": ["RHEL-*"],
                },
            },
            "suse": {
                "owners": ["013907871322"],  # SUSE
                "versions": {
                    "15": ["suse-sles-15-*"],
                    "12": ["suse-sles-12-*"],
                    "latest": ["suse-sles-*"],
                },
            },
            "debian": {
                "owners": ["136693071363"],  # Debian
                "versions": {
                    "12": ["debian-12-*"],
                    "11": ["debian-11-*"],
                    "latest": ["debian-*"],
                },
            },
        }
        
        filters = [
            {"Name": "state", "Values": ["available"]},
            {"Name": "architecture", "Values": ["x86_64"]},
            {"Name": "image-type", "Values": ["machine"]},  # Only machine images (not kernel/ramdisk)
        ]
        
        if os_type and os_type in os_configs:
            config = os_configs[os_type]
            owners = config["owners"]
            
            # For Amazon Linux, don't use name filter - just filter by owner and architecture
            # Name filters are too restrictive and may miss valid AMIs
            if os_type == "amazon-linux":
                # Don't add name filter for Amazon Linux - let it return all Amazon Linux AMIs
                # We'll filter by version after fetching
                pass
            elif os_version and os_version in config.get("versions", {}):
                version_patterns = config["versions"][os_version]
                # Use name filter for version
                filters.append({"Name": "name", "Values": version_patterns})
            elif os_version == "latest" and "versions" in config:
                version_patterns = config["versions"].get("latest", [])
                filters.append({"Name": "name", "Values": version_patterns})
        else:
            # Default: Amazon Linux 2023 - no name filter
            owners = ["137112412989"]
        
        # For Amazon Linux, fetch more results since we filter after
        max_results = 100 if os_type == "amazon-linux" else 50
        response = ec2.describe_images(Filters=filters, Owners=owners, MaxResults=max_results)
        amis = []
        for img in response["Images"]:
            name = img.get("Name", "Unknown")
            description = img.get("Description", "")
            
            # Extract version from name
            version = "latest"
            if os_type == "amazon-linux":
                name_lower = name.lower()
                if "al2023" in name_lower or ("2023" in name_lower and "al2022" not in name_lower):
                    version = "2023"
                elif "al2022" in name_lower or ("2022" in name_lower and "al2023" not in name_lower):
                    version = "2022"
            elif os_type == "ubuntu":
                if "24.04" in name or "noble" in name:
                    version = "24.04"
                elif "22.04" in name or "jammy" in name:
                    version = "22.04"
                elif "20.04" in name or "focal" in name:
                    version = "20.04"
            elif os_type == "windows":
                if "2022" in name:
                    version = "2022"
                elif "2019" in name:
                    version = "2019"
                elif "2016" in name:
                    version = "2016"
            elif os_type == "rhel":
                if "RHEL-9" in name:
                    version = "9"
                elif "RHEL-8" in name:
                    version = "8"
                elif "RHEL-7" in name:
                    version = "7"
            
            amis.append({
                "id": img["ImageId"],
                "name": name,
                "description": description,
                "creation_date": img.get("CreationDate", ""),
                "os_type": os_type or "amazon-linux",
                "os_version": version,
                "platform": img.get("Platform", "linux"),
                "virtualization_type": img.get("VirtualizationType", "hvm"),
            })
        
        # For Amazon Linux, filter by version after fetching (if version specified)
        if os_type == "amazon-linux" and os_version and os_version != "latest":
            filtered_amis = []
            for ami in amis:
                ami_name_lower = ami["name"].lower()
                if os_version == "2023":
                    # Match al2023, amzn2-ami with 2023, or any 2023 reference
                    if "al2023" in ami_name_lower or ("2023" in ami_name_lower and "al2022" not in ami_name_lower):
                        filtered_amis.append(ami)
                elif os_version == "2022":
                    # Match al2022, amzn2-ami with 2022, or any 2022 reference
                    if "al2022" in ami_name_lower or ("2022" in ami_name_lower and "al2023" not in ami_name_lower):
                        filtered_amis.append(ami)
            amis = filtered_amis if filtered_amis else amis  # If no matches, return all to avoid empty results
        
        # Sort by creation date (newest first) - get latest/fresh AMIs
        amis.sort(key=lambda x: x["creation_date"], reverse=True)
        # Return only the latest 5 AMIs (fresh/new like AWS console) for faster loading
        return amis[:5]
    except Exception as e:
        print(f"⚠️  Could not fetch AMIs: {e}")
        import traceback
        print(traceback.format_exc())
        # Return empty list instead of default AMI - let frontend handle it
        return []


def get_rds_engines(region: str) -> List[str]:
    """Get available RDS engine versions"""
    try:
        rds = boto3.client("rds", region_name=region)
        response = rds.describe_db_engine_versions()
        engines = sorted(set([v["Engine"] for v in response["DBEngineVersions"]]))
        return engines
    except (ClientError, BotoCoreError) as e:
        print(f"⚠️  Could not fetch RDS engines: {e}")
        return ["mysql", "postgres", "mariadb", "oracle-ee", "sqlserver-ee"]


def get_rds_instance_classes(region: str, engine: str) -> List[str]:
    """Get available RDS instance classes for an engine"""
    try:
        rds = boto3.client("rds", region_name=region)
        response = rds.describe_orderable_db_instance_options(
            Engine=engine,
            MaxRecords=100
        )
        classes = sorted(set([opt["DBInstanceClass"] for opt in response["OrderableDBInstanceOptions"]]))
        return classes
    except (ClientError, BotoCoreError) as e:
        print(f"⚠️  Could not fetch RDS instance classes: {e}")
        return ["db.t3.micro", "db.t3.small", "db.t3.medium", "db.m5.large"]


def get_lambda_runtimes(region: str) -> List[str]:
    """Get available Lambda runtimes"""
    try:
        lambda_client = boto3.client("lambda", region_name=region)
        response = lambda_client.list_layer_versions(LayerName="AWSLambda-Python-AWS-SDK")
        # Standard runtimes
        runtimes = [
            "python3.11", "python3.10", "python3.9", "python3.8",
            "nodejs20.x", "nodejs18.x", "nodejs16.x",
            "java17", "java11", "java8.al2",
            "go1.x", "dotnet8", "dotnet6", "ruby3.2"
        ]
        return runtimes
    except (ClientError, BotoCoreError) as e:
        print(f"⚠️  Could not fetch Lambda runtimes: {e}")
        return ["python3.11", "python3.10", "nodejs20.x", "java17"]


def get_iam_roles(region: str) -> List[Dict[str, str]]:
    """Get available IAM roles"""
    try:
        iam = boto3.client("iam")
        response = iam.list_roles(MaxItems=50)
        roles = [{"arn": r["Arn"], "name": r["RoleName"]} for r in response["Roles"]]
        return roles
    except (ClientError, BotoCoreError) as e:
        print(f"⚠️  Could not fetch IAM roles: {e}")
        return []


def write(path: Path, content: str):
    """Write content to file, creating directories if needed"""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content.strip() + "\n", encoding="utf-8")


def coerce_tfvars_value(value: Any, value_type: str = "string") -> Any:
    """Coerce value to appropriate Terraform type"""
    if value_type == "number":
        try:
            if value is None or value == "":
                return 0
            return float(value) if "." in str(value) else int(value)
        except (ValueError, TypeError):
            return 0
    if value_type == "bool":
        return str(value).strip().lower() in {"1", "true", "yes", "y"}
    if value_type == "list":
        if isinstance(value, list):
            return value
        if not value:
            return []
        return [item.strip() for item in str(value).split(",") if item.strip()]
    return value or ""


# ==================== TERRAFORM MODULES ====================

def vpc_module() -> Dict[str, str]:
    """VPC Terraform module with enhanced configuration"""
    return {
        "variables.tf": """
variable "cidr" {
  description = "CIDR block for VPC"
  type        = string
}

variable "enable_dns_hostnames" {
  description = "Enable DNS hostnames"
  type        = bool
  default     = true
}

variable "enable_dns_support" {
  description = "Enable DNS support"
  type        = bool
  default     = true
}

variable "num_public_subnets" {
  description = "Number of public subnets (used if subnets not provided)"
  type        = number
  default     = 2
}

variable "num_private_subnets" {
  description = "Number of private subnets (used if subnets not provided)"
  type        = number
  default     = 2
}

variable "subnets" {
  description = "List of subnets with custom CIDRs. Format: [{cidr: \"10.0.1.0/24\", type: \"public\"}, ...]"
  type = list(object({
    cidr = string
    type = string
  }))
  default = []
}

variable "enable_nat_gateway" {
  description = "Enable NAT Gateway for private subnets"
  type        = bool
  default     = true
}

variable "enable_internet_gateway" {
  description = "Enable Internet Gateway"
  type        = bool
  default     = true
}

variable "tags" {
  description = "Tags to apply to VPC"
  type        = map(string)
  default     = {}
}
""",
        "main.tf": """
resource "aws_vpc" "this" {
  cidr_block           = var.cidr
  enable_dns_hostnames = var.enable_dns_hostnames
  enable_dns_support   = var.enable_dns_support

  tags = merge(
    {
      Name = "box-vpc"
    },
    var.tags
  )
}

resource "aws_internet_gateway" "this" {
  count  = var.enable_internet_gateway ? 1 : 0
  vpc_id = aws_vpc.this.id

  tags = {
    Name = "box-igw"
  }
}

locals {
  # Use custom subnets if provided, otherwise generate from num_public/num_private
  use_custom_subnets = length(var.subnets) > 0
  public_subnets = local.use_custom_subnets ? [
    for subnet in var.subnets : subnet if subnet.type == "public"
  ] : [
    for i in range(var.num_public_subnets) : {
      cidr = cidrsubnet(var.cidr, 8, i)
      type = "public"
    }
  ]
  private_subnets = local.use_custom_subnets ? [
    for subnet in var.subnets : subnet if subnet.type == "private"
  ] : [
    for i in range(var.num_private_subnets) : {
      cidr = cidrsubnet(var.cidr, 8, i + var.num_public_subnets)
      type = "private"
    }
  ]
  all_subnets = local.use_custom_subnets ? var.subnets : concat(local.public_subnets, local.private_subnets)
}

resource "aws_subnet" "public" {
  count             = length(local.public_subnets)
  vpc_id            = aws_vpc.this.id
  cidr_block        = local.public_subnets[count.index].cidr
  availability_zone = data.aws_availability_zones.available.names[count.index % length(data.aws_availability_zones.available.names)]
  map_public_ip_on_launch = true

  tags = {
    Name = "box-public-subnet-${count.index + 1}"
  }
}

resource "aws_subnet" "private" {
  count             = length(local.private_subnets)
  vpc_id            = aws_vpc.this.id
  cidr_block        = local.private_subnets[count.index].cidr
  availability_zone = data.aws_availability_zones.available.names[count.index % length(data.aws_availability_zones.available.names)]

  tags = {
    Name = "box-private-subnet-${count.index + 1}"
  }
}

resource "aws_eip" "nat" {
  count  = var.enable_nat_gateway ? length(local.private_subnets) : 0
  domain = "vpc"
  depends_on = [aws_internet_gateway.this]

  tags = {
    Name = "box-nat-eip-${count.index + 1}"
  }
}

resource "aws_nat_gateway" "this" {
  count         = var.enable_nat_gateway ? length(local.private_subnets) : 0
  allocation_id = aws_eip.nat[count.index].id
  subnet_id     = aws_subnet.public[count.index % length(local.public_subnets)].id
  depends_on    = [aws_internet_gateway.this]

  tags = {
    Name = "box-nat-gateway-${count.index + 1}"
  }
}

resource "aws_route_table" "public" {
  count  = var.enable_internet_gateway ? 1 : 0
  vpc_id = aws_vpc.this.id

  route {
    cidr_block = "0.0.0.0/0"
    gateway_id = aws_internet_gateway.this[0].id
  }

  tags = {
    Name = "box-public-rt"
  }
}

resource "aws_route_table" "private" {
  count  = var.enable_nat_gateway ? length(local.private_subnets) : 0
  vpc_id = aws_vpc.this.id

  route {
    cidr_block     = "0.0.0.0/0"
    nat_gateway_id = aws_nat_gateway.this[count.index].id
  }

  tags = {
    Name = "box-private-rt-${count.index + 1}"
  }
}

resource "aws_route_table_association" "public" {
  count          = var.enable_internet_gateway ? length(aws_subnet.public) : 0
  subnet_id      = aws_subnet.public[count.index].id
  route_table_id = aws_route_table.public[0].id
}

resource "aws_route_table_association" "private" {
  count          = var.enable_nat_gateway ? length(aws_subnet.private) : 0
  subnet_id      = aws_subnet.private[count.index].id
  route_table_id = aws_route_table.private[count.index].id
}

data "aws_availability_zones" "available" {
  state = "available"
}
""",
        "outputs.tf": """
output "vpc_id" {
  description = "VPC ID"
  value       = aws_vpc.this.id
}

output "vpc_cidr" {
  description = "VPC CIDR block"
  value       = aws_vpc.this.cidr_block
}

output "public_subnet_ids" {
  description = "Public subnet IDs"
  value       = aws_subnet.public[*].id
}

output "private_subnet_ids" {
  description = "Private subnet IDs"
  value       = aws_subnet.private[*].id
}

output "internet_gateway_id" {
  description = "Internet Gateway ID"
  value       = var.enable_internet_gateway ? aws_internet_gateway.this[0].id : null
}

output "nat_gateway_ids" {
  description = "NAT Gateway IDs"
  value       = var.enable_nat_gateway ? aws_nat_gateway.this[*].id : []
}
"""
    }


def ec2_module() -> Dict[str, str]:
    """EC2 Terraform module"""
    return {
        "variables.tf": """
variable "ami" {
  description = "AMI ID"
  type        = string
}

variable "instance_type" {
  description = "EC2 instance type"
  type        = string
}

variable "subnet_id" {
  description = "Subnet ID to launch instance in"
  type        = string
}

variable "key_name" {
  description = "Key pair name (optional)"
  type        = string
  default     = ""
}

variable "user_data" {
  description = "User data script"
  type        = string
  default     = ""
}

variable "security_group_ids" {
  description = "Security group IDs"
  type        = list(string)
  default     = []
}

variable "tags" {
  description = "Tags to apply to instance"
  type        = map(string)
  default     = {}
}

variable "vpc_id" {
  description = "VPC ID"
  type        = string
}
""",
        "main.tf": """
resource "aws_security_group" "ec2" {
  name        = "box-ec2-sg"
  description = "Security group for EC2 instance"
  vpc_id      = var.vpc_id

  ingress {
    description = "SSH"
    from_port   = 22
    to_port     = 22
    protocol    = "tcp"
    cidr_blocks = ["0.0.0.0/0"]
  }

  ingress {
    description = "HTTP"
    from_port   = 80
    to_port     = 80
    protocol    = "tcp"
    cidr_blocks = ["0.0.0.0/0"]
  }

  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }

  tags = {
    Name = "box-ec2-sg"
  }
}

resource "aws_instance" "this" {
  ami                    = var.ami
  instance_type          = var.instance_type
  subnet_id              = var.subnet_id
  key_name               = var.key_name != "" ? var.key_name : null
  user_data              = var.user_data != "" ? base64encode(var.user_data) : null
  vpc_security_group_ids = concat([aws_security_group.ec2.id], var.security_group_ids)

  tags = merge(
    {
      Name = "box-ec2"
    },
    var.tags
  )
}
""",
        "outputs.tf": """
output "instance_id" {
  description = "EC2 instance ID"
  value       = aws_instance.this.id
}

output "instance_public_ip" {
  description = "Public IP address"
  value       = aws_instance.this.public_ip
}

output "instance_private_ip" {
  description = "Private IP address"
  value       = aws_instance.this.private_ip
}

output "security_group_id" {
  description = "Security group ID"
  value       = aws_security_group.ec2.id
}
"""
    }


def s3_module() -> Dict[str, str]:
    """S3 Terraform module"""
    return {
        "variables.tf": """
variable "bucket_name" {
  description = "S3 bucket name (must be globally unique)"
  type        = string
}

variable "versioning" {
  description = "Enable versioning"
  type        = bool
  default     = false
}

variable "encryption" {
  description = "Enable server-side encryption"
  type        = bool
  default     = true
}

variable "public_access" {
  description = "Allow public access"
  type        = bool
  default     = false
}

variable "tags" {
  description = "Tags to apply to bucket"
  type        = map(string)
  default     = {}
}
""",
        "main.tf": """
resource "aws_s3_bucket" "this" {
  bucket = var.bucket_name

  tags = merge(
    {
      Name = var.bucket_name
    },
    var.tags
  )
}

resource "aws_s3_bucket_versioning" "this" {
  bucket = aws_s3_bucket.this.id
  versioning_configuration {
    status = var.versioning ? "Enabled" : "Disabled"
  }
}

resource "aws_s3_bucket_server_side_encryption_configuration" "this" {
  bucket = aws_s3_bucket.this.id

  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
  }
}

resource "aws_s3_bucket_public_access_block" "this" {
  bucket = aws_s3_bucket.this.id

  block_public_acls       = !var.public_access
  block_public_policy     = !var.public_access
  ignore_public_acls      = !var.public_access
  restrict_public_buckets = !var.public_access
}
""",
        "outputs.tf": """
output "bucket_id" {
  description = "S3 bucket ID"
  value       = aws_s3_bucket.this.id
}

output "bucket_arn" {
  description = "S3 bucket ARN"
  value       = aws_s3_bucket.this.arn
}

output "bucket_domain_name" {
  description = "S3 bucket domain name"
  value       = aws_s3_bucket.this.bucket_domain_name
}
"""
    }


def rds_module() -> Dict[str, str]:
    """RDS Terraform module"""
    return {
        "variables.tf": """
variable "identifier" {
  description = "RDS instance identifier"
  type        = string
}

variable "engine" {
  description = "Database engine"
  type        = string
}

variable "engine_version" {
  description = "Engine version"
  type        = string
  default     = ""
}

variable "instance_class" {
  description = "DB instance class"
  type        = string
}

variable "allocated_storage" {
  description = "Allocated storage in GB"
  type        = number
  default     = 20
}

variable "storage_type" {
  description = "Storage type"
  type        = string
  default     = "gp3"
}

variable "db_name" {
  description = "Database name"
  type        = string
  default     = ""
}

variable "username" {
  description = "Master username"
  type        = string
}

variable "password" {
  description = "Master password"
  type        = string
  sensitive   = true
}

variable "subnet_ids" {
  description = "Subnet IDs for RDS"
  type        = list(string)
}

variable "vpc_id" {
  description = "VPC ID"
  type        = string
}

variable "backup_retention_period" {
  description = "Backup retention period in days"
  type        = number
  default     = 7
}

variable "skip_final_snapshot" {
  description = "Skip final snapshot on deletion"
  type        = bool
  default     = true
}

variable "tags" {
  description = "Tags to apply to RDS instance"
  type        = map(string)
  default     = {}
}
""",
        "main.tf": """
resource "aws_db_subnet_group" "this" {
  name       = "${var.identifier}-subnet-group"
  subnet_ids = var.subnet_ids

  tags = {
    Name = "${var.identifier}-subnet-group"
  }
}

resource "aws_security_group" "rds" {
  name        = "${var.identifier}-sg"
  description = "Security group for RDS instance"
  vpc_id      = var.vpc_id

  ingress {
    description     = "Database access"
    from_port       = 3306
    to_port         = 3306
    protocol        = "tcp"
    security_groups = []
    cidr_blocks     = [data.aws_vpc.this.cidr_block]
  }

  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }

  tags = {
    Name = "${var.identifier}-sg"
  }
}

data "aws_vpc" "this" {
  id = var.vpc_id
}

resource "aws_db_instance" "this" {
  identifier             = var.identifier
  engine                 = var.engine
  engine_version         = var.engine_version != "" ? var.engine_version : null
  instance_class         = var.instance_class
  allocated_storage      = var.allocated_storage
  storage_type           = var.storage_type
  db_name                = var.db_name != "" ? var.db_name : null
  username               = var.username
  password               = var.password
  db_subnet_group_name   = aws_db_subnet_group.this.name
  vpc_security_group_ids = [aws_security_group.rds.id]
  backup_retention_period = var.backup_retention_period
  skip_final_snapshot    = var.skip_final_snapshot
  publicly_accessible    = false

  tags = merge(
    {
      Name = var.identifier
    },
    var.tags
  )
}
""",
        "outputs.tf": """
output "db_instance_id" {
  description = "RDS instance ID"
  value       = aws_db_instance.this.id
}

output "db_instance_endpoint" {
  description = "RDS instance endpoint"
  value       = aws_db_instance.this.endpoint
}

output "db_instance_arn" {
  description = "RDS instance ARN"
  value       = aws_db_instance.this.arn
}
"""
    }


def ebs_module() -> Dict[str, str]:
    """EBS Terraform module"""
    return {
        "variables.tf": """
variable "volume_size" {
  description = "Size of the volume in GB"
  type        = number
  default     = 20
}

variable "volume_type" {
  description = "Type of EBS volume"
  type        = string
  default     = "gp3"
}

variable "availability_zone" {
  description = "Availability zone for the volume"
  type        = string
}

variable "encrypted" {
  description = "Enable encryption"
  type        = bool
  default     = true
}

variable "kms_key_id" {
  description = "KMS key ID for encryption (optional)"
  type        = string
  default     = ""
}

variable "iops" {
  description = "IOPS for gp3 volumes"
  type        = number
  default     = 3000
}

variable "throughput" {
  description = "Throughput in MiB/s for gp3 volumes"
  type        = number
  default     = 125
}

variable "tags" {
  description = "Tags to apply to EBS volume"
  type        = map(string)
  default     = {}
}
""",
        "main.tf": """
resource "aws_ebs_volume" "this" {
  availability_zone = var.availability_zone
  size              = var.volume_size
  type              = var.volume_type
  encrypted         = var.encrypted
  iops              = var.volume_type == "gp3" ? var.iops : null
  throughput        = var.volume_type == "gp3" ? var.throughput : null
  kms_key_id        = var.encrypted && var.kms_key_id != "" ? var.kms_key_id : null

  tags = merge(
    {
      Name = "box-ebs-volume"
    },
    var.tags
  )
}
""",
        "outputs.tf": """
output "volume_id" {
  description = "EBS volume ID"
  value       = aws_ebs_volume.this.id
}

output "volume_arn" {
  description = "EBS volume ARN"
  value       = aws_ebs_volume.this.arn
}

output "volume_size" {
  description = "EBS volume size"
  value       = aws_ebs_volume.this.size
}
"""
    }


SERVICE_TEMPLATES = {
    "vpc": vpc_module,
    "ec2": ec2_module,
    "s3": s3_module,
    "rds": rds_module,
    "ebs": ebs_module,
}


def configure_vpc(region: str) -> Dict[str, Any]:
    """Configure VPC module interactively"""
    print("\n" + "="*60)
    print("🔧 Configuring VPC Module")
    print("="*60)
    
    cidr = ask("VPC CIDR block", "10.0.0.0/16")
    enable_dns = ask("Enable DNS hostnames? (true/false)", "true", ["true", "false"])
    
    return {
        "cidr": cidr,
        "enable_dns_hostnames": coerce_tfvars_value(enable_dns, "bool"),
        "enable_dns_support": coerce_tfvars_value("true", "bool"),
    }


def configure_ec2(region: str, vpc_id: Optional[str] = None) -> Dict[str, Any]:
    """Configure EC2 module interactively"""
    print("\n" + "="*60)
    print("🔧 Configuring EC2 Module")
    print("="*60)
    
    # Get AMIs
    print("\n📦 Fetching available AMIs...")
    amis = get_ec2_amis(region)
    if amis:
        ami_items = [(ami["id"], f"{ami['name']} ({ami['id']})") for ami in amis]
        ami_items.append(("custom", "Enter custom AMI ID"))
        ami_choice = ask_numbered("Select AMI", ami_items)
        if ami_choice == "custom":
            ami = ask("Enter AMI ID")
        else:
            ami = ami_choice
    else:
        ami = ask("AMI ID", "ami-0c55b159cbfafe1f0")
    
    # Get instance types
    print("\n💻 Fetching available instance types...")
    instance_types = get_ec2_instance_types(region)
    if instance_types:
        # Show common types first
        common = ["t3.micro", "t3.small", "t3.medium", "t3.large", "m5.large", "m5.xlarge"]
        common_filtered = [t for t in common if t in instance_types]
        other = [t for t in instance_types if t not in common]
        display_types = common_filtered[:10] + other[:5]
        
        type_items = [(t, t) for t in display_types]
        type_items.append(("custom", "Enter custom instance type"))
        instance_choice = ask_numbered("Select instance type", type_items)
        if instance_choice == "custom":
            instance_type = ask("Enter instance type", "t3.micro")
        else:
            instance_type = instance_choice
    else:
        instance_type = ask("Instance type", "t3.micro")
    
    subnet_id = ask("Subnet ID (leave empty if VPC module selected)", "")
    key_name = ask("Key pair name (optional)", "")
    
    return {
        "ami": ami,
        "instance_type": instance_type,
        "subnet_id": subnet_id,
        "key_name": key_name,
    }


def configure_s3(region: str) -> Dict[str, Any]:
    """Configure S3 module interactively"""
    print("\n" + "="*60)
    print("🔧 Configuring S3 Module")
    print("="*60)
    
    bucket_name = ask("Bucket name (must be globally unique)")
    versioning = ask("Enable versioning? (true/false)", "false", ["true", "false"])
    encryption = ask("Enable encryption? (true/false)", "true", ["true", "false"])
    public_access = ask("Allow public access? (true/false)", "false", ["true", "false"])
    
    return {
        "bucket_name": bucket_name,
        "versioning": coerce_tfvars_value(versioning, "bool"),
        "encryption": coerce_tfvars_value(encryption, "bool"),
        "public_access": coerce_tfvars_value(public_access, "bool"),
    }


def configure_rds(region: str, vpc_id: Optional[str] = None, subnet_ids: Optional[List[str]] = None) -> Dict[str, Any]:
    """Configure RDS module interactively"""
    print("\n" + "="*60)
    print("🔧 Configuring RDS Module")
    print("="*60)
    
    identifier = ask("RDS instance identifier")
    
    # Get engines
    print("\n🗄️  Fetching available database engines...")
    engines = get_rds_engines(region)
    if engines:
        engine_items = [(e, e) for e in engines[:10]]
        engine = ask_numbered("Select database engine", engine_items)
    else:
        engine = ask("Database engine", "mysql", ["mysql", "postgres", "mariadb"])
    
    # Get instance classes
    print(f"\n💻 Fetching available instance classes for {engine}...")
    instance_classes = get_rds_instance_classes(region, engine)
    if instance_classes:
        class_items = [(c, c) for c in instance_classes[:15]]
        class_items.append(("custom", "Enter custom instance class"))
        class_choice = ask_numbered("Select instance class", class_items)
        if class_choice == "custom":
            instance_class = ask("Enter instance class", "db.t3.micro")
        else:
            instance_class = class_choice
    else:
        instance_class = ask("Instance class", "db.t3.micro")
    
    allocated_storage = ask("Allocated storage (GB)", "20")
    db_name = ask("Database name (optional)", "")
    username = ask("Master username", "admin")
    password = ask("Master password", "")
    
    subnet_ids_input = ask("Subnet IDs (comma-separated, leave empty if VPC module selected)", "")
    subnet_ids_list = subnet_ids if subnet_ids else (
        [s.strip() for s in subnet_ids_input.split(",") if s.strip()] if subnet_ids_input else []
    )
    
    vpc_id_input = ask("VPC ID (leave empty if VPC module selected)", "")
    
    return {
        "identifier": identifier,
        "engine": engine,
        "instance_class": instance_class,
        "allocated_storage": coerce_tfvars_value(allocated_storage, "number"),
        "db_name": db_name,
        "username": username,
        "password": password,
        "subnet_ids": subnet_ids_list,
        "vpc_id": vpc_id_input,
    }


def configure_ebs(region: str) -> Dict[str, Any]:
    """Configure EBS module interactively"""
    print("\n" + "="*60)
    print("🔧 Configuring EBS Module")
    print("="*60)
    
    # Get availability zones
    try:
        ec2 = boto3.client("ec2", region_name=region)
        az_response = ec2.describe_availability_zones()
        azs = [az["ZoneName"] for az in az_response["AvailabilityZones"]]
        if azs:
            az_items = [(az, az) for az in azs[:5]]
            availability_zone = ask_numbered("Select availability zone", az_items)
        else:
            availability_zone = ask("Availability zone")
    except Exception:
        availability_zone = ask("Availability zone")
    
    volume_size = ask("Volume size (GB)", "20")
    volume_type = ask("Volume type", "gp3", ["gp3", "gp2", "io1", "io2", "st1", "sc1"])
    encrypted = ask("Enable encryption? (true/false)", "true", ["true", "false"])
    
    iops = ""
    throughput = ""
    if volume_type == "gp3":
        iops = ask("IOPS (3000-16000)", "3000")
        throughput = ask("Throughput in MiB/s (125-1000)", "125")
    elif volume_type in ["io1", "io2"]:
        iops = ask("IOPS (100-64000)", "3000")
    
    return {
        "volume_size": coerce_tfvars_value(volume_size, "number"),
        "volume_type": volume_type,
        "availability_zone": availability_zone,
        "encrypted": coerce_tfvars_value(encrypted, "bool"),
        "iops": coerce_tfvars_value(iops, "number") if iops else None,
        "throughput": coerce_tfvars_value(throughput, "number") if throughput else None,
    }


CONFIGURERS = {
    "vpc": configure_vpc,
    "ec2": configure_ec2,
    "s3": configure_s3,
    "rds": configure_rds,
    "ebs": configure_ebs,
}


def render_variables_tf(variable_names: List[str]) -> str:
    """Render variables.tf file"""
    blocks = [f'variable "{name}" {{\n}}\n' for name in variable_names]
    return "\n".join(blocks)


def render_tfvars(values: Dict[str, Any]) -> str:
    """Render terraform.tfvars file"""
    lines = []
    for key, value in values.items():
        if value is None:
            continue
        if isinstance(value, bool):
            lines.append(f"{key} = {str(value).lower()}")
        elif isinstance(value, (int, float)):
            lines.append(f"{key} = {value}")
        elif isinstance(value, list):
            lines.append(f"{key} = {json.dumps(value)}")
        else:
            lines.append(f"{key} = {json.dumps(str(value))}")
    return "\n".join(lines)


def root_module_call(service: str, inputs: Dict[str, str]) -> str:
    """Generate root module call"""
    source = f"./modules/{service}"
    block = f'module "{service}" {{\n  source = "{source}"\n'
    for k, v in inputs.items():
        block += f"  {k} = {v}\n"
    block += "}\n"
    return block


def provider_tf() -> str:
    """Generate provider.tf"""
    return """
terraform {
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.0"
    }
    archive = {
      source  = "hashicorp/archive"
      version = "~> 2.4"
    }
  }
  required_version = ">= 1.0"
}

provider "aws" {
  region = var.region
}
"""


def main():
    """Main function"""
    print("\n" + "="*60)
    print("🚀 AWS Terraform Generator with boto3 Integration")
    print("="*60)
    print("\nThis tool uses boto3 to fetch real AWS data and generate")
    print("accurate Terraform code for your infrastructure.\n")
    
    # Get region
    print("🌍 Fetching available AWS regions...")
    regions = get_aws_regions()
    region_items = [(r, r) for r in regions[:20]]
    region_items.append(("custom", "Enter custom region"))
    region_choice = ask_numbered("Select AWS region", region_items)
    if region_choice == "custom":
        region = ask("Enter region name")
    else:
        region = region_choice
    
    print(f"\n✅ Using region: {region}\n")
    
    # Select services
    selected_services = ask_numbered("Select AWS services to configure", TOP_5_SERVICES, allow_multiple=True)
    
    if not selected_services:
        print("\n⚠️  No services selected. Exiting.")
        return
    
    print(f"\n✅ Selected services: {', '.join([s.upper() for s in selected_services])}\n")
    
    # Setup directories
    ROOT.mkdir(exist_ok=True)
    write(ROOT / "provider.tf", provider_tf())
    
    module_blocks = []
    ordered_variables = []
    seen_variables = set()
    tfvars_values = {}
    
    def register_variable(name: str):
        if name not in seen_variables:
            seen_variables.add(name)
            ordered_variables.append(name)
    
    register_variable("region")
    tfvars_values["region"] = region
    
    # Track VPC outputs for other modules
    vpc_id_var = None
    subnet_ids_var = None
    
    # Configure each service
    for svc in selected_services:
        if svc not in SERVICE_TEMPLATES:
            print(f"\n⚠️  Skipping {svc} (not implemented)")
            continue
        
        # Write module files
        mod_path = MODULES_DIR / svc
        template_files = SERVICE_TEMPLATES[svc]()
        for fname, content in template_files.items():
            write(mod_path / fname, content)
        
        # Configure module
        configurer = CONFIGURERS.get(svc)
        if configurer:
            if svc == "vpc":
                config = configurer(region)
            elif svc == "ec2":
                config = configurer(region, vpc_id_var)
            elif svc == "rds":
                config = configurer(region, vpc_id_var, subnet_ids_var)
            else:
                config = configurer(region)
        else:
            config = {}
        
        # Build module inputs
        inputs = {}
        for key, value in config.items():
            root_var_name = f"{svc}_{key}"
            register_variable(root_var_name)
            tfvars_values[root_var_name] = value
            inputs[key] = f"var.{root_var_name}"
        
        # Handle dependencies
        if svc == "vpc":
            vpc_id_var = "module.vpc.vpc_id"
            subnet_ids_var = "module.vpc.public_subnet_ids"
        elif svc == "ec2" and vpc_id_var:
            inputs["vpc_id"] = vpc_id_var
            if not inputs.get("subnet_id"):
                inputs["subnet_id"] = "module.vpc.public_subnet_ids[0]"
        elif svc == "rds" and vpc_id_var:
            if not inputs.get("vpc_id"):
                inputs["vpc_id"] = vpc_id_var
            if not inputs.get("subnet_ids") or not config.get("subnet_ids"):
                inputs["subnet_ids"] = subnet_ids_var if subnet_ids_var else "module.vpc.private_subnet_ids"
        
        module_blocks.append(root_module_call(svc, inputs))
    
    # Write main.tf
    if module_blocks:
        main_tf_body = "\n\n".join(block.strip() for block in module_blocks)
    else:
        main_tf_body = "# No modules were selected."
    
    write(ROOT / "main.tf", main_tf_body)
    write(ROOT / "variables.tf", render_variables_tf(ordered_variables))
    write(ROOT / "terraform.tfvars", render_tfvars(tfvars_values))
    
    print("\n" + "="*60)
    print("✅ Terraform code generated successfully!")
    print("="*60)
    print(f"\n📁 Output directory: {ROOT.resolve()}")
    print("\n📝 Next steps:")
    print("  1. Review the generated Terraform files")
    print("  2. Update terraform.tfvars with any missing values")
    print("  3. Run: terraform init")
    print("  4. Run: terraform plan")
    print("  5. Run: terraform apply")
    print()


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n\n⚠️  Interrupted by user. Exiting.")
        sys.exit(1)
    except Exception as e:
        print(f"\n❌ Error: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)

