# LENS — AWS/GCP Infrastructure Automation Backend

[![CI](https://github.com/rohioffl/LENS/actions/workflows/ci.yml/badge.svg)](https://github.com/rohioffl/LENS/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Stack](https://img.shields.io/badge/stack-Django%20%7C%20AWS%20%7C%20GCP%20%7C%20Terraform-blue)](#tech-stack)
[![Python](https://img.shields.io/badge/python-3.11+-green.svg)](#prerequisites)

A unified infrastructure automation backend that exposes AWS/GCP scripts through a Django UI and REST API. Each task produces downloadable artifacts (XLSX, Terraform bundles, ZIP archives), and new scripts can be plugged in through the shared automation registry.

## Table of Contents

- [What This Does](#what-this-does)
- [Automation Tasks](#automation-tasks)
- [Architecture](#architecture)
- [Prerequisites](#prerequisites)
- [Run the Backend](#run-the-backend)
- [API Usage](#api-usage)
- [CLI Usage](#cli-usage)
- [Adding New Scripts](#adding-new-scripts)

## What This Does

LENS wraps complex multi-cloud infrastructure scripts behind a clean API, letting you:

1. Choose an automation task (Inventory, VPC Migration, VPN Planning, ECR Migration)
2. Supply cloud credentials and task-specific inputs
3. Download the generated artifacts (XLSX reports, Terraform bundles, migration plans)

## Automation Tasks

| Task | Output | Cloud |
|------|--------|-------|
| AWS Inventory Export | Multi-region XLSX (EC2, S3, RDS, etc.) | AWS |
| VPC Migration Toolkit | Terraform bundle + migration plan | AWS → GCP |
| HA VPN Builder | Terraform + VPN config | AWS ↔ GCP |
| Classic VPN Builder | Terraform config | AWS ↔ GCP |
| ECR to Artifact Registry | Repository migration scripts | AWS → GCP |

## Architecture

```
┌─────────────────┐     ┌──────────────────┐     ┌─────────────────┐
│   Django UI      │────▶│  Task Registry   │────▶│  Cloud SDKs     │
│  (browser form)  │     │  (task_registry) │     │  boto3 / GCP    │
└─────────────────┘     └──────────────────┘     └─────────────────┘
         │                        │
         ▼                        ▼
┌─────────────────┐     ┌──────────────────┐
│  REST API        │     │  feature/ CLI    │
│  POST /api/tasks │     │  (same services) │
└─────────────────┘     └──────────────────┘
         │
         ▼
   Artifact Download
   (XLSX / ZIP / Terraform)
```

## Prerequisites

- Python 3.11+
- Terraform CLI (for bundle validation)
- AWS credentials (access key/secret or named profile)
- GCP service account JSON (for GCP tasks)

```bash
pip install django boto3 botocore openpyxl \
  google-cloud-compute google-cloud-resource-manager \
  google-api-core google-auth
```

## Run the Backend

```bash
cd lens-backend/inventory_site
python3 manage.py runserver
# Open http://127.0.0.1:8000/
```

## API Usage

### Run a task

```bash
POST /api/tasks/run/

# AWS Inventory
curl -X POST http://localhost:8000/api/tasks/run/ \
  -H "Content-Type: application/json" \
  -d '{
    "task_id": "aws_inventory",
    "data": {
      "access_key": "AKIA...",
      "secret_key": "...",
      "regions": "us-east-1,ap-south-1",
      "resources": ["all"],
      "from_date": "last 30 days"
    }
  }'
```

Response:
```json
{
  "status": "ok",
  "artifacts": [{"filename": "..._Inventory.xlsx", "data": "BASE64..."}],
  "logs": "console output..."
}
```

### Helper endpoints

```bash
# List AWS VPCs in a region
POST /api/aws/vpcs/

# List subnets for a VPC
POST /api/aws/subnets/

# List GCP networks
POST /api/gcp/networks/

# List GCP projects
POST /api/gcp/projects/
```

## CLI Usage

```bash
cd lens-backend

# AWS Inventory
python3 -m feature.xlsx_inventory --access-key AKIA... --secret-key ... \
  --regions us-east-1 --resources all

# Terraform VPC Migration
python3 -m feature.terraform_vpc --aws-region us-east-1 \
  --aws-vpc-id vpc-123456 --mode generate-terraform
```

## Adding New Scripts

1. Place logic in `inventory/services/`
2. Add CLI entry point in `feature/`
3. Create a `forms.Form` subclass for user inputs
4. Register in `inventory/services/task_registry.py`

---

**Author:** Rohit P T | Cloud Automation Engineer @ Ankercloud
