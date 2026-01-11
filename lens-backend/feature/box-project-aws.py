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
    ("efs", "Amazon EFS"),
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
  description = "List of subnets with custom CIDRs. Each subnet must have 'cidr' and 'type' fields."
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
  
  # Calculate subnet bits dynamically based on VPC CIDR and required subnet count
  # Find the minimum number of bits needed to accommodate all subnets
  total_subnets = var.num_public_subnets + var.num_private_subnets
  # Use pow(2, i) to find the smallest power of 2 >= total_subnets (min 4)
  subnet_newbits_candidates = [
    for i in range(0, 16) : i
    if pow(2, i) >= max(local.total_subnets, 4)
  ]
  subnet_newbits = length(local.subnet_newbits_candidates) > 0 ? local.subnet_newbits_candidates[0] : 2
  
  public_subnets = local.use_custom_subnets ? [
    for subnet in var.subnets : subnet if subnet.type == "public"
  ] : [
    for i in range(var.num_public_subnets) : {
      cidr = cidrsubnet(var.cidr, local.subnet_newbits, i)
      type = "public"
    }
  ]
  private_subnets = local.use_custom_subnets ? [
    for subnet in var.subnets : subnet if subnet.type == "private"
  ] : [
    for i in range(var.num_private_subnets) : {
      cidr = cidrsubnet(var.cidr, local.subnet_newbits, i + var.num_public_subnets)
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
  # NAT Gateway requires Internet Gateway to be enabled
  count  = var.enable_nat_gateway && var.enable_internet_gateway ? length(local.private_subnets) : 0
  domain = "vpc"
  depends_on = [aws_internet_gateway.this]

  tags = {
    Name = "box-nat-eip-${count.index + 1}"
  }
}

resource "aws_nat_gateway" "this" {
  # NAT Gateway requires Internet Gateway to be enabled
  count         = var.enable_nat_gateway && var.enable_internet_gateway ? length(local.private_subnets) : 0
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
  # Private route table requires both NAT Gateway and Internet Gateway
  count  = var.enable_nat_gateway && var.enable_internet_gateway ? length(local.private_subnets) : 0
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
  # Private route table requires both NAT Gateway and Internet Gateway
  count          = var.enable_nat_gateway && var.enable_internet_gateway ? length(aws_subnet.private) : 0
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
variable "instances" {
  description = "List of EC2 instances to create. SSH key is shared across all instances (see key_name and public_key variables)."
  type = list(object({
    id                  = string
    name                = string
    ami                 = string
    instance_type       = string
    root_volume_size    = number
    root_volume_type    = string
    security_group_name = string
    iam_role            = string
    user_data           = string
    tags                = map(string)
  }))
  default = []
}

variable "subnet_ids" {
  description = "List of subnet IDs to use for instances. Instances will be distributed across these subnets."
  type        = list(string)
  default     = []
}

variable "key_name" {
  description = "Key pair name (optional)"
  type        = string
  default     = ""
}

variable "public_key" {
  description = "Public key content for creating AWS key pair (optional)"
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
  description = "Tags to apply to instances"
  type        = map(string)
  default     = {}
}

variable "vpc_id" {
  description = "VPC ID"
  type        = string
}

variable "additional_volumes" {
  description = "Additional EBS volumes to attach to instances"
  type = list(object({
    id         = string
    name       = string
    size       = number
    type       = string
    iops       = number
    encrypted  = bool
    linked_ec2 = string
  }))
  default = []
}

variable "availability_zone" {
  description = "Availability zone for EBS volumes"
  type        = string
  default     = ""
}
""",
        "main.tf": """
# Create AWS Key Pair from public key (if provided)
resource "aws_key_pair" "this" {
  count      = var.public_key != "" ? 1 : 0
  key_name   = var.key_name != "" ? var.key_name : "box-ec2-key"
  public_key = var.public_key

  tags = {
    Name = var.key_name != "" ? var.key_name : "box-ec2-key"
  }
}

resource "aws_security_group" "ec2" {
  name        = "box-ec2-sg"
  description = "Security group for EC2 instances"
  vpc_id      = var.vpc_id

  # HTTP and HTTPS are allowed from anywhere for web servers
  ingress {
    description = "HTTP"
    from_port   = 80
    to_port     = 80
    protocol    = "tcp"
    cidr_blocks = ["0.0.0.0/0"]
  }

  ingress {
    description = "HTTPS"
    from_port   = 443
    to_port     = 443
    protocol    = "tcp"
    cidr_blocks = ["0.0.0.0/0"]
  }

  # SSH is restricted - add your IP or use a bastion host
  # To enable SSH access, uncomment and specify your IP:
  # ingress {
  #   description = "SSH from specific IP"
  #   from_port   = 22
  #   to_port     = 22
  #   protocol    = "tcp"
  #   cidr_blocks = ["YOUR_IP/32"]  # Replace with your IP
  # }

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

# IAM role for EC2 instances (created per-instance if iam_role is specified)
resource "aws_iam_role" "ec2" {
  for_each = { for inst in var.instances : inst.id => inst if inst.iam_role != "" }

  name = each.value.iam_role

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Action = "sts:AssumeRole"
        Effect = "Allow"
        Principal = {
          Service = "ec2.amazonaws.com"
        }
      }
    ]
  })

  tags = {
    Name = each.value.iam_role
  }
}

resource "aws_iam_instance_profile" "ec2" {
  for_each = { for inst in var.instances : inst.id => inst if inst.iam_role != "" }

  name = "${each.value.iam_role}-profile"
  role = aws_iam_role.ec2[each.key].name
}

# Security groups for instances with custom security group names
resource "aws_security_group" "custom" {
  for_each = { for inst in var.instances : inst.id => inst if inst.security_group_name != "" }

  name        = each.value.security_group_name
  description = "Custom security group for ${each.value.name}"
  vpc_id      = var.vpc_id

  # HTTP and HTTPS are allowed from anywhere for web servers
  ingress {
    description = "HTTP"
    from_port   = 80
    to_port     = 80
    protocol    = "tcp"
    cidr_blocks = ["0.0.0.0/0"]
  }

  ingress {
    description = "HTTPS"
    from_port   = 443
    to_port     = 443
    protocol    = "tcp"
    cidr_blocks = ["0.0.0.0/0"]
  }

  # SSH is restricted - add your IP or use a bastion host
  # To enable SSH access, uncomment and specify your IP:
  # ingress {
  #   description = "SSH from specific IP"
  #   from_port   = 22
  #   to_port     = 22
  #   protocol    = "tcp"
  #   cidr_blocks = ["YOUR_IP/32"]  # Replace with your IP
  # }

  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }

  tags = {
    Name = each.value.security_group_name
  }
}

locals {
  # Create a map of instance index to subnet ID, distributing instances across available subnets
  instance_subnet_map = { 
    for idx, inst in var.instances : 
    inst.id => length(var.subnet_ids) > 0 ? var.subnet_ids[idx % length(var.subnet_ids)] : null
  }
}

# Create multiple EC2 instances using for_each
resource "aws_instance" "this" {
  for_each = { for inst in var.instances : inst.id => inst }

  ami                    = each.value.ami
  instance_type          = each.value.instance_type
  subnet_id              = local.instance_subnet_map[each.key]
  key_name               = var.public_key != "" ? aws_key_pair.this[0].key_name : (var.key_name != "" ? var.key_name : null)
  user_data              = each.value.user_data != "" ? each.value.user_data : (var.user_data != "" ? var.user_data : null)
  iam_instance_profile   = each.value.iam_role != "" ? aws_iam_instance_profile.ec2[each.key].name : null
  vpc_security_group_ids = each.value.security_group_name != "" ? concat([aws_security_group.custom[each.key].id], var.security_group_ids) : concat([aws_security_group.ec2.id], var.security_group_ids)

  root_block_device {
    volume_size = each.value.root_volume_size
    volume_type = each.value.root_volume_type
    encrypted   = true
  }

  tags = merge(
    {
      Name = each.value.name
    },
    each.value.tags,
    var.tags
  )
}

# Additional EBS volumes
resource "aws_ebs_volume" "additional" {
  for_each = { 
    for vol in var.additional_volumes : vol.id => vol 
    # Only create volumes if they're linked to an existing instance (ensures AZ is available)
    if vol.linked_ec2 != "" && contains(keys({ for inst in var.instances : inst.id => inst }), vol.linked_ec2)
  }

  # Use the AZ of the linked instance
  availability_zone = aws_instance.this[each.value.linked_ec2].availability_zone
  size              = each.value.size
  type              = each.value.type
  encrypted         = each.value.encrypted
  iops              = contains(["gp3", "io1", "io2"], each.value.type) ? each.value.iops : null
  throughput        = each.value.type == "gp3" ? 125 : null

  tags = merge(
    {
      Name = each.value.name
    },
    var.tags
  )
}

# Attach additional volumes to EC2 instances
resource "aws_volume_attachment" "additional" {
  for_each = { 
    for vol in var.additional_volumes : vol.id => vol 
    if vol.linked_ec2 != "" && contains(keys({ for inst in var.instances : inst.id => inst }), vol.linked_ec2)
  }

  # Use smarter device naming: /dev/sdf through /dev/sdz (20 devices), then wrap around
  # This avoids collisions better than the previous 11-letter limit
  device_name = "/dev/sd${substr("fghijklmnopqrstuvwxyz", index([for v in var.additional_volumes : v.id], each.key) % 21, 1)}"
  volume_id   = aws_ebs_volume.additional[each.key].id
  instance_id = aws_instance.this[each.value.linked_ec2].id
}
""",
        "outputs.tf": """
output "instance_ids" {
  description = "Map of EC2 instance IDs"
  value       = { for k, v in aws_instance.this : k => v.id }
}

output "instance_public_ips" {
  description = "Map of public IP addresses"
  value       = { for k, v in aws_instance.this : k => v.public_ip }
}

output "instance_private_ips" {
  description = "Map of private IP addresses"
  value       = { for k, v in aws_instance.this : k => v.private_ip }
}

output "security_group_id" {
  description = "Security group ID"
  value       = aws_security_group.ec2.id
}

output "key_pair_name" {
  description = "Key pair name used for SSH access"
  value       = var.public_key != "" ? aws_key_pair.this[0].key_name : var.key_name
}

output "key_pair_fingerprint" {
  description = "Key pair fingerprint (if created)"
  value       = var.public_key != "" ? aws_key_pair.this[0].fingerprint : ""
}

output "instances_summary" {
  description = "Summary of all created instances"
  value = { for k, v in aws_instance.this : k => {
    id         = v.id
    name       = v.tags["Name"]
    public_ip  = v.public_ip
    private_ip = v.private_ip
    type       = v.instance_type
  }}
}

output "additional_volume_ids" {
  description = "Map of additional EBS volume IDs"
  value       = { for k, v in aws_ebs_volume.additional : k => v.id }
}

output "additional_volumes_summary" {
  description = "Summary of additional volumes"
  value = { for k, v in aws_ebs_volume.additional : k => {
    id        = v.id
    name      = v.tags["Name"]
    size      = v.size
    type      = v.type
    encrypted = v.encrypted
  }}
}

output "volume_attachments" {
  description = "Volume attachment details"
  value = { for k, v in aws_volume_attachment.additional : k => {
    volume_id   = v.volume_id
    instance_id = v.instance_id
    device_name = v.device_name
  }}
}
"""
    }


def s3_module() -> Dict[str, str]:
    """S3 Terraform module with support for multiple buckets"""
    return {
        "variables.tf": """
variable "buckets" {
  description = "List of S3 buckets to create"
  type = list(object({
    bucket_name                = string
    versioning                 = bool
    encryption                 = bool
    block_public_access        = bool
    storage_class              = string
    enable_logging             = bool
    lifecycle_ia_days          = number
    lifecycle_glacier_days     = number
    lifecycle_expiration_days  = number
    enable_cors                = bool
    tags                       = map(string)
  }))
  default = []
}

variable "tags" {
  description = "Tags to apply to buckets"
  type        = map(string)
  default     = {}
}
""",
        "main.tf": """
# Create multiple S3 buckets using for_each
resource "aws_s3_bucket" "this" {
  for_each = { for idx, bucket in var.buckets : bucket.bucket_name => bucket if bucket.bucket_name != "" }

  bucket = each.value.bucket_name

  tags = merge(
    {
      Name         = each.value.bucket_name
      StorageClass = each.value.storage_class
    },
    each.value.tags,
    var.tags
  )
}

resource "aws_s3_bucket_versioning" "this" {
  for_each = aws_s3_bucket.this

  bucket = each.value.id
  versioning_configuration {
    status = var.buckets[index(var.buckets.*.bucket_name, each.key)].versioning ? "Enabled" : "Disabled"
  }
}

resource "aws_s3_bucket_server_side_encryption_configuration" "this" {
  for_each = { for k, v in aws_s3_bucket.this : k => v if var.buckets[index(var.buckets.*.bucket_name, k)].encryption }

  bucket = each.value.id

  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
    # bucket_key_enabled only works with aws:kms, not AES256
    # bucket_key_enabled = true
  }
}

resource "aws_s3_bucket_public_access_block" "this" {
  for_each = aws_s3_bucket.this

  bucket = each.value.id

  block_public_acls       = var.buckets[index(var.buckets.*.bucket_name, each.key)].block_public_access
  block_public_policy     = var.buckets[index(var.buckets.*.bucket_name, each.key)].block_public_access
  ignore_public_acls      = var.buckets[index(var.buckets.*.bucket_name, each.key)].block_public_access
  restrict_public_buckets = var.buckets[index(var.buckets.*.bucket_name, each.key)].block_public_access
}

# Lifecycle configuration for buckets with lifecycle rules
resource "aws_s3_bucket_lifecycle_configuration" "this" {
  for_each = { 
    for k, v in aws_s3_bucket.this : k => v 
    if (
      var.buckets[index(var.buckets.*.bucket_name, k)].lifecycle_ia_days != null ||
      var.buckets[index(var.buckets.*.bucket_name, k)].lifecycle_glacier_days != null ||
      var.buckets[index(var.buckets.*.bucket_name, k)].lifecycle_expiration_days != null
    )
  }

  bucket = each.value.id

  rule {
    id     = "lifecycle-rule"
    status = "Enabled"

    # Required: filter block (empty prefix applies to all objects)
    filter {
      prefix = ""
    }

    dynamic "transition" {
      for_each = var.buckets[index(var.buckets.*.bucket_name, each.key)].lifecycle_ia_days != null ? [1] : []
      content {
        days          = var.buckets[index(var.buckets.*.bucket_name, each.key)].lifecycle_ia_days
        storage_class = "STANDARD_IA"
      }
    }

    dynamic "transition" {
      for_each = var.buckets[index(var.buckets.*.bucket_name, each.key)].lifecycle_glacier_days != null ? [1] : []
      content {
        days          = var.buckets[index(var.buckets.*.bucket_name, each.key)].lifecycle_glacier_days
        storage_class = "GLACIER"
      }
    }

    dynamic "expiration" {
      for_each = var.buckets[index(var.buckets.*.bucket_name, each.key)].lifecycle_expiration_days != null ? [1] : []
      content {
        days = var.buckets[index(var.buckets.*.bucket_name, each.key)].lifecycle_expiration_days
      }
    }
  }
}

# CORS configuration for buckets with CORS enabled
resource "aws_s3_bucket_cors_configuration" "this" {
  for_each = { 
    for k, v in aws_s3_bucket.this : k => v 
    if var.buckets[index(var.buckets.*.bucket_name, k)].enable_cors
  }

  bucket = each.value.id

  cors_rule {
    allowed_headers = ["*"]
    allowed_methods = ["GET", "PUT", "POST", "DELETE", "HEAD"]
    allowed_origins = ["*"]
    expose_headers  = ["ETag"]
    max_age_seconds = 3000
  }
}

# Create log buckets for buckets with logging enabled
resource "aws_s3_bucket" "logs" {
  for_each = { 
    for k, v in aws_s3_bucket.this : k => v 
    if var.buckets[index(var.buckets.*.bucket_name, k)].enable_logging
  }

  bucket = "${each.value.id}-logs"

  tags = merge(
    {
      Name = "${each.value.id}-logs"
      Purpose = "Logs for ${each.value.id}"
    },
    var.tags
  )
}

# Block public access for log buckets
resource "aws_s3_bucket_public_access_block" "logs" {
  for_each = aws_s3_bucket.logs

  bucket = each.value.id

  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

# Logging configuration for buckets with logging enabled
resource "aws_s3_bucket_logging" "this" {
  for_each = { 
    for k, v in aws_s3_bucket.this : k => v 
    if var.buckets[index(var.buckets.*.bucket_name, k)].enable_logging
  }

  bucket = each.value.id

  target_bucket = aws_s3_bucket.logs[each.key].id
  target_prefix = "log/"

  depends_on = [aws_s3_bucket.logs]
}
""",
        "outputs.tf": """
output "bucket_ids" {
  description = "Map of S3 bucket IDs"
  value       = { for k, v in aws_s3_bucket.this : k => v.id }
}

output "bucket_arns" {
  description = "Map of S3 bucket ARNs"
  value       = { for k, v in aws_s3_bucket.this : k => v.arn }
}

output "bucket_domain_names" {
  description = "Map of S3 bucket domain names"
  value       = { for k, v in aws_s3_bucket.this : k => v.bucket_domain_name }
}

output "buckets_summary" {
  description = "Summary of all created buckets"
  value = { for k, v in aws_s3_bucket.this : k => {
    id          = v.id
    arn         = v.arn
    domain_name = v.bucket_domain_name
  }}
}
"""
    }


def rds_module() -> Dict[str, str]:
    """RDS Terraform module with support for multiple databases"""
    return {
        "variables.tf": """
variable "databases" {
  description = "List of RDS databases to create. Note: Contains sensitive data (passwords)."
  type = list(object({
    identifier              = string
    engine                  = string
    instance_class          = string
    allocated_storage       = number
    storage_type            = string
    iops                    = optional(number)  # Required for io1/io2, optional for gp3
    db_name                 = string
    username                = string
    password                = string
    backup_retention_period = number
    security_group_name     = string
    publicly_accessible     = bool
    multi_az                = bool
    backup_window           = string
    maintenance_window      = string
    tags                    = map(string)
  }))
  default   = []
  # Note: Cannot mark as sensitive=true because it's used in for_each
  # Passwords should be managed via tfvars marked as sensitive or environment variables
}

variable "subnet_ids" {
  description = "Subnet IDs for RDS"
  type        = list(string)
}

variable "vpc_id" {
  description = "VPC ID"
  type        = string
}

variable "skip_final_snapshot" {
  description = "Skip final snapshot on deletion"
  type        = bool
  default     = true
}

variable "tags" {
  description = "Tags to apply to RDS instances"
  type        = map(string)
  default     = {}
}
""",
        "main.tf": """
# Shared subnet group for all RDS instances
resource "aws_db_subnet_group" "this" {
  name       = "rds-subnet-group"
  subnet_ids = var.subnet_ids

  tags = merge(
    {
      Name = "rds-subnet-group"
    },
    var.tags
  )
}

data "aws_vpc" "this" {
  id = var.vpc_id
}

# Security group for all RDS instances (default)
resource "aws_security_group" "rds" {
  name        = "rds-security-group"
  description = "Security group for RDS instances"
  vpc_id      = var.vpc_id

  ingress {
    description = "MySQL"
    from_port   = 3306
    to_port     = 3306
    protocol    = "tcp"
    cidr_blocks = [data.aws_vpc.this.cidr_block]
  }

  ingress {
    description = "PostgreSQL"
    from_port   = 5432
    to_port     = 5432
    protocol    = "tcp"
    cidr_blocks = [data.aws_vpc.this.cidr_block]
  }

  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }

  tags = merge(
    {
      Name = "rds-security-group"
    },
    var.tags
  )
}

# Custom security groups for databases with custom security group names
resource "aws_security_group" "custom" {
  for_each = { for db in var.databases : db.identifier => db if db.security_group_name != "" }

  name        = each.value.security_group_name
  description = "Custom security group for ${each.value.identifier}"
  vpc_id      = var.vpc_id

  ingress {
    description = "MySQL"
    from_port   = 3306
    to_port     = 3306
    protocol    = "tcp"
    cidr_blocks = [data.aws_vpc.this.cidr_block]
  }

  ingress {
    description = "PostgreSQL"
    from_port   = 5432
    to_port     = 5432
    protocol    = "tcp"
    cidr_blocks = [data.aws_vpc.this.cidr_block]
  }

  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }

  tags = {
    Name = each.value.security_group_name
  }
}

# Create multiple RDS instances using for_each
resource "aws_db_instance" "this" {
  for_each = { for db in var.databases : db.identifier => db }

  identifier                  = each.value.identifier
  engine                      = each.value.engine
  instance_class              = each.value.instance_class
  allocated_storage           = each.value.allocated_storage
  storage_type                = each.value.storage_type
  iops                        = can(each.value.iops) ? each.value.iops : (contains(["io1", "io2", "gp3"], each.value.storage_type) ? 3000 : null)
  db_name                     = each.value.db_name != "" ? each.value.db_name : null
  username                    = each.value.username
  password                    = each.value.password
  db_subnet_group_name        = aws_db_subnet_group.this.name
  vpc_security_group_ids      = each.value.security_group_name != "" ? [aws_security_group.custom[each.key].id] : [aws_security_group.rds.id]
  backup_retention_period     = each.value.backup_retention_period
  skip_final_snapshot         = var.skip_final_snapshot
  publicly_accessible         = each.value.publicly_accessible
  multi_az                    = each.value.multi_az

  tags = merge(
    {
      Name = each.value.identifier
    },
    each.value.tags,
    var.tags
  )
}
""",
        "outputs.tf": """
output "db_instance_ids" {
  description = "Map of RDS instance IDs"
  value       = { for k, v in aws_db_instance.this : k => v.id }
}

output "db_instance_endpoints" {
  description = "Map of RDS instance endpoints"
  value       = { for k, v in aws_db_instance.this : k => v.endpoint }
}

output "db_instance_arns" {
  description = "Map of RDS instance ARNs"
  value       = { for k, v in aws_db_instance.this : k => v.arn }
}

output "databases_summary" {
  description = "Summary of all created databases"
  value = { for k, v in aws_db_instance.this : k => {
    id       = v.id
    endpoint = v.endpoint
    engine   = v.engine
    port     = v.port
  }}
}
"""
    }


def ebs_module() -> Dict[str, str]:
    """EBS Terraform module - supports multiple volumes with EC2 attachment"""
    return {
        "variables.tf": """
variable "volumes" {
  description = "List of EBS volumes to create"
  type = list(object({
    id         = string
    name       = string
    size       = number
    type       = string
    iops       = number
    encrypted  = bool
    linked_ec2 = string  # EC2 instance ID to attach to (empty for standalone)
  }))
  default = []
}

variable "availability_zone" {
  description = "Availability zone for the volumes"
  type        = string
}

variable "ec2_instance_ids" {
  description = "Map of EC2 instance IDs for attachment"
  type        = map(string)
  default     = {}
}

variable "kms_key_id" {
  description = "KMS key ID for encryption (optional)"
  type        = string
  default     = ""
}

variable "tags" {
  description = "Tags to apply to EBS volumes"
  type        = map(string)
  default     = {}
}
""",
        "main.tf": """
# Create multiple EBS volumes using for_each
resource "aws_ebs_volume" "this" {
  for_each = { for vol in var.volumes : vol.id => vol }

  availability_zone = var.availability_zone
  size              = each.value.size
  type              = each.value.type
  encrypted         = each.value.encrypted
  iops              = contains(["gp3", "io1", "io2"], each.value.type) ? each.value.iops : null
  throughput        = each.value.type == "gp3" ? 125 : null
  kms_key_id        = each.value.encrypted && var.kms_key_id != "" ? var.kms_key_id : null

  tags = merge(
    {
      Name = each.value.name
    },
    var.tags
  )
}

# Attach volumes to EC2 instances (if linked_ec2 is specified)
resource "aws_volume_attachment" "this" {
  for_each = { 
    for vol in var.volumes : vol.id => vol 
    if vol.linked_ec2 != "" && lookup(var.ec2_instance_ids, vol.linked_ec2, "") != ""
  }

  device_name = "/dev/sd${substr("fghijklmnop", index([for v in var.volumes : v.id if v.linked_ec2 != ""], each.key), 1)}"
  volume_id   = aws_ebs_volume.this[each.key].id
  instance_id = var.ec2_instance_ids[each.value.linked_ec2]
}
""",
        "outputs.tf": """
output "volume_ids" {
  description = "Map of EBS volume IDs"
  value       = { for k, v in aws_ebs_volume.this : k => v.id }
}

output "volume_arns" {
  description = "Map of EBS volume ARNs"
  value       = { for k, v in aws_ebs_volume.this : k => v.arn }
}

output "volumes_summary" {
  description = "Summary of all created volumes"
  value = { for k, v in aws_ebs_volume.this : k => {
    id        = v.id
    name      = v.tags["Name"]
    size      = v.size
    type      = v.type
    encrypted = v.encrypted
  }}
}

output "attachments" {
  description = "Volume attachments to EC2 instances"
  value       = { for k, v in aws_volume_attachment.this : k => {
    volume_id   = v.volume_id
    instance_id = v.instance_id
    device_name = v.device_name
  }}
}
"""
    }


def efs_module() -> Dict[str, str]:
    """EFS Terraform module with support for multiple file systems"""
    return {
        "variables.tf": """
variable "filesystems" {
  description = "List of EFS file systems to create. Encryption uses AWS-managed keys by default (kms_key_id is optional)."
  type = list(object({
    name                            = string
    performance_mode                = string
    throughput_mode                 = string
    provisioned_throughput_in_mibps = optional(number)  # Required when throughput_mode is "provisioned"
    storage_class                   = string
    encrypted                       = bool
    enable_backup                   = bool
    transition_to_ia                = number
    security_group_name             = string
    tags                            = map(string)
    # Note: kms_key_id is optional and handled via lookup() in main.tf
  }))
  default = []
}

variable "subnet_ids" {
  description = "List of subnet IDs for mount targets"
  type        = list(string)
  default     = []
}

variable "vpc_id" {
  description = "VPC ID for creating security group"
  type        = string
  default     = ""
}

variable "tags" {
  description = "Tags to apply to EFS resources"
  type        = map(string)
  default     = {}
}
""",
        "main.tf": """
# Get VPC CIDR block for security group rules
data "aws_vpc" "this" {
  id = var.vpc_id
}

# Security Group for all EFS file systems (default)
resource "aws_security_group" "efs" {
  name        = "efs-security-group"
  description = "Security group for EFS mount targets"
  vpc_id      = var.vpc_id

  ingress {
    description = "NFS from VPC only"
    from_port   = 2049
    to_port     = 2049
    protocol    = "tcp"
    cidr_blocks = [data.aws_vpc.this.cidr_block]
  }

  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }

  tags = merge(
    {
      Name = "efs-security-group"
    },
    var.tags
  )
}

# Custom security groups for file systems with custom security group names
resource "aws_security_group" "custom" {
  for_each = { for fs in var.filesystems : fs.name => fs if fs.security_group_name != "" }

  name        = each.value.security_group_name
  description = "Custom security group for ${each.value.name}"
  vpc_id      = var.vpc_id

  ingress {
    description = "NFS from VPC only"
    from_port   = 2049
    to_port     = 2049
    protocol    = "tcp"
    cidr_blocks = [data.aws_vpc.this.cidr_block]
  }

  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }

  tags = {
    Name = each.value.security_group_name
  }
}

# Create multiple EFS file systems using for_each
resource "aws_efs_file_system" "this" {
  for_each = { for fs in var.filesystems : fs.name => fs }

  creation_token                  = each.value.name
  performance_mode                = each.value.performance_mode
  throughput_mode                 = each.value.throughput_mode
  provisioned_throughput_in_mibps = can(each.value.provisioned_throughput_in_mibps) && each.value.throughput_mode == "provisioned" ? each.value.provisioned_throughput_in_mibps : null
  encrypted                       = each.value.encrypted
  # KMS key is optional - if not provided, AWS-managed encryption is used
  kms_key_id                      = each.value.encrypted && lookup(each.value, "kms_key_id", null) != null && lookup(each.value, "kms_key_id", "") != "" ? lookup(each.value, "kms_key_id", null) : null

  dynamic "lifecycle_policy" {
    for_each = each.value.transition_to_ia != null && each.value.transition_to_ia > 0 ? [1] : []
    content {
      transition_to_ia = "AFTER_${each.value.transition_to_ia}_DAYS"
    }
  }

  tags = merge(
    {
      Name = each.value.name
    },
    each.value.tags,
    var.tags
  )
}

# Mount Targets for each file system (one per subnet)
# Use locals to create static keys, then reference subnet IDs dynamically
locals {
  mount_targets = flatten([
    for fs_idx, fs in var.filesystems : [
      for subnet_idx in range(length(var.subnet_ids)) : {
        key                 = "${fs.name}-${subnet_idx}"
        file_system_name    = fs.name
        subnet_idx          = subnet_idx
        security_group_name = fs.security_group_name
      }
    ]
  ])
}

resource "aws_efs_mount_target" "this" {
  for_each = { for mt in local.mount_targets : mt.key => mt }
  
  file_system_id  = aws_efs_file_system.this[each.value.file_system_name].id
  subnet_id       = var.subnet_ids[each.value.subnet_idx]
  security_groups = each.value.security_group_name != "" ? [aws_security_group.custom[each.value.file_system_name].id] : [aws_security_group.efs.id]
}

# Backup Policy for each file system (if enabled)
resource "aws_efs_backup_policy" "this" {
  for_each = { for fs in var.filesystems : fs.name => fs if fs.enable_backup }
  
  file_system_id = aws_efs_file_system.this[each.key].id

  backup_policy {
    status = "ENABLED"
  }
}

# Access Point for each file system
resource "aws_efs_access_point" "this" {
  for_each = aws_efs_file_system.this

  file_system_id = each.value.id
  
  posix_user {
    gid = 1000
    uid = 1000
  }
  
  root_directory {
    path = "/data"
    creation_info {
      owner_gid   = 1000
      owner_uid   = 1000
      permissions = "755"
    }
  }

  tags = merge(
    {
      Name = "${each.key}-access-point"
    },
    var.tags
  )
}
""",
        "outputs.tf": """
output "file_system_ids" {
  description = "Map of EFS file system IDs"
  value       = { for k, v in aws_efs_file_system.this : k => v.id }
}

output "file_system_arns" {
  description = "Map of EFS file system ARNs"
  value       = { for k, v in aws_efs_file_system.this : k => v.arn }
}

output "file_system_dns_names" {
  description = "Map of EFS file system DNS names"
  value       = { for k, v in aws_efs_file_system.this : k => v.dns_name }
}

output "access_point_ids" {
  description = "Map of EFS access point IDs"
  value       = { for k, v in aws_efs_access_point.this : k => v.id }
}

output "security_group_id" {
  description = "Security group ID for EFS"
  value       = aws_security_group.efs.id
}

output "filesystems_summary" {
  description = "Summary of all created file systems"
  value = { for k, v in aws_efs_file_system.this : k => {
    id       = v.id
    arn      = v.arn
    dns_name = v.dns_name
  }}
}

output "mount_commands" {
  description = "Commands to mount EFS on EC2 instances"
  value = { for k, v in aws_efs_file_system.this : k => "sudo mount -t efs -o tls ${v.id}:/ /mnt/${k}" }
}
"""
    }


SERVICE_TEMPLATES = {
    "vpc": vpc_module,
    "ec2": ec2_module,
    "s3": s3_module,
    "rds": rds_module,
    "ebs": ebs_module,
    "efs": efs_module,
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
    """Render terraform.tfvars file with proper HCL formatting"""
    
    def format_value(value, indent=0):
        """Format a value as HCL"""
        spaces = "  " * indent
        
        if value is None:
            return "null"
        elif isinstance(value, bool):
            return str(value).lower()
        elif isinstance(value, (int, float)):
            return str(value)
        elif isinstance(value, str):
            # Check if it contains newlines (multiline string)
            if '\n' in value:
                lines = value.split('\n')
                return '<<-EOF\n' + '\n'.join(f'{spaces}  {line}' for line in lines) + f'\n{spaces}EOF'
            else:
                # Check if it looks like JSON (starts with { or [)
                if value.strip().startswith(('{', '[')):
                    try:
                        # Try to parse as JSON and convert to HCL
                        parsed = json.loads(value)
                        return format_value(parsed, indent)
                    except:
                        pass
                return json.dumps(value)
        elif isinstance(value, dict):
            # Format as HCL map
            items = []
            for k, v in value.items():
                # Skip empty KMS key IDs and other optional advanced fields
                if k == 'kms_key_id' and (v == "" or v is None):
                    continue
                formatted_v = format_value(v, indent + 1)
                items.append(f'{spaces}  {k} = {formatted_v}')
            return '{\n' + '\n'.join(items) + f'\n{spaces}}}'
        elif isinstance(value, list):
            if not value:
                return '[]'
            
            # Check if it's a list of objects/dicts
            if isinstance(value[0], dict):
                items = []
                for item in value:
                    obj_items = []
                    for k, v in item.items():
                        # Skip empty/null KMS key IDs and other optional advanced fields
                        if k == 'kms_key_id' and (not v or v == "" or v is None):
                            continue
                        formatted_v = format_value(v, indent + 1)
                        obj_items.append(f'{spaces}    {k} = {formatted_v}')
                    items.append(f'{spaces}  {{\n' + '\n'.join(obj_items) + f'\n{spaces}  }}')
                return '[\n' + ',\n'.join(items) + f'\n{spaces}]'
            else:
                # Simple list
                formatted_items = [json.dumps(item) if isinstance(item, str) else str(item) for item in value]
                return '[' + ', '.join(formatted_items) + ']'
        else:
            return json.dumps(str(value))
    
    def add_section(lines, title, keys, values):
        """Add a commented section"""
        lines.append("")
        lines.append("#" * 50)
        lines.append(f"# {title}")
        lines.append("#" * 50)
        lines.append("")
        
        for key in keys:
            if key in values and values[key] is not None:
                # Skip empty KMS key IDs and other sensitive/optional advanced fields
                if key == 'kms_key_id' and (values[key] == "" or values[key] is None):
                    continue
                    
                formatted = format_value(values[key])
                
                # Handle long single-line values
                if isinstance(values[key], str) and '\n' not in values[key] and len(formatted) > 80:
                    lines.append(f"{key} = \\")
                    lines.append(f"  {formatted}")
                else:
                    lines.append(f"{key} = {formatted}")
                lines.append("")
    
    lines = []
    
    # Global section
    if 'region' in values:
        add_section(lines, "Global Configuration", ['region'], values)
    
    # VPC section
    vpc_keys = [k for k in values.keys() if k.startswith('vpc_')]
    if vpc_keys:
        add_section(lines, "VPC Configuration", sorted(vpc_keys), values)
    
    # EC2 section
    ec2_keys = [k for k in values.keys() if k.startswith('ec2_')]
    if ec2_keys:
        # Separate EC2 instances and volumes
        instance_keys = ['ec2_key_name', 'ec2_public_key', 'ec2_instances']
        volume_keys = ['ec2_additional_volumes']
        
        if any(k in values for k in instance_keys):
            add_section(lines, "EC2 Configuration", 
                       [k for k in instance_keys if k in values], values)
        
        if any(k in values for k in volume_keys):
            add_section(lines, "EC2 Additional Volumes", 
                       [k for k in volume_keys if k in values], values)
    
    # S3 section
    s3_keys = [k for k in values.keys() if k.startswith('s3_')]
    if s3_keys:
        add_section(lines, "S3 Buckets", sorted(s3_keys), values)
    
    # RDS section
    rds_keys = [k for k in values.keys() if k.startswith('rds_')]
    if rds_keys:
        add_section(lines, "RDS Configuration", sorted(rds_keys), values)
    
    # EFS section
    efs_keys = [k for k in values.keys() if k.startswith('efs_')]
    if efs_keys:
        add_section(lines, "EFS Configuration", sorted(efs_keys), values)
    
    # EBS section (if any standalone)
    ebs_keys = [k for k in values.keys() if k.startswith('ebs_') and not k.startswith('ec2_')]
    if ebs_keys:
        add_section(lines, "EBS Configuration", sorted(ebs_keys), values)
    
    # Any other keys not covered
    covered_prefixes = ['region', 'vpc_', 'ec2_', 's3_', 'rds_', 'efs_', 'ebs_']
    other_keys = [k for k in values.keys() if not any(k.startswith(p) or k == p for p in covered_prefixes)]
    if other_keys:
        add_section(lines, "Additional Configuration", sorted(other_keys), values)
    
    return '\n'.join(lines).strip() + '\n'


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

