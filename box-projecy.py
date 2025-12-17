#!/usr/bin/env python3
import os
from pathlib import Path

ROOT = Path("box-project")
MODULES_DIR = ROOT / "modules"

SERVICES = {
    "1": "vpc",
    "2": "ec2",
    "3": "rds",
    "4": "s3",
    "5": "alb",
}

def ask(q, default=None):
    v = input(f"{q}{f' [{default}]' if default else ''}: ").strip()
    return v or default

def select_services():
    print("\nSelect services:")
    for k, v in SERVICES.items():
        print(f"{k}. {v.upper()}")
    choices = input("\nEnter choice (e.g. 1,3,5): ").split(",")
    return [SERVICES[c.strip()] for c in choices if c.strip() in SERVICES]

def write(path, content):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content.strip() + "\n")

def provider_tf(region):
    return f"""
terraform {{
  required_providers {{
    aws = {{
      source  = "hashicorp/aws"
      version = "~> 5.0"
    }}
  }}
}}

provider "aws" {{
  region = var.region
}}
"""

def root_variables_tf(region):
    return f"""
variable "region" {{
  default = "{region}"
}}
"""

def vpc_module():
    return {
        "variables.tf": """
variable "cidr" {}
""",
        "main.tf": """
resource "aws_vpc" "this" {
  cidr_block = var.cidr
  tags = { Name = "box-vpc" }
}
""",
        "outputs.tf": """
output "vpc_id" {
  value = aws_vpc.this.id
}
"""
    }

def ec2_module():
    return {
        "variables.tf": """
variable "ami" {}
variable "instance_type" {}
""",
        "main.tf": """
resource "aws_instance" "this" {
  ami           = var.ami
  instance_type = var.instance_type
  tags = { Name = "box-ec2" }
}
""",
        "outputs.tf": """
output "instance_id" {
  value = aws_instance.this.id
}
"""
    }

def rds_module():
    return {
        "variables.tf": """
variable "engine" {}
variable "instance_class" {}
variable "db_name" {}
variable "username" {}
variable "password" {}
""",
        "main.tf": """
resource "aws_db_instance" "this" {
  allocated_storage   = 20
  engine              = var.engine
  instance_class      = var.instance_class
  db_name             = var.db_name
  username            = var.username
  password            = var.password
  skip_final_snapshot = true
}
""",
        "outputs.tf": """
output "endpoint" {
  value = aws_db_instance.this.endpoint
}
"""
    }

def s3_module():
    return {
        "variables.tf": "variable \"bucket\" {}\n",
        "main.tf": """
resource "aws_s3_bucket" "this" {
  bucket = var.bucket
}
""",
        "outputs.tf": """
output "bucket_name" {
  value = aws_s3_bucket.this.bucket
}
"""
    }

def alb_module():
    return {
        "variables.tf": "variable \"name\" {}\n",
        "main.tf": """
resource "aws_lb" "this" {
  name               = var.name
  load_balancer_type = "application"
  internal           = false
}
""",
        "outputs.tf": """
output "dns_name" {
  value = aws_lb.this.dns_name
}
"""
    }

MODULE_DEFS = {
    "vpc": vpc_module,
    "ec2": ec2_module,
    "rds": rds_module,
    "s3": s3_module,
    "alb": alb_module,
}

def root_module_call(service, inputs):
    block = f'module "{service}" {{\n  source = "./modules/{service}"\n'
    for k, v in inputs.items():
        block += f'  {k} = "{v}"\n'
    block += "}\n"
    return block

def main():
    print("\n🚀 Box Terraform Module Generator\n")

    region = ask("AWS region", "ap-south-1")
    services = select_services()

    ROOT.mkdir(exist_ok=True)
    write(ROOT / "provider.tf", provider_tf(region))
    write(ROOT / "variables.tf", root_variables_tf(region))
    write(ROOT / "main.tf", "")

    for svc in services:
        print(f"\n⚙️ Configuring {svc.upper()} module")
        mod_path = MODULES_DIR / svc
        mod_files = MODULE_DEFS[svc]()

        for fname, content in mod_files.items():
            write(mod_path / fname, content)

        inputs = {}
        if svc == "vpc":
            inputs["cidr"] = ask("VPC CIDR", "10.0.0.0/16")
        elif svc == "ec2":
            inputs["ami"] = ask("AMI ID")
            inputs["instance_type"] = ask("Instance type", "t3.micro")
        elif svc == "rds":
            inputs["engine"] = ask("Engine", "mysql")
            inputs["instance_class"] = ask("Instance class", "db.t3.micro")
            inputs["db_name"] = ask("DB name")
            inputs["username"] = ask("DB user")
            inputs["password"] = ask("DB password")
        elif svc == "s3":
            inputs["bucket"] = ask("Bucket name")
        elif svc == "alb":
            inputs["name"] = ask("ALB name", "box-alb")

        write(ROOT / "main.tf", root_module_call(svc, inputs))

    print("\n✅ Module-based Terraform generated in:", ROOT.resolve())

if __name__ == "__main__":
    main()
