#!/usr/bin/env python3
import json
import textwrap
from pathlib import Path

ROOT = Path("box-project")
MODULES_DIR = ROOT / "modules"

CLOUD_NAMES = {
    "aws": "AWS",
    "gcp": "GCP",
}

TOP_SERVICES = {
    "aws": [
        ("vpc", "Amazon VPC"),
        ("ec2", "Amazon EC2"),
        ("s3", "Amazon S3"),
        ("rds", "Amazon RDS"),
        ("lambda", "AWS Lambda"),
        ("cloudwatch", "Amazon CloudWatch"),
        ("iam", "AWS Identity and Access Management"),
        ("sns", "Amazon SNS"),
        ("sqs", "Amazon SQS"),
        ("ecs", "Amazon ECS"),
        ("eks", "Amazon EKS"),
        ("cloudfront", "Amazon CloudFront"),
        ("route53", "Amazon Route 53"),
        ("cloudformation", "AWS CloudFormation"),
        ("dynamodb", "Amazon DynamoDB"),
        ("redshift", "Amazon Redshift"),
        ("elasticache", "Amazon ElastiCache"),
        ("kinesis", "Amazon Kinesis"),
        ("glue", "AWS Glue"),
        ("athena", "Amazon Athena"),
        ("emr", "Amazon EMR"),
        ("apigateway", "Amazon API Gateway"),
        ("ssm", "AWS Systems Manager"),
        ("secretsmanager", "AWS Secrets Manager"),
        ("acm", "AWS Certificate Manager"),
        ("waf", "AWS WAF"),
        ("guardduty", "Amazon GuardDuty"),
        ("config", "AWS Config"),
        ("backup", "AWS Backup"),
        ("organizations", "AWS Organizations"),
    ],
    "gcp": [
        ("vpc", "VPC (Virtual Private Cloud)"),
        ("compute", "Compute Engine"),
        ("storage", "Cloud Storage"),
        ("cloudsql", "Cloud SQL"),
        ("functions", "Cloud Functions"),
        ("cloudrun", "Cloud Run"),
        ("pubsub", "Cloud Pub/Sub"),
        ("bigquery", "BigQuery"),
        ("gke", "Google Kubernetes Engine"),
        ("spanner", "Cloud Spanner"),
        ("firestore", "Cloud Firestore"),
        ("memorystore", "Memorystore"),
        ("cloudcdn", "Cloud CDN"),
        ("iam", "Cloud IAM"),
        ("logging", "Cloud Logging"),
        ("monitoring", "Cloud Monitoring"),
        ("cloudbuild", "Cloud Build"),
        ("deploymentmanager", "Cloud Deployment Manager"),
        ("clouddns", "Cloud DNS"),
        ("cloudarmor", "Cloud Armor"),
        ("vertexai", "Vertex AI"),
        ("dataflow", "Dataflow"),
        ("dataproc", "Dataproc"),
        ("cloudcomposer", "Cloud Composer"),
        ("secretmanager", "Secret Manager"),
        ("scheduler", "Cloud Scheduler"),
        ("cloudtasks", "Cloud Tasks"),
        ("firebasehosting", "Firebase Hosting"),
        ("sourcerepo", "Cloud Source Repositories"),
        ("anthos", "Anthos"),
    ],
}

def ask(q, default=None):
    v = input(f"{q}{f' [{default}]' if default else ''}: ").strip()
    return v or default

def ask_cloud_provider():
    options = "/".join(name.upper() for name in CLOUD_NAMES)
    while True:
        choice = ask(f"Select cloud provider ({options})").lower()
        if choice in CLOUD_NAMES:
            return choice
        print("Please enter either 'aws' or 'gcp'.")

def select_services(cloud):
    services = TOP_SERVICES[cloud]
    print(f"\nTop {len(services)} {CLOUD_NAMES[cloud]} services:")
    for idx, (_, label) in enumerate(services, start=1):
        print(f"{idx:2}. {label}")
    choices = input("\nEnter choice numbers (e.g. 1,3,5): ").split(",")
    selected = []
    for item in choices:
        item = item.strip()
        if not item.isdigit():
            continue
        idx = int(item)
        if 1 <= idx <= len(services):
            selected.append(services[idx - 1][0])
    return selected

def write(path, content):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content.strip() + "\n")

def coerce_tfvars_value(value, meta):
    value_type = (meta or {}).get("value_type", "string")
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

def module_template(variable_blocks, main_body, outputs):
    def normalize(block):
        return textwrap.dedent(block).strip()

    variables_tf = "\n\n".join(normalize(block) for block in variable_blocks if block.strip())
    outputs_tf = "\n\n".join(
        normalize(f'''
output "{name}" {{
  value = {expr}
}}
''')
        for name, expr in outputs
    ) if outputs else ""

    return {
        "variables.tf": variables_tf,
        "main.tf": normalize(main_body),
        "outputs.tf": outputs_tf,
    }

def provider_tf(cloud):
    if cloud == "aws":
        return """
terraform {
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.0"
    }
  }
}

provider "aws" {
  region = var.region
}
"""
    return """
terraform {
  required_providers {
    google = {
      source  = "hashicorp/google"
      version = "~> 5.0"
    }
  }
}

provider "google" {
  project = var.project
  region  = var.region
}
"""

def render_variables_tf(variable_names):
    blocks = [f'variable "{name}" {{\n}}\n' for name in variable_names]
    return "\n".join(blocks)

def render_tfvars(values):
    lines = []
    for key, value in values.items():
        safe_value = "" if value is None else value
        lines.append(f"{key} = {json.dumps(safe_value)}")
    return "\n".join(lines)

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

def lambda_module():
    return module_template(
        [
            'variable "function_name" {}',
            'variable "filename" {}',
            'variable "handler" {}',
            'variable "runtime" {}',
            'variable "role_arn" {}',
            """
variable "timeout" {
  type    = number
  default = 3
}
""",
        ],
        """
resource "aws_lambda_function" "this" {
  function_name    = var.function_name
  role             = var.role_arn
  handler          = var.handler
  runtime          = var.runtime
  filename         = var.filename
  source_code_hash = filebase64sha256(var.filename)
  timeout          = var.timeout
}
""",
        [("function_arn", "aws_lambda_function.this.arn")],
    )

def cloudwatch_module():
    return module_template(
        [
            'variable "log_group_name" {}',
            """
variable "retention_in_days" {
  type    = number
  default = 7
}
""",
        ],
        """
resource "aws_cloudwatch_log_group" "this" {
  name              = var.log_group_name
  retention_in_days = var.retention_in_days
}
""",
        [("log_group_name", "aws_cloudwatch_log_group.this.name")],
    )

def iam_module():
    return module_template(
        [
            'variable "role_name" {}',
            'variable "assume_role_policy" {}',
            """
variable "description" {
  default = ""
}
""",
        ],
        """
resource "aws_iam_role" "this" {
  name               = var.role_name
  assume_role_policy = var.assume_role_policy
  description        = var.description
}
""",
        [("role_arn", "aws_iam_role.this.arn")],
    )

def sns_module():
    return module_template(
        [
            'variable "topic_name" {}',
            """
variable "display_name" {
  default = ""
}
""",
        ],
        """
resource "aws_sns_topic" "this" {
  name         = var.topic_name
  display_name = var.display_name
}
""",
        [("topic_arn", "aws_sns_topic.this.arn")],
    )

def sqs_module():
    return module_template(
        [
            'variable "queue_name" {}',
            """
variable "visibility_timeout_seconds" {
  type    = number
  default = 30
}
""",
        ],
        """
resource "aws_sqs_queue" "this" {
  name                        = var.queue_name
  visibility_timeout_seconds  = var.visibility_timeout_seconds
}
""",
        [("queue_url", "aws_sqs_queue.this.url")],
    )

def ecs_module():
    return module_template(
        ['variable "cluster_name" {}'],
        """
resource "aws_ecs_cluster" "this" {
  name = var.cluster_name
}
""",
        [("cluster_arn", "aws_ecs_cluster.this.arn")],
    )

def eks_module():
    return module_template(
        [
            'variable "cluster_name" {}',
            'variable "role_arn" {}',
            """
variable "subnet_ids" {
  type = list(string)
}
""",
            """
variable "version" {
  default = "1.28"
}
""",
        ],
        """
resource "aws_eks_cluster" "this" {
  name     = var.cluster_name
  role_arn = var.role_arn
  version  = var.version

  vpc_config {
    subnet_ids = var.subnet_ids
  }
}
""",
        [("cluster_endpoint", "aws_eks_cluster.this.endpoint")],
    )

def cloudfront_module():
    return module_template(
        ['variable "comment" {}'],
        """
resource "aws_cloudfront_origin_access_identity" "this" {
  comment = var.comment
}
""",
        [("cloudfront_access_identity_path", "aws_cloudfront_origin_access_identity.this.cloudfront_access_identity_path")],
    )

def route53_module():
    return module_template(
        ['variable "zone_name" {}'],
        """
resource "aws_route53_zone" "this" {
  name = var.zone_name
}
""",
        [("zone_id", "aws_route53_zone.this.zone_id")],
    )

def cloudformation_module():
    return module_template(
        ['variable "stack_name" {}', 'variable "template_body" {}'],
        """
resource "aws_cloudformation_stack" "this" {
  name          = var.stack_name
  template_body = var.template_body
}
""",
        [("stack_id", "aws_cloudformation_stack.this.id")],
    )

def dynamodb_module():
    return module_template(
        [
            'variable "table_name" {}',
            'variable "hash_key" {}',
            """
variable "attribute_type" {
  default = "S"
}
""",
            """
variable "billing_mode" {
  default = "PAY_PER_REQUEST"
}
""",
        ],
        """
resource "aws_dynamodb_table" "this" {
  name         = var.table_name
  billing_mode = var.billing_mode
  hash_key     = var.hash_key

  attribute {
    name = var.hash_key
    type = var.attribute_type
  }
}
""",
        [("table_arn", "aws_dynamodb_table.this.arn")],
    )

def redshift_module():
    return module_template(
        [
            'variable "name" {}',
            """
variable "subnet_ids" {
  type = list(string)
}
""",
            """
variable "description" {
  default = "Redshift subnet group"
}
""",
        ],
        """
resource "aws_redshift_subnet_group" "this" {
  name        = var.name
  description = var.description
  subnet_ids  = var.subnet_ids
}
""",
        [("subnet_group_name", "aws_redshift_subnet_group.this.name")],
    )

def elasticache_module():
    return module_template(
        [
            'variable "cluster_id" {}',
            'variable "engine" {}',
            'variable "node_type" {}',
            """
variable "num_cache_nodes" {
  type    = number
  default = 1
}
""",
            """
variable "parameter_group_name" {
  default = "default.redis7"
}
""",
        ],
        """
resource "aws_elasticache_cluster" "this" {
  cluster_id           = var.cluster_id
  engine               = var.engine
  node_type            = var.node_type
  num_cache_nodes      = var.num_cache_nodes
  parameter_group_name = var.parameter_group_name
}
""",
        [("cache_cluster_id", "aws_elasticache_cluster.this.cluster_id")],
    )

def kinesis_module():
    return module_template(
        [
            'variable "stream_name" {}',
            """
variable "shard_count" {
  type    = number
  default = 1
}
""",
            """
variable "retention_period" {
  type    = number
  default = 24
}
""",
        ],
        """
resource "aws_kinesis_stream" "this" {
  name             = var.stream_name
  shard_count      = var.shard_count
  retention_period = var.retention_period
}
""",
        [("stream_arn", "aws_kinesis_stream.this.arn")],
    )

def glue_module():
    return module_template(
        ['variable "database_name" {}'],
        """
resource "aws_glue_catalog_database" "this" {
  name = var.database_name
}
""",
        [("database_name", "aws_glue_catalog_database.this.name")],
    )

def athena_module():
    return module_template(
        ['variable "database_name" {}', 'variable "s3_bucket" {}'],
        """
resource "aws_athena_database" "this" {
  name   = var.database_name
  bucket = var.s3_bucket
}
""",
        [("database_name", "aws_athena_database.this.name")],
    )

def emr_module():
    return module_template(
        ['variable "name" {}', 'variable "configuration_json" {}'],
        """
resource "aws_emr_security_configuration" "this" {
  name          = var.name
  configuration = var.configuration_json
}
""",
        [("security_configuration_id", "aws_emr_security_configuration.this.id")],
    )

def apigateway_module():
    return module_template(
        ['variable "name" {}', """
variable "description" {
  default = ""
}
"""],
        """
resource "aws_api_gateway_rest_api" "this" {
  name        = var.name
  description = var.description
}
""",
        [("api_id", "aws_api_gateway_rest_api.this.id")],
    )

def ssm_module():
    return module_template(
        [
            'variable "name" {}',
            """
variable "type" {
  default = "String"
}
""",
            'variable "value" {}',
        ],
        """
resource "aws_ssm_parameter" "this" {
  name  = var.name
  type  = var.type
  value = var.value
}
""",
        [("parameter_arn", "aws_ssm_parameter.this.arn")],
    )

def secretsmanager_module():
    return module_template(
        [
            'variable "name" {}',
            """
variable "description" {
  default = ""
}
""",
        ],
        """
resource "aws_secretsmanager_secret" "this" {
  name        = var.name
  description = var.description
}
""",
        [("secret_arn", "aws_secretsmanager_secret.this.arn")],
    )

def acm_module():
    return module_template(
        [
            'variable "domain_name" {}',
            """
variable "validation_method" {
  default = "DNS"
}
""",
            """
variable "subject_alternative_names" {
  type    = list(string)
  default = []
}
""",
        ],
        """
resource "aws_acm_certificate" "this" {
  domain_name               = var.domain_name
  validation_method         = var.validation_method
  subject_alternative_names = var.subject_alternative_names
}
""",
        [("certificate_arn", "aws_acm_certificate.this.arn")],
    )

def waf_module():
    return module_template(
        [
            'variable "name" {}',
            """
variable "scope" {
  default = "REGIONAL"
}
""",
        ],
        """
locals {
  waf_default_allow = true
}

resource "aws_wafv2_web_acl" "this" {
  name  = var.name
  scope = var.scope

  default_action {
    allow {}
  }

  visibility_config {
    cloudwatch_metrics_enabled = true
    metric_name                = "${var.name}-metrics"
    sampled_requests_enabled   = true
  }
}
""",
        [("web_acl_arn", "aws_wafv2_web_acl.this.arn")],
    )

def guardduty_module():
    return module_template(
        [
            """
variable "enable" {
  type    = bool
  default = true
}
""",
        ],
        """
resource "aws_guardduty_detector" "this" {
  enable = var.enable
}
""",
        [("detector_id", "aws_guardduty_detector.this.id")],
    )

def config_module():
    return module_template(
        [
            'variable "recorder_name" {}',
            'variable "role_arn" {}',
            'variable "s3_bucket_name" {}',
        ],
        """
resource "aws_config_configuration_recorder" "this" {
  name     = var.recorder_name
  role_arn = var.role_arn

  recording_group {
    all_supported = true
  }
}

resource "aws_config_delivery_channel" "this" {
  name           = "${var.recorder_name}-channel"
  s3_bucket_name = var.s3_bucket_name
  depends_on     = [aws_config_configuration_recorder.this]
}

resource "aws_config_configuration_recorder_status" "this" {
  name       = aws_config_configuration_recorder.this.name
  is_enabled = true
  depends_on = [aws_config_delivery_channel.this]
}
""",
        [("recorder_name", "aws_config_configuration_recorder.this.name")],
    )

def backup_module():
    return module_template(
        [
            'variable "vault_name" {}',
            """
variable "kms_key_arn" {
  default = null
}
""",
        ],
        """
resource "aws_backup_vault" "this" {
  name        = var.vault_name
  kms_key_arn = var.kms_key_arn
}
""",
        [("backup_vault_arn", "aws_backup_vault.this.arn")],
    )

def organizations_module():
    return module_template(
        [
            """
variable "feature_set" {
  default = "ALL"
}
""",
        ],
        """
resource "aws_organizations_organization" "this" {
  feature_set = var.feature_set
}
""",
        [("organization_arn", "aws_organizations_organization.this.arn")],
    )

# -----------------
# GCP module defs
# -----------------

def gcp_vpc_module():
    return module_template(
        [
            'variable "name" {}',
            """
variable "auto_create_subnetworks" {
  type    = bool
  default = true
}
""",
        ],
        """
resource "google_compute_network" "this" {
  name                    = var.name
  auto_create_subnetworks = var.auto_create_subnetworks
}
""",
        [("network_self_link", "google_compute_network.this.self_link")],
    )

def gcp_compute_module():
    return module_template(
        [
            'variable "name" {}',
            'variable "machine_type" {}',
            'variable "zone" {}',
            'variable "image" {}',
            """
variable "network" {
  default = "default"
}
""",
        ],
        """
resource "google_compute_instance" "this" {
  name         = var.name
  machine_type = var.machine_type
  zone         = var.zone

  boot_disk {
    initialize_params {
      image = var.image
    }
  }

  network_interface {
    network = var.network
  }
}
""",
        [("instance_self_link", "google_compute_instance.this.self_link")],
    )

def gcp_storage_module():
    return module_template(
        [
            'variable "bucket_name" {}',
            'variable "location" {}',
        ],
        """
resource "google_storage_bucket" "this" {
  name     = var.bucket_name
  location = var.location
}
""",
        [("bucket_url", "google_storage_bucket.this.url")],
    )

def gcp_cloudsql_module():
    return module_template(
        [
            'variable "name" {}',
            'variable "database_version" {}',
            'variable "tier" {}',
        ],
        """
resource "google_sql_database_instance" "this" {
  name             = var.name
  database_version = var.database_version

  settings {
    tier = var.tier
  }
}
""",
        [("connection_name", "google_sql_database_instance.this.connection_name")],
    )

def gcp_functions_module():
    return module_template(
        [
            'variable "name" {}',
            'variable "runtime" {}',
            'variable "entry_point" {}',
            'variable "bucket" {}',
            'variable "source_archive_object" {}',
            """
variable "trigger_http" {
  type    = bool
  default = true
}
""",
        ],
        """
resource "google_cloudfunctions_function" "this" {
  name                  = var.name
  runtime               = var.runtime
  entry_point           = var.entry_point
  source_archive_bucket = var.bucket
  source_archive_object = var.source_archive_object
  trigger_http          = var.trigger_http
}
""",
        [("function_https_trigger_url", "google_cloudfunctions_function.this.https_trigger_url")],
    )

def gcp_cloudrun_module():
    return module_template(
        [
            'variable "name" {}',
            'variable "location" {}',
            'variable "image" {}',
        ],
        """
resource "google_cloud_run_service" "this" {
  name     = var.name
  location = var.location

  template {
    spec {
      containers {
        image = var.image
      }
    }
  }
}
""",
        [("service_status", "google_cloud_run_service.this.status[0].url")],
    )

def gcp_pubsub_module():
    return module_template(
        ['variable "topic_name" {}'],
        """
resource "google_pubsub_topic" "this" {
  name = var.topic_name
}
""",
        [("topic_name", "google_pubsub_topic.this.name")],
    )

def gcp_bigquery_module():
    return module_template(
        [
            'variable "dataset_id" {}',
            """
variable "location" {
  default = "US"
}
""",
        ],
        """
resource "google_bigquery_dataset" "this" {
  dataset_id = var.dataset_id
  location   = var.location
}
""",
        [("dataset_self_link", "google_bigquery_dataset.this.self_link")],
    )

def gcp_gke_module():
    return module_template(
        [
            'variable "name" {}',
            """
variable "location" {
  default = "us-central1"
}
""",
            """
variable "initial_node_count" {
  type    = number
  default = 1
}
""",
        ],
        """
resource "google_container_cluster" "this" {
  name               = var.name
  location           = var.location
  initial_node_count = var.initial_node_count
  remove_default_node_pool = true
  lifecycle {
    ignore_changes = [node_config]
  }
}
""",
        [("cluster_endpoint", "google_container_cluster.this.endpoint")],
    )

def gcp_spanner_module():
    return module_template(
        [
            'variable "name" {}',
            """
variable "config" {
  default = "regional-us-central1"
}
""",
            """
variable "display_name" {
  default = "spanner-instance"
}
""",
            """
variable "processing_units" {
  type    = number
  default = 100
}
""",
        ],
        """
resource "google_spanner_instance" "this" {
  name             = var.name
  config           = var.config
  display_name     = var.display_name
  processing_units = var.processing_units
}
""",
        [("instance_id", "google_spanner_instance.this.id")],
    )

def gcp_firestore_module():
    return module_template(
        [
            'variable "name" {}',
            """
variable "location_id" {
  default = "nam5"
}
""",
            """
variable "type" {
  default = "FIRESTORE_NATIVE"
}
""",
        ],
        """
resource "google_firestore_database" "this" {
  name        = var.name
  location_id = var.location_id
  type        = var.type
}
""",
        [("database_name", "google_firestore_database.this.name")],
    )

def gcp_memorystore_module():
    return module_template(
        [
            'variable "name" {}',
            'variable "tier" {}',
            """
variable "memory_size_gb" {
  type    = number
  default = 1
}
""",
            """
variable "region" {
  default = "us-central1"
}
""",
        ],
        """
resource "google_redis_instance" "this" {
  name           = var.name
  tier           = var.tier
  memory_size_gb = var.memory_size_gb
  region         = var.region
}
""",
        [("host", "google_redis_instance.this.host")],
    )

def gcp_cloudcdn_module():
    return module_template(
        [
            'variable "name" {}',
            'variable "bucket" {}',
        ],
        """
resource "google_compute_backend_bucket" "this" {
  name        = var.name
  bucket_name = var.bucket
  enable_cdn  = true
}
""",
        [("backend_bucket_name", "google_compute_backend_bucket.this.name")],
    )

def gcp_iam_module():
    return module_template(
        [
            'variable "role_id" {}',
            'variable "title" {}',
            'variable "permissions" {}',
        ],
        """
resource "google_project_iam_custom_role" "this" {
  role_id     = var.role_id
  title       = var.title
  permissions = var.permissions
}
""",
        [("role_name", "google_project_iam_custom_role.this.name")],
    )

def gcp_logging_module():
    return module_template(
        [
            'variable "bucket_id" {}',
            """
variable "location" {
  default = "global"
}
""",
            """
variable "retention_days" {
  type    = number
  default = 30
}
""",
        ],
        """
resource "google_logging_project_bucket_config" "this" {
  bucket_id      = var.bucket_id
  location       = var.location
  retention_days = var.retention_days
}
""",
        [("bucket_id", "google_logging_project_bucket_config.this.bucket_id")],
    )

def gcp_monitoring_module():
    return module_template(
        [
            'variable "dashboard_json" {}',
        ],
        """
resource "google_monitoring_dashboard" "this" {
  dashboard_json = var.dashboard_json
}
""",
        [("dashboard_name", "google_monitoring_dashboard.this.id")],
    )

def gcp_cloudbuild_module():
    return module_template(
        [
            'variable "name" {}',
            'variable "filename" {}',
        ],
        """
resource "google_cloudbuild_trigger" "this" {
  name     = var.name
  filename = var.filename
}
""",
        [("trigger_id", "google_cloudbuild_trigger.this.id")],
    )

def gcp_deployment_manager_module():
    return module_template(
        [
            'variable "name" {}',
            'variable "config" {}',
        ],
        """
resource "google_deployment_manager_deployment" "this" {
  name   = var.name
  target {
    config {
      content = var.config
    }
  }
}
""",
        [("deployment_id", "google_deployment_manager_deployment.this.id")],
    )

def gcp_cloud_dns_module():
    return module_template(
        [
            'variable "name" {}',
            'variable "dns_name" {}',
        ],
        """
resource "google_dns_managed_zone" "this" {
  name     = var.name
  dns_name = var.dns_name
}
""",
        [("zone_name", "google_dns_managed_zone.this.name")],
    )

def gcp_cloud_armor_module():
    return module_template(
        ['variable "name" {}'],
        """
resource "google_compute_security_policy" "this" {
  name = var.name
}
""",
        [("policy_id", "google_compute_security_policy.this.id")],
    )

def gcp_vertex_ai_module():
    return module_template(
        [
            'variable "display_name" {}',
            'variable "metadata_schema_uri" {}',
        ],
        """
resource "google_vertex_ai_dataset" "this" {
  display_name        = var.display_name
  metadata_schema_uri = var.metadata_schema_uri
}
""",
        [("dataset_id", "google_vertex_ai_dataset.this.id")],
    )

def gcp_dataflow_module():
    return module_template(
        [
            'variable "name" {}',
            """
variable "template_gcs_path" {
  default = ""
}
""",
            """
variable "parameters_json" {
  default = "{}"
}
""",
            """
variable "region" {
  default = "us-central1"
}
""",
        ],
        """
resource "google_dataflow_job" "this" {
  name               = var.name
  template_gcs_path  = var.template_gcs_path
  parameters         = jsondecode(var.parameters_json)
  region             = var.region
}
""",
        [("job_id", "google_dataflow_job.this.id")],
    )

def gcp_dataproc_module():
    return module_template(
        [
            'variable "name" {}',
            """
variable "region" {
  default = "us-central1"
}
""",
            'variable "cluster_config" {}',
        ],
        """
resource "google_dataproc_cluster" "this" {
  name   = var.name
  region = var.region
  cluster_config = jsondecode(var.cluster_config)
}
""",
        [("cluster_id", "google_dataproc_cluster.this.id")],
    )

def gcp_cloud_composer_module():
    return module_template(
        [
            'variable "name" {}',
            """
variable "region" {
  default = "us-central1"
}
""",
            """
variable "image_version" {
  default = "composer-3-airflow-2.6.3"
}
""",
        ],
        """
resource "google_composer_environment" "this" {
  name          = var.name
  region        = var.region
  image_version = var.image_version
}
""",
        [("composer_id", "google_composer_environment.this.id")],
    )

def gcp_secret_manager_module():
    return module_template(
        [
            'variable "name" {}',
            """
variable "replication_automatic" {
  type    = bool
  default = true
}
""",
        ],
        """
resource "google_secret_manager_secret" "this" {
  secret_id = var.name
  replication {
    automatic = var.replication_automatic
  }
}
""",
        [("secret_id", "google_secret_manager_secret.this.id")],
    )

def gcp_scheduler_module():
    return module_template(
        [
            'variable "name" {}',
            'variable "schedule" {}',
            'variable "http_target_uri" {}',
        ],
        """
resource "google_cloud_scheduler_job" "this" {
  name     = var.name
  schedule = var.schedule
  http_target {
    uri = var.http_target_uri
  }
}
""",
        [("job_id", "google_cloud_scheduler_job.this.id")],
    )

def gcp_cloud_tasks_module():
    return module_template(
        [
            'variable "name" {}',
            """
variable "location" {
  default = "us-central1"
}
""",
        ],
        """
resource "google_cloud_tasks_queue" "this" {
  name     = var.name
  location = var.location
}
""",
        [("queue_name", "google_cloud_tasks_queue.this.name")],
    )

def gcp_firebase_hosting_module():
    return module_template(
        ['variable "site_id" {}'],
        """
resource "google_firebase_hosting_site" "this" {
  site_id = var.site_id
}
""",
        [("site_id", "google_firebase_hosting_site.this.site_id")],
    )

def gcp_sourcerepo_module():
    return module_template(
        ['variable "name" {}'],
        """
resource "google_sourcerepo_repository" "this" {
  name = var.name
}
""",
        [("url", "google_sourcerepo_repository.this.url")],
    )

def gcp_anthos_module():
    return module_template(
        [
            'variable "membership_id" {}',
            'variable "endpoint" {}',
        ],
        """
resource "google_gke_hub_membership" "this" {
  membership_id = var.membership_id
  endpoint {
    gke_cluster {
      resource_link = var.endpoint
    }
  }
}
""",
        [("membership_name", "google_gke_hub_membership.this.name")],
    )

def root_module_call(cloud, service, inputs):
    if cloud == "aws":
        source = f"./modules/{service}"
    else:
        source = f"./modules/gcp/{service}"
    block = f'module "{service}" {{\n  source = "{source}"\n'
    for k, v in inputs.items():
        block += f"  {k} = {v}\n"
    block += "}\n"
    return block

SERVICE_TEMPLATES = {
    "aws": {
        "vpc": vpc_module,
        "ec2": ec2_module,
        "rds": rds_module,
        "s3": s3_module,
        "alb": alb_module,
        "lambda": lambda_module,
        "cloudwatch": cloudwatch_module,
        "iam": iam_module,
        "sns": sns_module,
        "sqs": sqs_module,
        "ecs": ecs_module,
        "eks": eks_module,
        "cloudfront": cloudfront_module,
        "route53": route53_module,
        "cloudformation": cloudformation_module,
        "dynamodb": dynamodb_module,
        "redshift": redshift_module,
        "elasticache": elasticache_module,
        "kinesis": kinesis_module,
        "glue": glue_module,
        "athena": athena_module,
        "emr": emr_module,
        "apigateway": apigateway_module,
        "ssm": ssm_module,
        "secretsmanager": secretsmanager_module,
        "acm": acm_module,
        "waf": waf_module,
        "guardduty": guardduty_module,
        "config": config_module,
        "backup": backup_module,
        "organizations": organizations_module,
    },
    "gcp": {
        "vpc": gcp_vpc_module,
        "compute": gcp_compute_module,
        "storage": gcp_storage_module,
        "cloudsql": gcp_cloudsql_module,
        "functions": gcp_functions_module,
        "cloudrun": gcp_cloudrun_module,
        "pubsub": gcp_pubsub_module,
        "bigquery": gcp_bigquery_module,
        "gke": gcp_gke_module,
        "spanner": gcp_spanner_module,
        "firestore": gcp_firestore_module,
        "memorystore": gcp_memorystore_module,
        "cloudcdn": gcp_cloudcdn_module,
        "iam": gcp_iam_module,
        "logging": gcp_logging_module,
        "monitoring": gcp_monitoring_module,
        "cloudbuild": gcp_cloudbuild_module,
        "deploymentmanager": gcp_deployment_manager_module,
        "clouddns": gcp_cloud_dns_module,
        "cloudarmor": gcp_cloud_armor_module,
        "vertexai": gcp_vertex_ai_module,
        "dataflow": gcp_dataflow_module,
        "dataproc": gcp_dataproc_module,
        "cloudcomposer": gcp_cloud_composer_module,
        "secretmanager": gcp_secret_manager_module,
        "scheduler": gcp_scheduler_module,
        "cloudtasks": gcp_cloud_tasks_module,
        "firebasehosting": gcp_firebase_hosting_module,
        "sourcerepo": gcp_sourcerepo_module,
        "anthos": gcp_anthos_module,
    },
}

MODULE_INPUTS = {
    "aws": {
        "vpc": [
            {"name": "cidr", "prompt": "VPC CIDR", "default": "10.0.0.0/16"},
        ],
        "ec2": [
            {"name": "ami", "prompt": "AMI ID", "default": None},
            {"name": "instance_type", "prompt": "Instance type", "default": "t3.micro"},
        ],
        "rds": [
            {"name": "engine", "prompt": "Engine", "default": "mysql"},
            {"name": "instance_class", "prompt": "Instance class", "default": "db.t3.micro"},
            {"name": "db_name", "prompt": "DB name", "default": None},
            {"name": "username", "prompt": "DB user", "default": None},
            {"name": "password", "prompt": "DB password", "default": None},
        ],
        "s3": [
            {"name": "bucket", "prompt": "Bucket name", "default": None},
        ],
        "alb": [
            {"name": "name", "prompt": "ALB name", "default": "box-alb"},
        ],
        "lambda": [
            {"name": "function_name", "prompt": "Lambda function name", "default": None},
            {"name": "filename", "prompt": "Zip file path", "default": "lambda.zip"},
            {"name": "handler", "prompt": "Lambda handler", "default": "index.handler"},
            {"name": "runtime", "prompt": "Lambda runtime", "default": "python3.11"},
            {"name": "role_arn", "prompt": "Execution role ARN", "default": None},
            {"name": "timeout", "prompt": "Timeout (seconds)", "default": "3", "value_type": "number"},
        ],
        "cloudwatch": [
            {"name": "log_group_name", "prompt": "Log group name", "default": "/aws/app/logs"},
            {"name": "retention_in_days", "prompt": "Retention (days)", "default": "7", "value_type": "number"},
        ],
        "iam": [
            {"name": "role_name", "prompt": "IAM role name", "default": None},
            {"name": "assume_role_policy", "prompt": "Assume role policy JSON", "default": '{"Version":"2012-10-17","Statement":[{"Effect":"Allow","Principal":{"Service":"ec2.amazonaws.com"},"Action":"sts:AssumeRole"}]}'},
            {"name": "description", "prompt": "Role description", "default": ""},
        ],
        "sns": [
            {"name": "topic_name", "prompt": "SNS topic name", "default": None},
            {"name": "display_name", "prompt": "Display name", "default": ""},
        ],
        "sqs": [
            {"name": "queue_name", "prompt": "SQS queue name", "default": None},
            {"name": "visibility_timeout_seconds", "prompt": "Visibility timeout (seconds)", "default": "30", "value_type": "number"},
        ],
        "ecs": [
            {"name": "cluster_name", "prompt": "ECS cluster name", "default": None},
        ],
        "eks": [
            {"name": "cluster_name", "prompt": "EKS cluster name", "default": None},
            {"name": "role_arn", "prompt": "EKS role ARN", "default": None},
            {"name": "subnet_ids", "prompt": "Subnet IDs (comma-separated)", "default": "", "value_type": "list"},
            {"name": "version", "prompt": "EKS version", "default": "1.28"},
        ],
        "cloudfront": [
            {"name": "comment", "prompt": "CloudFront comment", "default": "OAI for S3"},
        ],
        "route53": [
            {"name": "zone_name", "prompt": "Hosted zone domain", "default": "example.com"},
        ],
        "cloudformation": [
            {"name": "stack_name", "prompt": "Stack name", "default": None},
            {"name": "template_body", "prompt": "Template body (JSON/YAML)", "default": "{}"},
        ],
        "dynamodb": [
            {"name": "table_name", "prompt": "DynamoDB table name", "default": None},
            {"name": "hash_key", "prompt": "Partition key name", "default": "id"},
            {"name": "attribute_type", "prompt": "Partition key type (S/N/B)", "default": "S"},
            {"name": "billing_mode", "prompt": "Billing mode", "default": "PAY_PER_REQUEST"},
        ],
        "redshift": [
            {"name": "name", "prompt": "Redshift subnet group name", "default": None},
            {"name": "subnet_ids", "prompt": "Subnet IDs (comma-separated)", "default": "", "value_type": "list"},
            {"name": "description", "prompt": "Description", "default": "Redshift subnet group"},
        ],
        "elasticache": [
            {"name": "cluster_id", "prompt": "ElastiCache cluster ID", "default": None},
            {"name": "engine", "prompt": "Cache engine", "default": "redis"},
            {"name": "node_type", "prompt": "Node type", "default": "cache.t3.micro"},
            {"name": "num_cache_nodes", "prompt": "Cache nodes", "default": "1", "value_type": "number"},
            {"name": "parameter_group_name", "prompt": "Parameter group name", "default": "default.redis7"},
        ],
        "kinesis": [
            {"name": "stream_name", "prompt": "Kinesis stream name", "default": None},
            {"name": "shard_count", "prompt": "Shard count", "default": "1", "value_type": "number"},
            {"name": "retention_period", "prompt": "Retention period (hours)", "default": "24", "value_type": "number"},
        ],
        "glue": [
            {"name": "database_name", "prompt": "Glue database name", "default": None},
        ],
        "athena": [
            {"name": "database_name", "prompt": "Athena database name", "default": None},
            {"name": "s3_bucket", "prompt": "Results bucket", "default": None},
        ],
        "emr": [
            {"name": "name", "prompt": "EMR security configuration name", "default": None},
            {"name": "configuration_json", "prompt": "Configuration JSON", "default": '{"EncryptionConfiguration":{"EnableAtRestEncryption":false,"EnableInTransitEncryption":false,"EnableS3Encryption":false}}'},
        ],
        "apigateway": [
            {"name": "name", "prompt": "API name", "default": None},
            {"name": "description", "prompt": "API description", "default": ""},
        ],
        "ssm": [
            {"name": "name", "prompt": "SSM parameter name", "default": None},
            {"name": "type", "prompt": "Parameter type", "default": "String"},
            {"name": "value", "prompt": "Parameter value", "default": None},
        ],
        "secretsmanager": [
            {"name": "name", "prompt": "Secret name", "default": None},
            {"name": "description", "prompt": "Secret description", "default": ""},
        ],
        "acm": [
            {"name": "domain_name", "prompt": "Domain name", "default": None},
            {"name": "validation_method", "prompt": "Validation method (DNS/EMAIL)", "default": "DNS"},
            {"name": "subject_alternative_names", "prompt": "SANs (comma-separated)", "default": "", "value_type": "list"},
        ],
        "waf": [
            {"name": "name", "prompt": "WAF name", "default": None},
            {"name": "scope", "prompt": "Scope (REGIONAL/CLOUDFRONT)", "default": "REGIONAL"},
        ],
        "guardduty": [
            {"name": "enable", "prompt": "Enable detector? (true/false)", "default": "true", "value_type": "bool"},
        ],
        "config": [
            {"name": "recorder_name", "prompt": "Recorder name", "default": None},
            {"name": "role_arn", "prompt": "Config service role ARN", "default": None},
            {"name": "s3_bucket_name", "prompt": "Delivery bucket", "default": None},
        ],
        "backup": [
            {"name": "vault_name", "prompt": "Backup vault name", "default": None},
            {"name": "kms_key_arn", "prompt": "KMS key ARN (optional)", "default": ""},
        ],
        "organizations": [
            {"name": "feature_set", "prompt": "Feature set (ALL/CONSOLIDATED_BILLING)", "default": "ALL"},
        ],
    },
    "gcp": {
        "vpc": [
            {"name": "name", "prompt": "VPC name", "default": "box-vpc"},
            {"name": "auto_create_subnetworks", "prompt": "Auto create subnetworks? (true/false)", "default": "true", "value_type": "bool"},
        ],
        "compute": [
            {"name": "name", "prompt": "Instance name", "default": "box-vm"},
            {"name": "machine_type", "prompt": "Machine type", "default": "e2-medium"},
            {"name": "zone", "prompt": "Zone", "default": "us-central1-a"},
            {"name": "image", "prompt": "Boot image", "default": "debian-cloud/debian-12"},
            {"name": "network", "prompt": "Network self link or name", "default": "default"},
        ],
        "storage": [
            {"name": "bucket_name", "prompt": "Bucket name", "default": "box-bucket"},
            {"name": "location", "prompt": "Bucket location", "default": "US"},
        ],
        "cloudsql": [
            {"name": "name", "prompt": "Cloud SQL instance name", "default": "box-sql"},
            {"name": "database_version", "prompt": "Database version", "default": "MYSQL_8_0"},
            {"name": "tier", "prompt": "Instance tier", "default": "db-f1-micro"},
        ],
        "functions": [
            {"name": "name", "prompt": "Function name", "default": "box-function"},
            {"name": "runtime", "prompt": "Runtime", "default": "python311"},
            {"name": "entry_point", "prompt": "Entry point", "default": "hello"},
            {"name": "bucket", "prompt": "Source bucket", "default": "function-source"},
            {"name": "source_archive_object", "prompt": "Source archive object", "default": "function.zip"},
            {"name": "trigger_http", "prompt": "Trigger via HTTP? (true/false)", "default": "true", "value_type": "bool"},
        ],
        "cloudrun": [
            {"name": "name", "prompt": "Cloud Run service name", "default": "box-run"},
            {"name": "location", "prompt": "Location", "default": "us-central1"},
            {"name": "image", "prompt": "Container image", "default": "gcr.io/cloudrun/hello"},
        ],
        "pubsub": [
            {"name": "topic_name", "prompt": "Pub/Sub topic name", "default": "box-topic"},
        ],
        "bigquery": [
            {"name": "dataset_id", "prompt": "Dataset ID", "default": "box_dataset"},
            {"name": "location", "prompt": "Dataset location", "default": "US"},
        ],
        "gke": [
            {"name": "name", "prompt": "GKE cluster name", "default": "box-gke"},
            {"name": "location", "prompt": "Cluster location", "default": "us-central1"},
            {"name": "initial_node_count", "prompt": "Initial node count", "default": "1", "value_type": "number"},
        ],
        "spanner": [
            {"name": "name", "prompt": "Spanner instance name", "default": "box-spanner"},
            {"name": "config", "prompt": "Instance config", "default": "regional-us-central1"},
            {"name": "display_name", "prompt": "Display name", "default": "Box Spanner"},
            {"name": "processing_units", "prompt": "Processing units", "default": "100", "value_type": "number"},
        ],
        "firestore": [
            {"name": "name", "prompt": "Database name", "default": "(default)"},
            {"name": "location_id", "prompt": "Location ID", "default": "nam5"},
            {"name": "type", "prompt": "Database type", "default": "FIRESTORE_NATIVE"},
        ],
        "memorystore": [
            {"name": "name", "prompt": "Redis instance name", "default": "box-redis"},
            {"name": "tier", "prompt": "Tier (BASIC/STANDARD_HA)", "default": "BASIC"},
            {"name": "memory_size_gb", "prompt": "Memory (GB)", "default": "1", "value_type": "number"},
            {"name": "region", "prompt": "Region", "default": "us-central1"},
        ],
        "cloudcdn": [
            {"name": "name", "prompt": "Backend bucket name", "default": "box-cdn"},
            {"name": "bucket", "prompt": "Origin bucket", "default": "cdn-bucket"},
        ],
        "iam": [
            {"name": "role_id", "prompt": "Custom role ID", "default": "boxRole"},
            {"name": "title", "prompt": "Role title", "default": "Box Role"},
            {"name": "permissions", "prompt": "Comma separated permissions", "default": "resourcemanager.projects.get", "value_type": "list"},
        ],
        "logging": [
            {"name": "bucket_id", "prompt": "Logging bucket ID", "default": "audit-logs"},
            {"name": "location", "prompt": "Bucket location", "default": "global"},
            {"name": "retention_days", "prompt": "Retention days", "default": "30", "value_type": "number"},
        ],
        "monitoring": [
            {"name": "dashboard_json", "prompt": "Dashboard JSON", "default": '{"displayName":"Sample Dashboard","gridLayout":{"columns":1,"widgets":[]}}'},
        ],
        "cloudbuild": [
            {"name": "name", "prompt": "Trigger name", "default": "box-trigger"},
            {"name": "filename", "prompt": "Build config path", "default": "cloudbuild.yaml"},
        ],
        "deploymentmanager": [
            {"name": "name", "prompt": "Deployment name", "default": "box-deployment"},
            {"name": "config", "prompt": "YAML config content", "default": "resources: []"},
        ],
        "clouddns": [
            {"name": "name", "prompt": "Managed zone name", "default": "box-zone"},
            {"name": "dns_name", "prompt": "DNS name (must end with dot)", "default": "example.com."},
        ],
        "cloudarmor": [
            {"name": "name", "prompt": "Security policy name", "default": "box-policy"},
        ],
        "vertexai": [
            {"name": "display_name", "prompt": "Dataset display name", "default": "box-dataset"},
            {"name": "metadata_schema_uri", "prompt": "Metadata schema URI", "default": "gs://google-cloud-aiplatform/schema/dataset/metadata/image_1.0.0.yaml"},
        ],
        "dataflow": [
            {"name": "name", "prompt": "Dataflow job name", "default": "box-job"},
            {"name": "template_gcs_path", "prompt": "Template GCS path", "default": "gs://dataflow-templates/latest/Word_Count"},
            {"name": "parameters_json", "prompt": "Parameters JSON", "default": '{"inputFile":"gs://dataflow-samples/kinglear.txt","output":"gs://my-bucket/output"}'},
            {"name": "region", "prompt": "Region", "default": "us-central1"},
        ],
        "dataproc": [
            {"name": "name", "prompt": "Dataproc cluster name", "default": "box-dataproc"},
            {"name": "region", "prompt": "Region", "default": "us-central1"},
            {"name": "cluster_config", "prompt": "Cluster config JSON", "default": '{"gceClusterConfig":{"zoneUri":"us-central1-a"},"masterConfig":{"numInstances":1,"machineTypeUri":"n1-standard-2"},"workerConfig":{"numInstances":2,"machineTypeUri":"n1-standard-2"}}'},
        ],
        "cloudcomposer": [
            {"name": "name", "prompt": "Composer environment name", "default": "box-composer"},
            {"name": "region", "prompt": "Region", "default": "us-central1"},
            {"name": "image_version", "prompt": "Image version", "default": "composer-3-airflow-2.6.3"},
        ],
        "secretmanager": [
            {"name": "name", "prompt": "Secret ID", "default": "box-secret"},
            {"name": "replication_automatic", "prompt": "Automatic replication? (true/false)", "default": "true", "value_type": "bool"},
        ],
        "scheduler": [
            {"name": "name", "prompt": "Scheduler job name", "default": "box-job"},
            {"name": "schedule", "prompt": "Cron schedule", "default": "*/5 * * * *"},
            {"name": "http_target_uri", "prompt": "HTTP target URL", "default": "https://example.com/hook"},
        ],
        "cloudtasks": [
            {"name": "name", "prompt": "Queue name", "default": "box-queue"},
            {"name": "location", "prompt": "Location", "default": "us-central1"},
        ],
        "firebasehosting": [
            {"name": "site_id", "prompt": "Hosting site ID", "default": "box-site"},
        ],
        "sourcerepo": [
            {"name": "name", "prompt": "Repository name", "default": "box-repo"},
        ],
        "anthos": [
            {"name": "membership_id", "prompt": "Membership ID", "default": "box-membership"},
            {"name": "endpoint", "prompt": "GKE cluster resource link", "default": "//container.googleapis.com/projects/my-project/locations/us-central1/clusters/box-gke"},
        ],
    },
}

def main():
    print("\n🚀 Box Terraform Module Generator\n")

    cloud = ask_cloud_provider()
    services = select_services(cloud)

    if not services:
        print("\n⚠️ No services were selected; exiting without generating Terraform.")
        return

    if cloud == "aws":
        region = ask("AWS region", "ap-south-1")
        project = None
    else:
        project = ask("GCP project ID")
        region = ask("GCP region", "us-central1")

    ROOT.mkdir(exist_ok=True)
    write(ROOT / "provider.tf", provider_tf(cloud))
    module_blocks = []
    ordered_variables = []
    seen_variables = set()
    tfvars_values = {}

    def register_variable(name):
        if name not in seen_variables:
            seen_variables.add(name)
            ordered_variables.append(name)

    if cloud == "aws":
        register_variable("region")
        tfvars_values["region"] = region
    else:
        register_variable("project")
        register_variable("region")
        tfvars_values["project"] = project
        tfvars_values["region"] = region

    for svc in services:
        mod_factory = SERVICE_TEMPLATES.get(cloud, {}).get(svc)
        if not mod_factory:
            print(f"\n⚠️ Skipping {svc.upper()} (not implemented yet)")
            continue

        print(f"\n⚙️ Configuring {svc.upper()} module")
        mod_path = MODULES_DIR / svc if cloud == "aws" else MODULES_DIR / "gcp" / svc
        template_files = mod_factory()

        for fname, content in template_files.items():
            write(mod_path / fname, content)

        inputs = {}
        for meta in MODULE_INPUTS.get(cloud, {}).get(svc, []):
            value = ask(meta["prompt"], meta.get("default"))
            root_var_name = f'{svc}_{meta["name"]}'
            register_variable(root_var_name)
            tfvars_values[root_var_name] = coerce_tfvars_value(value, meta)
            inputs[meta["name"]] = f"var.{root_var_name}"

        module_blocks.append(root_module_call(cloud, svc, inputs))

    if module_blocks:
        main_tf_body = "\n\n".join(block.strip() for block in module_blocks)
    else:
        main_tf_body = '# No modules were selected. Re-run box-project.py to add services.'
    write(ROOT / "main.tf", main_tf_body)
    write(ROOT / "variables.tf", render_variables_tf(ordered_variables))
    write(ROOT / "terraform.tfvars", render_tfvars(tfvars_values))

    print("\n✅ Module-based Terraform generated in:", ROOT.resolve())

if __name__ == "__main__":
    main()
