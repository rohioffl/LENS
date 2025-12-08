# Automation Backend

This project exposes multiple infrastructure automation scripts (AWS inventory exports, VPC migration helpers, HA AWS↔GCP VPN planning, etc.) through a unified Django UI and API. Each task produces downloadable artifacts (XLSX, Terraform bundles, text plans, ZIP archives), and new scripts can be plugged in through the shared automation registry.  Standalone CLI wrappers for those tasks now live under `feature/`, so every script shares a consistent entry point (`python -m feature.<module>`).

## Prerequisites

- Python 3.11+ (uses the system `python3`)
- Dependencies:
  ```bash
  python3 -m pip install django boto3 botocore openpyxl google-cloud-compute google-cloud-resource-manager google-api-core google-auth
  ```
- Terraform CLI on PATH if you want backend validation of generated bundles
- AWS credentials available via access key/secret (or a named profile for CLI usage)

## Run the backend

```bash
cd lens-backend/inventory_site
python3 manage.py runserver
```

Open http://127.0.0.1:8000/ and:

1. Choose an automation task (e.g., “AWS Inventory Export” or “VPC Migration Toolkit”).
2. Supply task-specific inputs (credentials, regions, subnet overrides, etc.).
3. Submit and download the generated artifact (single XLSX/ZIP download, or multi-file ZIP when needed).

### Adding new automation scripts

1. Place the reusable logic inside `inventory/services/` (or a subpackage) so Django can import it.
2. Add a companion CLI/entry script inside `feature/` (see `feature/terraform_vpc.py` or `feature/xlsx_inventory.py`). Anything you drop here becomes importable as `feature.<your_script>` across the project.
3. Expose a task-specific `forms.Form` subclass capturing user inputs.
4. Register the task via `inventory/services/task_registry.py`, similar to `aws_task.py` or `terraform_task.py`.

### Backend API usage

`POST /api/tasks/run/`

```bash
curl -X POST http://127.0.0.1:8000/api/tasks/run/ \
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
  "task_id": "aws_inventory",
  "artifacts": [
    { "filename": "US_East_(N._Virginia)_AWS_Inventory_20251204_191854.xlsx", "content_type": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", "data": "BASE64..." },
    { "filename": "Asia_Pacific_(Mumbai)_AWS_Inventory_20251204_191854.xlsx", "content_type": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", "data": "BASE64..." }
  ],
  "logs": "console output..."
}
```

Decode each `artifacts[*].data` string to download the file; `logs` mirrors stdout/stderr.

#### Terraform/VPC task example

```bash
curl -X POST http://127.0.0.1:8000/api/tasks/run/ \
  -H "Content-Type: application/json" \
  -d '{
    "task_id": "terraform_vpc",
    "data": {
      "access_key": "AKIA...",
      "secret_key": "...",
      "aws_region": "us-east-1",
      "aws_vpc_id": "vpc-123456",
      "gcp_project": "my-gcp-project",
      "gcp_network": "aws-migration",
      "gcp_region_fallback": "us-central1",
      "subnet_name_map": "{\"subnet-abc\": \"app-1\"}"
    }
  }'
```

Optional payload keys include `subnet_name_map` / `subnet_cidr_map` JSON overrides. Every response contains a single ZIP artifact (Terraform bundle + checks) and the captured `logs`.

### AWS/GCP helper endpoints

Use these to populate dropdowns when building UIs:

```bash
curl -X POST http://127.0.0.1:8000/api/aws/vpcs/ \
  -H "Content-Type: application/json" \
  -d '{
    "region": "us-east-1",
    "access_key": "AKIA...",
    "secret_key": "..."
  }'

curl -X POST http://127.0.0.1:8000/api/aws/subnets/ \
  -H "Content-Type: application/json" \
  -d '{
    "region": "us-east-1",
    "vpc_id": "vpc-123456",
    "access_key": "AKIA...",
    "secret_key": "..."
  }'
```

Each response includes normalized names, CIDRs, AZs, and a `suggested_name` you can surface next to editable inputs.

```bash
curl -X POST http://127.0.0.1:8000/api/gcp/networks/ \
  -H "Content-Type: application/json" \
  -d '{
    "service_key": "<service-account JSON>",
    "gcp_project": "my-gcp-project"
  }'
```

The GCP endpoint returns the resolved project ID and the set of VPC networks (plus high-level subnet metadata) accessible to your service-account key.

```bash
curl -X POST http://127.0.0.1:8000/api/gcp/projects/ \
  -H "Content-Type: application/json" \
  -d '{ "service_key": "<service-account JSON>" }'
```

The projects endpoint mirrors `gcloud auth projects list`, providing every active project visible to the uploaded service account so UIs can drive a dropdown before querying networks.

```bash
curl -X POST http://127.0.0.1:8000/api/gcp/network/ \
  -H "Content-Type: application/json" \
  -d '{
    "service_key": "<service-account JSON>",
    "gcp_project": "my-gcp-project",
    "gcp_network": "central-network"
  }'
```

Use the network-detail endpoint to lazily fetch subnet metadata for a single VPC once the user makes a selection, keeping the initial dropdown responsive even when projects host many networks/subnets.

### Sample React front-end

A minimal React harness now lives outside the backend folder at `../react-frontend-sample/`. It:

1. Auto-fetches VPCs/subnets as soon as AWS creds + region are entered.
2. Lets you tweak per-subnet name/CIDR overrides and GCP targets.
3. Triggers either the Terraform bundle task or the AWS inventory task, displaying logs and download links for the returned ZIP/XLSX files.

To run it:

```bash
cd ../react-frontend-sample
# some filesystems (WSL, containers) need --no-bin-links to avoid symlink errors
npm install --no-bin-links
npm run dev
```

The dev server proxies `/api/*` requests to http://127.0.0.1:8000, so keep the Django server running while testing.

The React sample now exposes four cards (“VPC Terraform Toolkit”, “AWS Inventory Export”, “HA VPN Builder”, and “ECR to Artifact Registry”). All views share the AWS credential block; the Terraform view auto-loads VPCs/subnets, the Inventory view offers checkbox-based multi-region/resource selection with date pickers and progress bars, the HA VPN builder adds service-account file upload plus GCP region/network pickers before producing artifacts, and the ECR workflow orchestrates repository migrations.

## CLI usage (unchanged)

```
cd lens-backend
# Run any feature script directly (or use python -m feature.<script>)
python3 -m feature.xlsx_inventory --access-key ... --secret-key ... --regions us-east-1 --resources all
python3 -m feature.terraform_vpc --aws-region us-east-1 --aws-vpc-id vpc-123456 --mode generate-terraform ...
```

CLI entry points delegate to the same shared services as the web/API layer, so adding a new script is as simple as dropping it into `feature/`.
