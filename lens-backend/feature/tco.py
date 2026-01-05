#!/usr/bin/env python3
"""
Generate a per-service usage cost summary from an AWS billing CSV and optionally map AWS compute to
GCP machine types + pricing (CPU+RAM SKUs) and produce a 3-year flex/CUD estimate.

Usage examples:
  python3 service_summary_with_gcp.py --input bill.csv --output-dir output

  python3 service_summary_with_gcp.py --input bill.csv --output-dir output \
    --gcp-use-api --gcp-project=my-gcp-project --gcp-zones=eu-central1-a,eu-central1-b \
    --gcp-region=europe-west3 --coverage-pct=100 --flex-rate=0.55 --refresh-cache

Notes:
- GCP API mode requires: google-api-python-client, google-auth and Application Default Credentials
  (gcloud auth application-default login) or GOOGLE_APPLICATION_CREDENTIALS pointing at a service
  account JSON that has roles/compute.viewer and roles/billing.viewer.
- AWS boto3 must have credentials configured for describe/pricing usage.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import csv
import json
import os
import sys
import time
import re
from pathlib import Path
from typing import Dict, Tuple

import pandas as pd
from xlsxwriter.utility import xl_col_to_name
try:
    import boto3  # type: ignore
except Exception:
    boto3 = None

# Try lazy-import GCP libs later when needed (so script can run AWS-only without google libs)
COMMITMENT_KEYWORDS = [
    ("savings plan", "Savings Plan"),
    ("savingsplan", "Savings Plan"),
    ("reserved instance", "Reserved Instances"),
    ("reserved-instances", "Reserved Instances"),
    ("reservation", "Reserved Instances"),
]

REGION_MAP = {
    "USE1": "us-east-1",
    "USE2": "us-east-2",
    "USW1": "us-west-1",
    "USW2": "us-west-2",
    "AFS1": "af-south-1",
    "APE1": "ap-east-1",
    "APN1": "ap-northeast-1",
    "APN2": "ap-northeast-2",
    "APN3": "ap-northeast-3",
    "APS1": "ap-southeast-1",
    "APS2": "ap-southeast-2",
    "APS3": "ap-south-1",
    "APS4": "ap-southeast-3",
    "APS5": "ap-south-2",
    "APS6": "ap-southeast-4",
    "EUC1": "eu-central-1",
    "EUC2": "eu-central-2",
    "EU": "eu-west-1",
    "EUW2": "eu-west-2",
    "EUW3": "eu-west-3",
    "EUN1": "eu-north-1",
    "EUS1": "eu-south-1",
    "EUS2": "eu-south-2",
    "CAN1": "ca-central-1",
    "SAE1": "sa-east-1",
    "MES1": "me-south-1",
    "MEC1": "me-central-1",
    "ILCE1": "il-central-1",
    "CNN1": "cn-north-1",
    "CNN2": "cn-northwest-1",
}

REGION_TO_LOCATION = {
    "us-east-1": "US East (N. Virginia)",
    "us-east-2": "US East (Ohio)",
    "us-west-1": "US West (N. California)",
    "us-west-2": "US West (Oregon)",
    "af-south-1": "Africa (Cape Town)",
    "ap-east-1": "Asia Pacific (Hong Kong)",
    "eu-central-1": "EU (Frankfurt)",
    "eu-central-2": "EU (Zurich)",
    "eu-west-1": "EU (Ireland)",
    "eu-west-2": "EU (London)",
    "eu-west-3": "EU (Paris)",
    "eu-north-1": "EU (Stockholm)",
    "eu-south-1": "EU (Milan)",
    "eu-south-2": "EU (Spain)",
    "ap-south-1": "Asia Pacific (Mumbai)",
    "ap-south-2": "Asia Pacific (Hyderabad)",
    "ap-southeast-1": "Asia Pacific (Singapore)",
    "ap-southeast-2": "Asia Pacific (Sydney)",
    "ap-southeast-3": "Asia Pacific (Jakarta)",
    "ap-southeast-4": "Asia Pacific (Melbourne)",
    "ap-northeast-1": "Asia Pacific (Tokyo)",
    "ap-northeast-2": "Asia Pacific (Seoul)",
    "ap-northeast-3": "Asia Pacific (Osaka)",
    "ca-central-1": "Canada (Central)",
    "sa-east-1": "South America (Sao Paulo)",
    "me-south-1": "Middle East (Bahrain)",
    "me-central-1": "Middle East (UAE)",
    "il-central-1": "Israel (Tel Aviv)",
}

PREDEFINED_INST_RE = re.compile(r"(e2|n2|n2d|c2|c2d)-(standard|highmem|highcpu)-\d+")

# baseline left empty: we will fetch dynamically
BASE_INSTANCE_SPECS: Dict[str, Tuple[float | None, float | None]] = {}

# Simple .env loader so credentials in .env are available to boto3 / GCP libs
def load_env_from_file(path: Path = Path(".env")) -> None:
    try:
        if not path.exists():
            return
        for line in path.read_text(encoding="utf-8").splitlines():
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                continue
            if "=" not in stripped:
                continue
            key, val = stripped.split("=", 1)
            key = key.strip()
            val = val.strip()
            if key and key not in os.environ:
                os.environ[key] = val
    except Exception:
        # best-effort: ignore malformed lines
        pass


def _expand_xlarge_suffix(value: str) -> str:
    text = value.strip()
    def repl(match: re.Match[str]) -> str:
        number = match.group(1)
        if number:
            return f".{number}xlarge"
        return ".xlarge"
    return re.sub(r"\.(\d*)xl\b", repl, text)


def normalize_rds_instance_class(value: str) -> tuple[str, str]:
    raw = value.strip()
    base = raw[3:] if raw.startswith("db.") else raw
    base = _expand_xlarge_suffix(base)
    display = f"db.{base}" if raw.startswith("db.") else base
    return display, base

# ----------------------------- AWS instance spec fetch -----------------------------
def fetch_instance_specs(instance_types: set[str]) -> Dict[str, Tuple[float | None, float | None]]:
    """
    Fetch vCPU and memory for instance types using EC2 describe_instance_types (batched) and
    fallback to pricing API attributes. Returns mapping lowercase -> (vcpus, memory_gib).
    """
    specs: Dict[str, Tuple[float | None, float | None]] = {}
    if not instance_types or boto3 is None:
        return specs

    originals = list(dict.fromkeys([it for it in instance_types if isinstance(it, str)]))
    remaining = {it.lower() for it in originals}

    # 1) Batched describe_instance_types
    try:
        client = boto3.client("ec2", region_name="us-east-1")
        paginator = client.get_paginator("describe_instance_types")
        for i in range(0, len(originals), 100):
            batch = originals[i : i + 100]
            try:
                for page in paginator.paginate(InstanceTypes=batch, PaginationConfig={"PageSize": 100}):
                    for itype in page.get("InstanceTypes", []):
                        name = itype.get("InstanceType")
                        vcpus = itype.get("VCpuInfo", {}).get("DefaultVCpus")
                        mem_mib = itype.get("MemoryInfo", {}).get("SizeInMiB")
                        mem_gib = float(mem_mib) / 1024.0 if mem_mib is not None else None
                        if name:
                            specs[name.lower()] = (float(vcpus) if vcpus is not None else None, mem_gib)
            except Exception:
                continue
        remaining -= set(specs.keys())
    except Exception:
        pass

    # 2) Retry describe per-instance for remaining
    if remaining and boto3 is not None:
        try:
            client = boto3.client("ec2", region_name="us-east-1")
            for inst in list(remaining):
                try:
                    resp = client.describe_instance_types(InstanceTypes=[inst])
                except Exception:
                    # try original-cased name fallback
                    orig = next((o for o in originals if o.lower() == inst), inst)
                    try:
                        resp = client.describe_instance_types(InstanceTypes=[orig])
                    except Exception:
                        continue
                for itype in resp.get("InstanceTypes", []):
                    name = itype.get("InstanceType")
                    vcpus = itype.get("VCpuInfo", {}).get("DefaultVCpus")
                    mem_mib = itype.get("MemoryInfo", {}).get("SizeInMiB")
                    mem_gib = float(mem_mib) / 1024.0 if mem_mib is not None else None
                    if name:
                        specs[name.lower()] = (float(vcpus) if vcpus is not None else None, mem_gib)
                remaining -= set(specs.keys())
                if not remaining:
                    break
        except Exception:
            pass

    # 3) Pricing API fallback (best-effort)
    if remaining and boto3 is not None:
        try:
            pricing = boto3.client("pricing", region_name="us-east-1")
            for inst in list(remaining):
                try:
                    resp = pricing.get_products(
                        ServiceCode="AmazonEC2",
                        Filters=[
                            {"Type": "TERM_MATCH", "Field": "instanceType", "Value": inst},
                            {"Type": "TERM_MATCH", "Field": "location", "Value": "US East (N. Virginia)"},
                            {"Type": "TERM_MATCH", "Field": "tenancy", "Value": "Shared"},
                            {"Type": "TERM_MATCH", "Field": "capacitystatus", "Value": "Used"},
                        ],
                        MaxResults=1,
                    )
                except Exception:
                    continue
                for price_str in resp.get("PriceList", []):
                    try:
                        data = json.loads(price_str)
                        attrs = data.get("product", {}).get("attributes", {})
                        vcpus = attrs.get("vcpu")
                        mem_str = attrs.get("memory")
                        mem_gib = None
                        if isinstance(mem_str, str):
                            import re
                            m = re.search(r"([0-9]*\.?[0-9]+)", mem_str.replace(",", ""))
                            if m:
                                val = float(m.group(1))
                                if "mib" in mem_str.lower():
                                    mem_gib = val / 1024.0
                                else:
                                    mem_gib = val
                        specs[inst.lower()] = (float(vcpus) if vcpus is not None else None, mem_gib)
                    except Exception:
                        continue
            remaining -= set(specs.keys())
        except Exception:
            pass

    if specs:
        sample = list(specs.items())[:8]
        print(f"[fetch_instance_specs] fetched {len(specs)} specs (sample): {sample}")
    if remaining:
        print(f"[fetch_instance_specs] still missing specs for: {sorted(remaining)}")

    return specs

# ----------------------------- service mapping helpers -----------------------------
SERVICE_RULES = [
    ("spot", "Spot VMs"),
    ("data transfer", "AWS DataTransfer"),
    ("bandwidth", "AWS DataTransfer"),
    ("elastic compute cloud", "Standard VMs"),
    ("ec2", "Standard VMs"),
    ("elastic block store", "Compute Storage"),
    ("ebs", "Compute Storage"),
    ("nvme", "NVMe"),
    ("relational database service", "AmazonRDS"),
    ("aurora", "AmazonRDS"),
    ("docdb", "AmazonRDS"),
    ("rds:storage", "Database Storage"),
    ("rds:chargedbackup", "Database Storage"),
    ("nat gateway", "NAT Gateway"),
    ("simple storage service", "AmazonS3"),
    ("s3", "AmazonS3"),
    ("kafka", "Amazon Apache Kafka"),
    ("cloudwatch", "AmazonCloudWatch"),
    ("waf", "AWS WAF"),
    ("elasticache", "AmazonElastiCache"),
    ("mq", "MQ"),
    ("config", "AmazonConfig"),
    ("cloudfront", "Amazon CloudFront"),
    ("guardduty", "AmazonGuardDuty"),
    ("virtual private cloud", "AmazonVPC"),
    ("vpc", "AmazonVPC"),
    ("opensearch", "OpenSearch Service"),
    ("eks", "AmazonEKS"),
    ("elastic load balanc", "AWSELB"),
    ("elastic file system", "AmazonFilesystem"),
    ("efs", "AmazonFilesystem"),
    ("backup", "AWS Backup"),
    ("route 53", "AmazonRoute53"),
    ("security hub", "AWSSecurityHub"),
    ("certificate manager", "AWSCertificate Manager"),
    ("cloudtrail", "CloudTrail"),
    ("ecr", "AmazonECR"),
    ("quicksight", "QuickSight"),
    ("direct connect", "Direct Connect"),
    ("iam", "AWS IAM"),
    ("key management service", "AWS KMS"),
    ("kms", "AWS KMS"),
    ("secrets manager", "AWSSecretsManager"),
    ("dynamodb", "AmazonDynamoDB"),
    ("simple email service", "SES"),
    ("ses", "SES"),
    ("glue", "AWSGlue"),
    ("simple notification service", "AWS NotificationService"),
    ("sns", "AWS NotificationService"),
    ("sqs", "AWS NotificationService"),
    ("redshift", "Amazon Redshift"),
    ("cost explorer", "Cost Explorer"),
    ("athena", "AmazonAthena"),
]


def canonical_service(product_name: str, usage_type: str, item_desc: str) -> str:
    text = " ".join([str(product_name or "").lower(), str(usage_type or "").lower(), str(item_desc or "").lower()])
    for needle, service in SERVICE_RULES:
        if needle in text:
            return service
    fallback = product_name if isinstance(product_name, str) else ""
    fallback = fallback.strip() or "Other"
    return fallback


def service_detail(product_name: str, usage_type: str, item_desc: str) -> str:
    text = " ".join([str(product_name or "").lower(), str(usage_type or "").lower(), str(item_desc or "").lower()])
    if "snapshot" in text:
        return "Snapshot Storage"
    if "volumeusage.gp3" in text or " gp3" in text:
        return "SSD (gp3)"
    if "volumeusage.gp2" in text or " gp2" in text:
        return "SSD (gp2)"
    if "natgateway" in text:
        return "NAT Gateway"
    return canonical_service(product_name, usage_type, item_desc)


def build_summary(df: pd.DataFrame) -> pd.DataFrame:
    required = {"ProductName", "UsageType", "ItemDescription", "TotalCost"}
    missing = required - set(df.columns)
    if missing:
        raise ValueError(f"Missing required columns: {', '.join(sorted(missing))}")
    df = df.copy()
    df["TotalCost"] = pd.to_numeric(df["TotalCost"], errors="coerce").fillna(0)
    df["Service"] = df.apply(lambda r: canonical_service(r["ProductName"], r["UsageType"], r["ItemDescription"]), axis=1)
    def on_demand(series: pd.Series) -> float:
        return series[series > 0].sum()
    summary = df.groupby("Service")["TotalCost"].agg(**{"Usage Cost": on_demand}).reset_index()
    summary = summary.sort_values("Usage Cost", ascending=False).reset_index(drop=True)
    totals = {"Service": "Total", "Usage Cost": summary["Usage Cost"].sum()}
    summary = pd.concat([summary, pd.DataFrame([totals])], ignore_index=True)
    numeric_cols = summary.select_dtypes(include="number").columns
    summary[numeric_cols] = summary[numeric_cols].where(summary[numeric_cols].abs() >= 1e-9, 0)
    return summary


def detect_commitments(df: pd.DataFrame) -> pd.DataFrame | None:
    df = df.copy()
    df["TotalCost"] = pd.to_numeric(df["TotalCost"], errors="coerce").fillna(0)
    def tag_commitment(row: pd.Series) -> str | None:
        text = " ".join([str(row.get("ProductName", "") or "").lower(), str(row.get("UsageType", "") or "").lower(), str(row.get("ItemDescription", "") or "").lower()])
        for needle, label in COMMITMENT_KEYWORDS:
            if needle in text:
                return label
        return None
    df["Commitment"] = df.apply(tag_commitment, axis=1)
    matches = df[df["Commitment"].notna()]
    if matches.empty:
        return None
    summary = matches.groupby("Commitment")["TotalCost"].agg(**{"Usage Cost (pos only)": lambda s: s[s > 0].sum(), "Net Amount": "sum", "Rows": "size"}).reset_index()
    totals = {"Commitment": "Total", "Usage Cost (pos only)": summary["Usage Cost (pos only)"].sum(), "Net Amount": summary["Net Amount"].sum(), "Rows": summary["Rows"].sum()}
    summary = pd.concat([summary, pd.DataFrame([totals])], ignore_index=True)
    numeric_cols = summary.select_dtypes(include="number").columns
    summary[numeric_cols] = summary[numeric_cols].where(summary[numeric_cols].abs() >= 1e-9, 0)
    return summary

def detect_os(row: pd.Series) -> str:
    text = " ".join([
        str(row.get("ProductName", "") or "").lower(),
        str(row.get("UsageType", "") or "").lower(),
        str(row.get("ItemDescription", "") or "").lower(),
        str(row.get("Operation", "") or "").lower(),
    ])
    if "windows" in text or "mswin" in text:
        return "Windows"
    return "Linux/Other"

def build_compute_sheet(df: pd.DataFrame, spec_map: dict[str, tuple[float | None, float | None]] | None = None) -> pd.DataFrame | None:
    mask = df["UsageType"].str.contains("BoxUsage", case=False, na=False)
    compute = df[mask].copy()
    if compute.empty:
        return None

    compute["InstanceType"] = compute["UsageType"].str.extract(r"BoxUsage:([^,]+)$")[0].str.lower()
    compute = compute[compute["InstanceType"].notna()]
    if compute.empty:
        return None

    compute["UsageQuantity"] = pd.to_numeric(compute["UsageQuantity"], errors="coerce").fillna(0)
    compute["TotalCost"] = pd.to_numeric(compute["TotalCost"], errors="coerce").fillna(0)

    grouped = (
        compute.groupby("InstanceType")
        .agg({"UsageQuantity": "sum", "TotalCost": "sum"})
        .reset_index()
        .rename(columns={"UsageQuantity": "No Of Hours", "TotalCost": "AWS Cost"})
    )

    def aws_specs(instance: str) -> tuple[float | None, float | None]:
        mapping = spec_map or BASE_INSTANCE_SPECS
        if not isinstance(instance, str):
            return (None, None)
        return mapping.get(instance.lower(), (None, None))

    grouped[["AWS Cores", "AWS Memory"]] = grouped["InstanceType"].apply(lambda it: pd.Series(aws_specs(it)))
    grouped["AWS Cores"] = pd.to_numeric(grouped["AWS Cores"], errors="coerce")
    grouped["AWS Memory"] = pd.to_numeric(grouped["AWS Memory"], errors="coerce")
    grouped["OS"] = ""
    grouped["AWS Per Hour"] = ""

    grouped = grouped.sort_values("AWS Cost", ascending=False).reset_index(drop=True)

    totals = {
        "Region": "Total",
        "OS": "",
        "InstanceType": "",
        "No Of Hours": grouped["No Of Hours"].sum(),
        "AWS Cost": grouped["AWS Cost"].sum(),
        "AWS Cores": "",
        "AWS Memory": "",
        "AWS Per Hour": "",
    }
    grouped.loc[len(grouped)] = totals

    grouped["Google Cloud Instance"] = ""
    grouped["Google Cloud Cores"] = ""
    grouped["Google Cloud Memory"] = ""
    grouped["GCP Cost"] = ""
    grouped["GCP 3 Yr Flex CUD"] = ""
    grouped["%age Diff"] = ""
    grouped["GCP Per Hour"] = ""

    grouped = grouped[
        [
            "OS",
            "InstanceType",
            "Google Cloud Instance",
            "No Of Hours",
            "AWS Cores",
            "AWS Memory",
            "AWS Cost",
            "Google Cloud Cores",
            "Google Cloud Memory",
            "GCP Cost",
            "GCP 3 Yr Flex CUD",
            "%age Diff",
            "AWS Per Hour",
            "GCP Per Hour",
        ]
    ].rename(columns={"InstanceType": "AWS Instance"})

    grouped["AWS Per Hour"] = grouped["AWS Per Hour"].fillna("")

    numeric_cols = grouped.select_dtypes(include="number").columns
    grouped[numeric_cols] = grouped[numeric_cols].where(grouped[numeric_cols].abs() >= 1e-9, 0)
    return grouped


def build_compute_by_region(
    df: pd.DataFrame, default_region: str | None = None, spec_map: dict[str, tuple[float | None, float | None]] | None = None
) -> tuple[pd.DataFrame, dict[tuple[str, str], pd.DataFrame]] | tuple[None, None]:
    mask = df["UsageType"].str.contains("BoxUsage", case=False, na=False)
    compute = df[mask].copy()
    if compute.empty:
        return None, None

    compute["InstanceType"] = compute["UsageType"].str.extract(r"BoxUsage:([^,]+)$")[0].str.lower()
    compute = compute[compute["InstanceType"].notna()]
    if compute.empty:
        return None, None

    compute["Region Code"] = compute["UsageType"].str.extract(r"^([A-Z0-9]+)-")[0]
    compute["Region"] = compute["Region Code"].map(REGION_MAP)
    if default_region is None:
        fallback = (
            compute["Region"].dropna().mode().iloc[0]
            if not compute["Region"].dropna().empty
            else "Unknown"
        )
    else:
        fallback = default_region
    compute["Region"] = compute["Region"].fillna(fallback)
    compute["UsageQuantity"] = pd.to_numeric(compute["UsageQuantity"], errors="coerce").fillna(0)
    compute["TotalCost"] = pd.to_numeric(compute["TotalCost"], errors="coerce").fillna(0)
    compute["OS"] = compute.apply(detect_os, axis=1)

    def aws_specs(instance: str) -> tuple[float | None, float | None]:
        mapping = spec_map or BASE_INSTANCE_SPECS
        if not isinstance(instance, str):
            return (None, None)
        return mapping.get(instance.lower(), (None, None))

    grouped = (
        compute.groupby(["Region", "OS", "InstanceType"])
        .agg({"UsageQuantity": "sum", "TotalCost": "sum"})
        .reset_index()
        .rename(columns={"UsageQuantity": "No Of Hours", "TotalCost": "AWS Cost"})
    )
    grouped[["AWS Cores", "AWS Memory"]] = grouped["InstanceType"].apply(lambda it: pd.Series(aws_specs(it)))
    grouped["AWS Cores"] = pd.to_numeric(grouped["AWS Cores"], errors="coerce")
    grouped["AWS Memory"] = pd.to_numeric(grouped["AWS Memory"], errors="coerce")
    grouped["AWS Cores"] = grouped.apply(
        lambda r: "" if r["InstanceType"] == "Total" else ("" if pd.isna(r["AWS Cores"]) else r["AWS Cores"]),
        axis=1,
    )
    grouped["AWS Memory"] = grouped.apply(
        lambda r: "" if r["InstanceType"] == "Total" else ("" if pd.isna(r["AWS Memory"]) else r["AWS Memory"]),
        axis=1,
    )
    grouped["AWS Per Hour"] = ""

    grouped = grouped.sort_values("AWS Cost", ascending=False).reset_index(drop=True)
    totals = {
        "Region": "Total",
        "InstanceType": "",
        "No Of Hours": grouped["No Of Hours"].sum(),
        "AWS Cost": grouped["AWS Cost"].sum(),
        "AWS Cores": "",
        "AWS Memory": "",
        "OS": "",
        "AWS Per Hour": "",
    }
    grouped.loc[len(grouped)] = totals

    if "AWS Per Hour" not in grouped.columns:
        grouped["AWS Per Hour"] = ""

    grouped = grouped[
        [
            "Region",
            "OS",
            "InstanceType",
            "No Of Hours",
            "AWS Cores",
            "AWS Memory",
            "AWS Cost",
            "AWS Per Hour",
        ]
    ].rename(columns={"InstanceType": "AWS Instance"})

    grouped["AWS Per Hour"] = grouped["AWS Per Hour"].fillna("")
    numeric_cols = grouped.select_dtypes(include="number").columns
    grouped[numeric_cols] = grouped[numeric_cols].where(grouped[numeric_cols].abs() >= 1e-9, 0)

    region_tables: dict[tuple[str, str], pd.DataFrame] = {}
    without_total = grouped[grouped["Region"] != "Total"]
    for (region, os_name), region_df in without_total.groupby(["Region", "OS"]):
        region_df = region_df.copy()
        totals = {
            "Region": region,
            "OS": os_name,
            "AWS Instance": "Total",
            "No Of Hours": region_df["No Of Hours"].sum(),
            "AWS Cores": "",
            "AWS Memory": "",
            "AWS Cost": region_df["AWS Cost"].sum(),
        }
        region_df.loc[len(region_df)] = totals
        region_tables[(region, os_name)] = region_df

    return grouped, region_tables


def build_spot_by_region(
    df: pd.DataFrame, default_region: str | None = None, spec_map: dict[str, tuple[float | None, float | None]] | None = None
) -> tuple[pd.DataFrame, dict[tuple[str, str], pd.DataFrame]] | tuple[None, None]:
    mask = (
        df["UsageType"].str.contains("spot", case=False, na=False)
        | df["ItemDescription"].str.contains("spot", case=False, na=False)
        | df["Operation"].str.contains("spot", case=False, na=False)
    )
    spot = df[mask].copy()
    if spot.empty:
        return None, None

    spot["InstanceType"] = spot["UsageType"].str.extract(r"SpotUsage[:/ -]([^,]+)")[0].str.lower()
    spot = spot[spot["InstanceType"].notna()]
    if spot.empty:
        return None, None

    spot["Region Code"] = spot["UsageType"].str.extract(r"^([A-Z0-9]+)-")[0]
    spot["Region"] = spot["Region Code"].map(REGION_MAP)
    if default_region is None:
        fallback = (
            spot["Region"].dropna().mode().iloc[0]
            if not spot["Region"].dropna().empty
            else "Unknown"
        )
    else:
        fallback = default_region
    spot["Region"] = spot["Region"].fillna(fallback)
    spot["UsageQuantity"] = pd.to_numeric(spot["UsageQuantity"], errors="coerce").fillna(0)
    spot["TotalCost"] = pd.to_numeric(spot["TotalCost"], errors="coerce").fillna(0)
    spot["OS"] = spot.apply(detect_os, axis=1)

    def aws_specs(instance: str) -> tuple[float | None, float | None]:
        mapping = spec_map or BASE_INSTANCE_SPECS
        if not isinstance(instance, str):
            return (None, None)
        return mapping.get(instance.lower(), (None, None))

    grouped = (
        spot.groupby(["Region", "OS", "InstanceType"])
        .agg({"UsageQuantity": "sum", "TotalCost": "sum"})
        .reset_index()
        .rename(columns={"UsageQuantity": "No Of Hours", "TotalCost": "AWS Cost"})
    )
    grouped[["AWS Cores", "AWS Memory"]] = grouped["InstanceType"].apply(lambda it: pd.Series(aws_specs(it)))
    grouped["AWS Cores"] = pd.to_numeric(grouped["AWS Cores"], errors="coerce")
    grouped["AWS Memory"] = pd.to_numeric(grouped["AWS Memory"], errors="coerce")

    grouped = grouped.sort_values("AWS Cost", ascending=False).reset_index(drop=True)
    totals = {
        "Region": "Total",
        "OS": "",
        "InstanceType": "",
        "No Of Hours": grouped["No Of Hours"].sum(),
        "AWS Cost": grouped["AWS Cost"].sum(),
        "AWS Cores": "",
        "AWS Memory": "",
    }
    grouped.loc[len(grouped)] = totals

    grouped = grouped[
        [
            "Region",
            "OS",
            "InstanceType",
            "No Of Hours",
            "AWS Cores",
            "AWS Memory",
            "AWS Cost",
        ]
    ].rename(columns={"InstanceType": "AWS Instance"})

    numeric_cols = grouped.select_dtypes(include="number").columns
    grouped[numeric_cols] = grouped[numeric_cols].where(grouped[numeric_cols].abs() >= 1e-9, 0)

    region_tables: dict[tuple[str, str], pd.DataFrame] = {}
    without_total = grouped[grouped["Region"] != "Total"]
    for (region, os_name), region_df in without_total.groupby(["Region", "OS"]):
        region_df = region_df.copy()
        totals = {
            "Region": region,
            "OS": os_name,
            "AWS Instance": "Total",
            "No Of Hours": region_df["No Of Hours"].sum(),
            "AWS Cores": "",
            "AWS Memory": "",
            "AWS Cost": region_df["AWS Cost"].sum(),
            "AWS Per Hour": "",
        }
        region_df.loc[len(region_df)] = totals
        region_tables[(region, os_name)] = region_df

    return grouped, region_tables


def build_service_usage_by_region(
    df: pd.DataFrame, default_region: str | None = None
) -> tuple[dict[str, pd.DataFrame], dict[str, pd.DataFrame]] | tuple[None, None]:
    df = df.copy()
    for col in ["UsageType", "ItemDescription", "ProductName", "RecordType"]:
        if col not in df.columns:
            return None, None

    df = df[~df["RecordType"].isin(["AccountTotal", "InvoiceTotal", "StatementTotal"])].copy()
    df = df[~df["UsageType"].str.contains("BoxUsage", case=False, na=False)]

    df["UsageQuantity"] = pd.to_numeric(df["UsageQuantity"], errors="coerce").fillna(0)
    df["TotalCost"] = pd.to_numeric(df["TotalCost"], errors="coerce").fillna(0)
    df = df[df["TotalCost"] > 0]
    if df.empty:
        return None, None

    location_to_region = {v.lower(): k for k, v in REGION_TO_LOCATION.items()}

    unknown_token = "Unknown"

    def region_from_row(row: pd.Series) -> str:
        usage_type = str(row.get("UsageType", "") or "")
        item_desc = str(row.get("ItemDescription", "") or "")
        region_code = None
        match = pd.Series([usage_type]).str.extract(r"^([A-Z0-9]+)-")[0].iloc[0]
        if isinstance(match, str) and match:
            region_code = REGION_MAP.get(match)
        if not region_code:
            lower_desc = item_desc.lower()
            for location, region in location_to_region.items():
                if location in lower_desc:
                    region_code = region
                    break
        if not region_code:
            return default_region or unknown_token
        return region_code

    def display_region(region: str) -> str:
        location = REGION_TO_LOCATION.get(region)
        if not location:
            return region
        if "(" in location and ")" in location:
            return location.split("(", 1)[1].split(")", 1)[0]
        return location

    def extract_unit(item_desc: str, usage_type: str) -> str:
        text = (item_desc or "").lower()
        if "gb-month" in text or "gb month" in text:
            return "GB-Mo"
        if "per gb" in text:
            return "GB"
        if "hour" in text:
            return "Hours"
        usage = (usage_type or "").lower()
        if "bytes" in usage:
            return "GB"
        if "volumeusage" in usage or "snapshot" in usage:
            return "GB-Mo"
        if "gp2-storage" in usage or "gp3-storage" in usage:
            return "GB-Mo"
        if "hours" in usage:
            return "Hours"
        return ""

    def format_qty(val: float) -> str:
        if val == 0:
            return "0"
        return f"{val:.6f}".rstrip("0").rstrip(".")

    df["Service"] = df.apply(lambda r: service_detail(r["ProductName"], r["UsageType"], r["ItemDescription"]), axis=1)
    allowed_services = {"Snapshot Storage", "SSD (gp3)", "SSD (gp2)", "NAT Gateway"}
    df = df[df["Service"].isin(allowed_services)]
    if df.empty:
        return None, None
    df["Region"] = df.apply(region_from_row, axis=1)
    known = df[df["Region"] != unknown_token]
    if not known.empty:
        region_lookup = (
            known.groupby(["Service", "UsageType"])["Region"]
            .agg(lambda s: s.mode().iloc[0] if not s.mode().empty else s.iloc[0])
        )
        def fill_region(row: pd.Series) -> str:
            if row["Region"] != unknown_token:
                return row["Region"]
            key = (row["Service"], row["UsageType"])
            if key in region_lookup.index:
                return region_lookup.loc[key]
            return row["Region"]
        df["Region"] = df.apply(fill_region, axis=1)
        if (df["Region"] == unknown_token).any():
            overall_mode = known["Region"].mode()
            fallback_region = overall_mode.iloc[0] if not overall_mode.empty else (default_region or unknown_token)
            df.loc[df["Region"] == unknown_token, "Region"] = fallback_region
    df["Region Display"] = df["Region"].map(display_region)
    df["Unit"] = df.apply(lambda r: extract_unit(str(r["ItemDescription"] or ""), str(r["UsageType"] or "")), axis=1)

    nat_df = df[df["Service"] == "NAT Gateway"].copy()
    storage_df = df[df["Service"] != "NAT Gateway"].copy()

    grouped = (
        storage_df.groupby(["Region Display", "Service", "Unit"])
        .agg({"UsageQuantity": "sum", "TotalCost": "sum"})
        .reset_index()
        .rename(columns={"Region Display": "Region", "UsageQuantity": "Usage Qty", "TotalCost": "Cost"})
    )

    region_tables: dict[str, pd.DataFrame] = {}
    for region, region_df in grouped.groupby("Region"):
        region_df = region_df.copy()
        region_df["Usage"] = region_df.apply(
            lambda r: f"{format_qty(r['Usage Qty'])} {r['Unit']}".strip(),
            axis=1,
        )
        region_df = region_df[["Service", "Usage", "Region", "Cost"]]
        region_df = region_df.sort_values("Cost", ascending=False).reset_index(drop=True)
        totals = {"Service": "Total", "Usage": "", "Region": "", "Cost": region_df["Cost"].sum()}
        region_df.loc[len(region_df)] = totals
        region_tables[region] = region_df

    nat_grouped = (
        nat_df.groupby(["Region Display", "Service", "Unit"])
        .agg({"UsageQuantity": "sum", "TotalCost": "sum"})
        .reset_index()
        .rename(columns={"Region Display": "Region", "UsageQuantity": "Usage Qty", "TotalCost": "Cost"})
    )
    nat_tables: dict[str, pd.DataFrame] = {}
    for region, region_df in nat_grouped.groupby("Region"):
        region_df = region_df.copy()
        region_df["Usage"] = region_df.apply(
            lambda r: f"{format_qty(r['Usage Qty'])} {r['Unit']}".strip(),
            axis=1,
        )
        region_df = region_df[["Service", "Usage", "Region", "Cost"]]
        region_df = region_df.sort_values("Cost", ascending=False).reset_index(drop=True)
        totals = {"Service": "Total", "Usage": "", "Region": "", "Cost": region_df["Cost"].sum()}
        region_df.loc[len(region_df)] = totals
        nat_tables[region] = region_df

    return region_tables, nat_tables


def build_rds_usage_by_region(
    df: pd.DataFrame,
    default_region: str | None = None,
    spec_map: dict[str, tuple[float | None, float | None]] | None = None,
    gcp_region_map: dict[str, str] | None = None,
    gcp_sql_rates: dict[str, dict[str, float]] | None = None,
    gcp_cpu_mem_rates: dict[str, dict[str, float]] | None = None,
) -> dict[str, list[tuple[str, pd.DataFrame]]] | None:
    df = df.copy()
    for col in ["UsageType", "ItemDescription", "ProductName", "RecordType"]:
        if col not in df.columns:
            return None

    df = df[~df["RecordType"].isin(["AccountTotal", "InvoiceTotal", "StatementTotal"])].copy()
    df["UsageQuantity"] = pd.to_numeric(df["UsageQuantity"], errors="coerce").fillna(0)
    df["TotalCost"] = pd.to_numeric(df["TotalCost"], errors="coerce").fillna(0)
    df = df[df["TotalCost"] > 0]
    if df.empty:
        return None

    def is_rds(row: pd.Series) -> bool:
        text = " ".join(
            [
                str(row.get("ProductName", "") or "").lower(),
                str(row.get("UsageType", "") or "").lower(),
                str(row.get("ItemDescription", "") or "").lower(),
            ]
        )
        return "relational database service" in text or "rds" in text

    df = df[df.apply(is_rds, axis=1)]
    if df.empty:
        return None

    location_to_region = {v.lower(): k for k, v in REGION_TO_LOCATION.items()}
    unknown_token = "Unknown"

    def region_from_row(row: pd.Series) -> str:
        usage_type = str(row.get("UsageType", "") or "")
        item_desc = str(row.get("ItemDescription", "") or "")
        region_code = None
        match = pd.Series([usage_type]).str.extract(r"^([A-Z0-9]+)-")[0].iloc[0]
        if isinstance(match, str) and match:
            region_code = REGION_MAP.get(match)
        if not region_code:
            lower_desc = item_desc.lower()
            for location, region in location_to_region.items():
                if location in lower_desc:
                    region_code = region
                    break
        if not region_code:
            return default_region or unknown_token
        return region_code

    def display_region(region: str) -> str:
        location = REGION_TO_LOCATION.get(region)
        if not location:
            return region
        if "(" in location and ")" in location:
            return location.split("(", 1)[1].split(")", 1)[0]
        return location

    def extract_unit(item_desc: str, usage_type: str) -> str:
        text = (item_desc or "").lower()
        if "gb-month" in text or "gb month" in text:
            return "GB-Mo"
        if "per gb" in text:
            return "GB"
        if "hour" in text:
            return "Hours"
        usage = (usage_type or "").lower()
        if "bytes" in usage:
            return "GB"
        if "storage" in usage or "snapshot" in usage:
            return "GB-Mo"
        if "hours" in usage:
            return "Hours"
        return ""

    def format_qty(val: float) -> str:
        if val == 0:
            return "0"
        return f"{val:.6f}".rstrip("0").rstrip(".")

    def rds_engine(row: pd.Series) -> str:
        text = str(row.get("ItemDescription", "") or "").lower()
        if "aurora postgresql" in text:
            return "Aurora PostgreSQL"
        if "aurora mysql" in text:
            return "Aurora MySQL"
        if "postgresql" in text:
            return "PostgreSQL"
        if "mysql" in text:
            return "MySQL"
        if "mariadb" in text:
            return "MariaDB"
        if "oracle" in text:
            return "Oracle"
        if "sql server" in text:
            return "SQL Server"
        return "Other"

    def extract_instance_class(row: pd.Series) -> str | None:
        usage = str(row.get("UsageType", "") or "")
        item = str(row.get("ItemDescription", "") or "")
        for pattern in [r"(?:InstanceUsageIOOptimized|InstanceUsage|Multi-AZUsage|HeavyUsage):([^,]+)", r"(db\\.[a-z0-9.-]+)"]:
            match = pd.Series([usage]).str.extract(pattern)[0].iloc[0]
            if isinstance(match, str) and match:
                return match.strip()
            match = pd.Series([item]).str.extract(pattern)[0].iloc[0]
            if isinstance(match, str) and match:
                return match.strip()
        return None

    def rds_storage_category(row: pd.Series) -> str:
        usage = str(row.get("UsageType", "") or "").lower()
        item = str(row.get("ItemDescription", "") or "").lower()
        text = f"{usage} {item}"
        if "serverlessv2iooptimizedusage" in text:
            return "Aurora Serverless v2 IO Optimized"
        if "serverlessv2usage" in text:
            return "Aurora Serverless v2"
        if "proxy" in text:
            return "RDS Proxy"
        if "dashboard" in text:
            return "Dashboards"
        if "cpucredits" in text:
            return "CPU Credits"
        if "billedoutgoingbytes" in text or "outgoingbytes" in text:
            return "Data Transfer Out"
        if "piops" in text:
            return "PIOPS"
        if "snapshotexporttos3" in text or "snapshot export" in text:
            return "Snapshot Export"
        if "chargedbackup" in text or "backup" in text:
            return "backup storage"
        if "storageiousage" in text or "i/o" in text:
            return "Storage IO"
        if "aurora:storageusage" in text:
            return "Storage (aurora)"
        if "multi-az" in text and "gp3" in text:
            return "Storage (gp3 Multi-AZ)"
        if "gp3" in text:
            return "Storage (gp3)"
        if "gp2" in text:
            return "Storage (gp2)"
        if "storage" in text:
            return "Storage"
        raw_usage = str(row.get("UsageType", "") or "").strip()
        raw_item = str(row.get("ItemDescription", "") or "").strip()
        return raw_usage or raw_item or "Other RDS"

    df["Region"] = df.apply(region_from_row, axis=1)
    known = df[df["Region"] != unknown_token]
    if not known.empty:
        region_lookup = (
            known.groupby(["UsageType"])["Region"]
            .agg(lambda s: s.mode().iloc[0] if not s.mode().empty else s.iloc[0])
        )
        def fill_region(row: pd.Series) -> str:
            if row["Region"] != unknown_token:
                return row["Region"]
            key = row["UsageType"]
            if key in region_lookup.index:
                return region_lookup.loc[key]
            return row["Region"]
        df["Region"] = df.apply(fill_region, axis=1)
        if (df["Region"] == unknown_token).any():
            overall_mode = known["Region"].mode()
            fallback_region = overall_mode.iloc[0] if not overall_mode.empty else (default_region or unknown_token)
            df.loc[df["Region"] == unknown_token, "Region"] = fallback_region

    df["Region Display"] = df["Region"].map(display_region)
    df["Unit"] = df.apply(lambda r: extract_unit(str(r["ItemDescription"] or ""), str(r["UsageType"] or "")), axis=1)
    df["Engine"] = df.apply(rds_engine, axis=1)
    df["InstanceClass"] = df.apply(extract_instance_class, axis=1)
    df[["InstanceClassDisplay", "InstanceClassLookup"]] = df["InstanceClass"].apply(
        lambda v: pd.Series(normalize_rds_instance_class(v)) if isinstance(v, str) and v else pd.Series([None, None])
    )

    instance_df = df[df["InstanceClassLookup"].notna()].copy()
    storage_df = df[df["InstanceClassLookup"].isna()].copy()
    storage_df["Service"] = storage_df.apply(rds_storage_category, axis=1)
    serverless_df = storage_df[
        storage_df["UsageType"].str.contains("ServerlessV2", case=False, na=False)
        | storage_df["ItemDescription"].str.contains("ServerlessV2", case=False, na=False)
    ].copy()
    storage_df = storage_df.drop(serverless_df.index)

    def rds_specs(instance_class: str) -> tuple[float | None, float | None]:
        mapping = spec_map or BASE_INSTANCE_SPECS
        if not isinstance(instance_class, str):
            return (None, None)
        key = instance_class.lower().replace("db.", "", 1)
        return mapping.get(key, (None, None))

    tables_by_region: dict[str, list[tuple[str, pd.DataFrame]]] = {}
    if not instance_df.empty:
        instance_df["Is IO Optimized"] = instance_df["UsageType"].str.contains("InstanceUsageIOOptimized", case=False, na=False)
        for io_flag, subset in instance_df.groupby("Is IO Optimized"):
            grouped = (
                subset.groupby(["Region", "Region Display", "Engine", "InstanceClassDisplay", "InstanceClassLookup"])
                .agg({"UsageQuantity": "sum", "TotalCost": "sum"})
                .reset_index()
            )
            grouped[["Cores", "RAM"]] = grouped["InstanceClassLookup"].apply(lambda it: pd.Series(rds_specs(it)))
            grouped["Cores"] = pd.to_numeric(grouped["Cores"], errors="coerce")
            grouped["RAM"] = pd.to_numeric(grouped["RAM"], errors="coerce")

            for (region_code, region_display, engine), engine_df in grouped.groupby(["Region", "Region Display", "Engine"]):
                engine_df = engine_df.copy()
                engine_df = engine_df.drop(columns=["Region"], errors="ignore")
                header = f"AWS ({engine} IO Optimized)" if io_flag else f"AWS ({engine})"
                engine_df = engine_df.rename(
                    columns={
                        "InstanceClassDisplay": header,
                        "UsageQuantity": "Hours",
                        "TotalCost": "Cost",
                        "Region Display": "Region",
                    }
                )
                gcp_region = None
                rates = {}
                cpu_mem = {}
                if gcp_region_map and gcp_sql_rates:
                    gcp_region = gcp_region_map.get(region_code)
                    rates = gcp_sql_rates.get(gcp_region or "", {})
                    if not rates and gcp_sql_rates:
                        rates = next(iter(gcp_sql_rates.values()))
                    if gcp_cpu_mem_rates:
                        cpu_mem = gcp_cpu_mem_rates.get(gcp_region or "", {})
                        if not cpu_mem and gcp_cpu_mem_rates:
                            cpu_mem = next(iter(gcp_cpu_mem_rates.values()))

                def _gcp_tier(row: pd.Series) -> str | None:
                    cores = row.get("Cores")
                    ram = row.get("RAM")
                    tier = choose_cloudsql_tier(cores, ram)
                    if tier:
                        return tier
                    if pd.notna(cores) and pd.notna(ram):
                        adj_cores = adjust_gcp_custom_cores(float(cores), float(ram))
                        return format_cloudsql_custom(adj_cores, float(ram))
                    return None

                engine_df["GCP equi Inst"] = engine_df.apply(_gcp_tier, axis=1)
                engine_df["Quantity"] = engine_df["Hours"].apply(lambda h: (float(h) / 720.0) if pd.notna(h) else pd.NA)
                engine_df["GCP Cores"] = engine_df.apply(
                    lambda r: adjust_gcp_custom_cores(float(r["Cores"]), float(r["RAM"]))
                    if pd.notna(r["Cores"]) and pd.notna(r["RAM"]) else pd.NA,
                    axis=1,
                )
                engine_df["GCP RAM"] = engine_df["RAM"]

                def _payg(row: pd.Series):
                    tier = row.get("GCP equi Inst")
                    hours = row.get("Hours")
                    if tier and pd.notna(hours):
                        if tier in rates:
                            rate = float(rates[tier])
                            if rate <= 0:
                                return pd.NA
                            return float(hours) * rate
                        if tier.startswith("db-custom-"):
                            cpu_rate = cpu_mem.get("cpu_per_core_hour")
                            mem_rate = cpu_mem.get("mem_per_gib_hour")
                            gcp_cores = row.get("GCP Cores")
                            gcp_ram = row.get("GCP RAM")
                            if cpu_rate and mem_rate and pd.notna(gcp_cores) and pd.notna(gcp_ram):
                                return float(hours) * (float(cpu_rate) * float(gcp_cores) + float(mem_rate) * float(gcp_ram))
                    return pd.NA

                engine_df["PAYG"] = engine_df.apply(_payg, axis=1)
                def _gcp_hourly(row: pd.Series):
                    tier = row.get("GCP equi Inst")
                    if tier in rates:
                        rate = float(rates[tier])
                        return rate if rate > 0 else pd.NA
                    if tier and tier.startswith("db-custom-"):
                        cpu_rate = cpu_mem.get("cpu_per_core_hour")
                        mem_rate = cpu_mem.get("mem_per_gib_hour")
                        gcp_cores = row.get("GCP Cores")
                        gcp_ram = row.get("GCP RAM")
                        if cpu_rate and mem_rate and pd.notna(gcp_cores) and pd.notna(gcp_ram):
                            return float(cpu_rate) * float(gcp_cores) + float(mem_rate) * float(gcp_ram)
                    return pd.NA
                engine_df["GCP Per Hour"] = engine_df.apply(_gcp_hourly, axis=1)
                engine_df["1 Yr CUD"] = engine_df["PAYG"] * GCP_CUD_1YR
                engine_df["3 Yr CUD"] = engine_df["PAYG"] * GCP_CUD_3YR

                base_cols = [header, "Hours", "Region", "Cores", "RAM", "Cost"]
                gcp_cols = []
                for col in ["GCP equi Inst", "GCP Cores", "GCP RAM", "Quantity", "GCP Per Hour", "PAYG", "1 Yr CUD", "3 Yr CUD"]:
                    if col in engine_df.columns:
                        gcp_cols.append(col)
                if gcp_cols:
                    engine_df.insert(len(base_cols), " ", "")
                    engine_df.insert(len(base_cols) + 1, "  ", "")
                engine_df = engine_df[
                    base_cols + ([" ", "  "] if gcp_cols else []) + gcp_cols
                ].sort_values("Cost", ascending=False).reset_index(drop=True)

                totals = {
                    header: "Total",
                    "Hours": "",
                    "Region": "",
                    "Cores": "",
                    "RAM": "",
                    "Cost": engine_df["Cost"].sum(),
                }
                if "GCP equi Inst" in engine_df.columns:
                    totals["GCP equi Inst"] = "Total"
                if "GCP Cores" in engine_df.columns:
                    totals["GCP Cores"] = ""
                if "GCP RAM" in engine_df.columns:
                    totals["GCP RAM"] = ""
                if "Quantity" in engine_df.columns:
                    totals["Quantity"] = ""
                if "GCP Per Hour" in engine_df.columns:
                    totals["GCP Per Hour"] = ""
                if "PAYG" in engine_df.columns:
                    totals["PAYG"] = engine_df["PAYG"].sum()
                    if "1 Yr CUD" in engine_df.columns:
                        totals["1 Yr CUD"] = engine_df["1 Yr CUD"].sum()
                    if "3 Yr CUD" in engine_df.columns:
                        totals["3 Yr CUD"] = engine_df["3 Yr CUD"].sum()
                engine_df.loc[len(engine_df)] = totals
                tables_by_region.setdefault(region_display, []).append((header, engine_df))

    if not storage_df.empty:
        table_map = {
            "Storage IO": "Storage",
            "Storage (aurora)": "Storage",
            "Storage (gp2)": "Storage",
            "Storage (gp3)": "Storage",
            "Storage (gp3 Multi-AZ)": "Storage",
            "Storage": "Storage",
            "backup storage": "Storage",
            "Snapshot Export": "Storage",
            "PIOPS": "IOPS",
            "RDS Proxy": "RDS Proxy",
            "CPU Credits": "CPU Credits",
            "Dashboards": "Dashboards",
            "Data Transfer Out": "Data Transfer",
        }
        storage_df["Table"] = storage_df["Service"].map(table_map).fillna("Other")
        grouped = (
            storage_df.groupby(["Region Display", "Table", "Service", "Unit"])
            .agg({"UsageQuantity": "sum", "TotalCost": "sum"})
            .reset_index()
            .rename(columns={"Region Display": "Region", "UsageQuantity": "Usage Qty", "TotalCost": "Cost"})
        )
        for (region, table_name), region_df in grouped.groupby(["Region", "Table"]):
            region_df = region_df.copy()
            region_df["Usage"] = region_df.apply(
                lambda r: f"{format_qty(r['Usage Qty'])} {r['Unit']}".strip(),
                axis=1,
            )
            region_df = region_df[["Service", "Usage", "Region", "Cost"]]
            region_df = region_df.sort_values("Cost", ascending=False).reset_index(drop=True)
            totals = {"Service": "Total", "Usage": "", "Region": "", "Cost": region_df["Cost"].sum()}
            region_df.loc[len(region_df)] = totals
            tables_by_region.setdefault(region, []).append((table_name, region_df))

    if not serverless_df.empty:
        serverless_df["Is IO Optimized"] = serverless_df["Service"] == "Aurora Serverless v2 IO Optimized"
        grouped = (
            serverless_df.groupby(["Region Display", "Is IO Optimized"])
            .agg({"UsageQuantity": "sum", "TotalCost": "sum"})
            .reset_index()
            .rename(columns={"Region Display": "Region", "UsageQuantity": "Hours", "TotalCost": "Cost"})
        )
        for _, row in grouped.iterrows():
            region = row["Region"]
            header = "AWS (Aurora Serverless v2 IO Optimized)" if row["Is IO Optimized"] else "AWS (Aurora Serverless v2)"
            region_df = pd.DataFrame(
                [
                    {
                        header: "Serverless v2",
                        "Hours": row["Hours"],
                        "Region": region,
                        "Cost": row["Cost"],
                    },
                    {
                        header: "Total",
                        "Hours": "",
                        "Region": "",
                        "Cost": row["Cost"],
                    },
                ]
            )
            tables_by_region.setdefault(region, []).append((header, region_df))

    return tables_by_region


def build_reserved_by_region(
    df: pd.DataFrame, default_region: str | None = None, spec_map: dict[str, tuple[float | None, float | None]] | None = None
) -> tuple[pd.DataFrame, dict[tuple[str, str], pd.DataFrame]] | tuple[None, None]:
    mask = (
        df["UsageType"].str.contains("reserved", case=False, na=False)
        | df["ItemDescription"].str.contains("reserved", case=False, na=False)
        | df["Operation"].str.contains("reserved", case=False, na=False)
    )
    ri = df[mask].copy()
    if ri.empty:
        return None, None

    ri["InstanceType"] = ri["UsageType"].str.extract(r"BoxUsage:([^,]+)$")[0].str.lower()
    ri = ri[ri["InstanceType"].notna()]
    if ri.empty:
        return None, None

    ri["Region Code"] = ri["UsageType"].str.extract(r"^([A-Z0-9]+)-")[0]
    ri["Region"] = ri["Region Code"].map(REGION_MAP)
    if default_region is None:
        fallback = (
            ri["Region"].dropna().mode().iloc[0]
            if not ri["Region"].dropna().empty
            else "Unknown"
        )
    else:
        fallback = default_region
    ri["Region"] = ri["Region"].fillna(fallback)
    ri["UsageQuantity"] = pd.to_numeric(ri["UsageQuantity"], errors="coerce").fillna(0)
    ri["TotalCost"] = pd.to_numeric(ri["TotalCost"], errors="coerce").fillna(0)
    ri["OS"] = ri.apply(detect_os, axis=1)

    def aws_specs(instance: str) -> tuple[float | None, float | None]:
        mapping = spec_map or BASE_INSTANCE_SPECS
        if not isinstance(instance, str):
            return (None, None)
        key = instance.lower()
        return mapping.get(key, (None, None))

    grouped = (
        ri.groupby(["Region", "OS", "InstanceType"])
        .agg({"UsageQuantity": "sum", "TotalCost": "sum"})
        .reset_index()
        .rename(columns={"UsageQuantity": "No Of Hours", "TotalCost": "AWS Cost"})
    )
    grouped[["AWS Cores", "AWS Memory"]] = grouped["InstanceType"].apply(lambda it: pd.Series(aws_specs(it)))
    grouped["AWS Cores"] = pd.to_numeric(grouped["AWS Cores"], errors="coerce")
    grouped["AWS Memory"] = pd.to_numeric(grouped["AWS Memory"], errors="coerce")
    grouped["AWS Cores"] = grouped.apply(
        lambda r: "" if r["InstanceType"] == "Total" else ("" if pd.isna(r["AWS Cores"]) else r["AWS Cores"]),
        axis=1,
    )
    grouped["AWS Memory"] = grouped.apply(
        lambda r: "" if r["InstanceType"] == "Total" else ("" if pd.isna(r["AWS Memory"]) else r["AWS Memory"]),
        axis=1,
    )

    grouped = grouped.sort_values("AWS Cost", ascending=False).reset_index(drop=True)
    totals = {
        "Region": "Total",
        "OS": "",
        "InstanceType": "",
        "No Of Hours": grouped["No Of Hours"].sum(),
        "AWS Cost": grouped["AWS Cost"].sum(),
        "AWS Cores": "",
        "AWS Memory": "",
    }
    grouped.loc[len(grouped)] = totals

    grouped = grouped[
        [
            "Region",
            "OS",
            "InstanceType",
            "No Of Hours",
            "AWS Cores",
            "AWS Memory",
            "AWS Cost",
        ]
    ].rename(columns={"InstanceType": "AWS Instance"})

    numeric_cols = grouped.select_dtypes(include="number").columns
    grouped[numeric_cols] = grouped[numeric_cols].where(grouped[numeric_cols].abs() >= 1e-9, 0)

    region_tables: dict[tuple[str, str], pd.DataFrame] = {}
    without_total = grouped[grouped["Region"] != "Total"]
    for (region, os_name), region_df in without_total.groupby(["Region", "OS"]):
        region_df = region_df.copy()
        totals = {
            "Region": region,
            "OS": os_name,
            "AWS Instance": "Total",
            "No Of Hours": region_df["No Of Hours"].sum(),
            "AWS Cores": "",
            "AWS Memory": "",
            "AWS Cost": region_df["AWS Cost"].sum(),
            "AWS Per Hour": "",
        }
        region_df.loc[len(region_df)] = totals
        region_tables[(region, os_name)] = region_df

    return grouped, region_tables


def build_coverage_scenario(total_on_demand: float, coverage_pct: float, flex_effective_rate: float, resource_commit: float) -> pd.DataFrame:
    covered_value = total_on_demand * (coverage_pct / 100.0)
    uncovered_value = total_on_demand - covered_value
    flex_cud_charge = covered_value * flex_effective_rate
    total_cost = flex_cud_charge + uncovered_value + resource_commit

    scenario = pd.DataFrame(
        [
            {"Item": "On-demand Cost", "Amount": total_on_demand},
            {"Item": f"{coverage_pct:.2f}% Split (covered)", "Amount": covered_value},
            {"Item": "Flex CUD Charge", "Amount": flex_cud_charge},
            {"Item": "Uncovered On-demand", "Amount": uncovered_value},
            {"Item": "Resource Commitment", "Amount": resource_commit},
            {"Item": "Total Scenario Cost", "Amount": total_cost},
        ]
    )

    numeric_cols = scenario.select_dtypes(include="number").columns
    scenario[numeric_cols] = scenario[numeric_cols].where(scenario[numeric_cols].abs() >= 1e-9, 0)
    return scenario


def fetch_on_demand_rates(
    instances_per_region_os: dict[tuple[str, str], set[str]]
) -> dict[tuple[str, str, str], float]:
    rates: dict[tuple[str, str, str], float] = {}
    if boto3 is None:
        return rates

    client = boto3.client("pricing", region_name="us-east-1")
    for (region, os_name), instances in instances_per_region_os.items():
        location = REGION_TO_LOCATION.get(region)
        if not location:
            continue
        os_key = "Windows" if "windows" in str(os_name).lower() else "Linux"
        for instance in instances:
            try:
                resp = client.get_products(
                    ServiceCode="AmazonEC2",
                    Filters=[
                        {"Type": "TERM_MATCH", "Field": "instanceType", "Value": instance},
                        {"Type": "TERM_MATCH", "Field": "location", "Value": location},
                        {"Type": "TERM_MATCH", "Field": "operatingSystem", "Value": os_key},
                        {"Type": "TERM_MATCH", "Field": "preInstalledSw", "Value": "NA"},
                        {"Type": "TERM_MATCH", "Field": "tenancy", "Value": "Shared"},
                        {"Type": "TERM_MATCH", "Field": "capacitystatus", "Value": "Used"},
                    ],
                    MaxResults=1,
                )
            except Exception:
                continue

            for price_str in resp.get("PriceList", []):
                try:
                    data = json.loads(price_str)
                    terms = data.get("terms", {}).get("OnDemand", {})
                    for term in terms.values():
                        for pd in term.get("priceDimensions", {}).values():
                            usd = pd.get("pricePerUnit", {}).get("USD")
                            if usd is not None:
                                rates[(region, instance, os_key)] = float(usd)
                                raise StopIteration
                except StopIteration:
                    break
                except Exception:
                    continue
    return rates


def apply_on_demand_rates(
    df: pd.DataFrame,
    rates: dict[tuple[str, str, str], float],
    fill_missing_cost: bool = True,
) -> pd.DataFrame:
    df = df.copy()
    def _lookup(row: pd.Series) -> float | str:
        if row.get("AWS Instance") == "Total" or row.get("Region") == "Total":
            return pd.NA
        region = row.get("Region")
        inst = row.get("AWS Instance")
        os_name = row.get("OS") or "Linux"
        os_key = "Windows" if "windows" in str(os_name).lower() else "Linux"
        return rates.get((region, inst, os_key), None)

    def _apply(row: pd.Series) -> float | str:
        rate = _lookup(row)
        if rate is None:
            return pd.NA
        return rate

    df["AWS Per Hour"] = df.apply(_apply, axis=1)
    if fill_missing_cost:
        def _calc_cost(row: pd.Series):
            if row.get("AWS Instance") == "Total" or row.get("Region") == "Total":
                return row.get("AWS Cost")
            cost = row.get("AWS Cost")
            rate = row.get("AWS Per Hour")
            hours = row.get("No Of Hours")
            try:
                cost_val = float(cost)
            except Exception:
                cost_val = None
            if (cost_val is None or cost_val == 0) and pd.notna(rate) and pd.notna(hours):
                try:
                    return float(hours) * float(rate)
                except Exception:
                    return cost
            return cost

        df["AWS Cost"] = df.apply(_calc_cost, axis=1)

        if {"Region", "OS", "AWS Instance", "AWS Cost"} <= set(df.columns):
            data_rows = df[df["AWS Instance"] != "Total"]
            for (region, os_name), group in data_rows.groupby(["Region", "OS"]):
                mask = (df["Region"] == region) & (df["OS"] == os_name) & (df["AWS Instance"] == "Total")
                if mask.any():
                    df.loc[mask, "AWS Cost"] = group["AWS Cost"].sum()
                    if "No Of Hours" in df.columns:
                        df.loc[mask, "No Of Hours"] = group["No Of Hours"].sum()
            total_mask = (df["Region"] == "Total")
            if total_mask.any():
                non_total = df[(df["Region"] != "Total") & (df["AWS Instance"] != "Total")]
                df.loc[total_mask, "AWS Cost"] = non_total["AWS Cost"].sum()
                if "No Of Hours" in df.columns:
                    df.loc[total_mask, "No Of Hours"] = non_total["No Of Hours"].sum()
    return df


def add_gcp_compute_mapping(
    df: pd.DataFrame,
    region_to_gcp: dict[str, str],
    gcp_cpu_mem_rates: dict[str, dict[str, float]],
    zones_map: dict[str, dict[str, dict[str, int]]],
    family_preference: list[str] | None = None,
    instance_map: dict[str, dict[str, float | str | None]] | None = None,
) -> pd.DataFrame:
    df = df.copy()
    hours_per_month = 730.0
    df["GCP equi Inst"] = ""
    df["GCP Cores"] = ""
    df["GCP RAM"] = ""
    df["Quantity"] = ""
    df["PAYG"] = ""
    df["GCP Per Hour"] = ""
    df["1 Yr CUD"] = ""
    df["3 Yr CUD"] = ""

    def _family_rates(rates: dict[str, float], family: str) -> tuple[float | None, float | None]:
        family_rates = rates.get("family_rates", {}) if isinstance(rates, dict) else {}
        fam = family_rates.get(family, {}) if isinstance(family_rates, dict) else {}
        cpu_rate = fam.get("cpu") if isinstance(fam, dict) else None
        mem_rate = fam.get("mem") if isinstance(fam, dict) else None
        if family == "e2" and (cpu_rate is None or mem_rate is None):
            return None, None
        if cpu_rate is None:
            cpu_rate = rates.get("cpu_per_core_hour")
        if mem_rate is None:
            mem_rate = rates.get("mem_per_gib_hour")
        return cpu_rate, mem_rate

    def _compute_custom_hourly(rates: dict[str, float], cores: float, mem: float):
        cpu_rate = rates.get("cpu_per_core_hour")
        mem_rate = rates.get("mem_per_gib_hour")
        if cpu_rate is None or mem_rate is None or cpu_rate <= 0 or mem_rate <= 0:
            return pd.NA
        return float(cpu_rate) * float(cores) + float(mem_rate) * float(mem)

    def exact_gcp_hourly(machine: str, family: str, cores: float, mem: float, rates: dict[str, float]):
        predefined = rates.get("predefined_prices", {}) if isinstance(rates, dict) else {}
        if machine in predefined:
            return predefined[machine]
        if "-custom-" in machine:
            return _compute_custom_hourly(rates, cores, mem)
        if family == "e2":
            return pd.NA
        cpu_rate, mem_rate = _family_rates(rates, family)
        if cpu_rate is None or mem_rate is None or cpu_rate <= 0 or mem_rate <= 0:
            return pd.NA
        return float(cpu_rate) * float(cores) + float(mem_rate) * float(mem)

    def _pick_candidate(family: str, cores: float, mem: float, rates: dict[str, float]):
        exact = find_exact_gcp_machine(zones_map, int(cores), float(mem), [family])
        if exact:
            _, gcp_machine = exact
            gcp_cores = cores
            gcp_ram = mem
            gcp_hourly = exact_gcp_hourly(gcp_machine, family, gcp_cores, gcp_ram, rates)
        else:
            gcp_cores = adjust_gcp_custom_cores(cores, mem)
            mem_mb = int(round(mem * 1024))
            gcp_machine = f"{family}-custom-{int(gcp_cores)}-{mem_mb}"
            gcp_ram = mem
            gcp_hourly = exact_gcp_hourly(gcp_machine, family, gcp_cores, gcp_ram, rates)
        return {
            "gcp_machine": gcp_machine,
            "gcp_cores": gcp_cores,
            "gcp_ram": gcp_ram,
            "gcp_hourly": gcp_hourly,
            "family": family,
        }

    def _calc(row: pd.Series):
        if row.get("AWS Instance") == "Total" or row.get("Region") == "Total":
            return ("", "", "", "", "", "", "", "")
        region = row.get("Region")
        gcp_region = region_to_gcp.get(region)
        if not gcp_region or gcp_region not in gcp_cpu_mem_rates:
            raise ValueError(f"No GCP pricing for region {gcp_region}")
        rates = gcp_cpu_mem_rates.get(gcp_region, {})
        vcpus = row.get("AWS Cores")
        mem = row.get("AWS Memory")
        hours = row.get("No Of Hours")
        if pd.isna(vcpus) or pd.isna(mem) or pd.isna(hours):
            return ("", "", "", "", "", "", "", "")
        vcpus = float(vcpus)
        mem = float(mem)
        aws_instance = str(row.get("AWS Instance") or "")
        mapping = instance_map.get(aws_instance.lower()) if instance_map else None
        pref = family_preference or build_gcp_family_preference(aws_instance)
        candidate_families = []
        for fam in (pref or []):
            if fam not in candidate_families:
                candidate_families.append(fam)
        for fam in ["e2", "n2d", "n2", "c2d", "c2"]:
            if fam not in candidate_families:
                candidate_families.append(fam)

        candidates = []
        if mapping:
            mapped_machine = str(mapping.get("gcp_inst") or "")
            mapped_cores = float(mapping.get("gcp_vcpu") or vcpus)
            mapped_ram = float(mapping.get("gcp_mem") or mem)
            mapped_family = mapped_machine.split("-", 1)[0] if mapped_machine else ""
            mapped_hourly = exact_gcp_hourly(mapped_machine, mapped_family, mapped_cores, mapped_ram, rates) if mapped_family else pd.NA
            candidates.append({
                "gcp_machine": mapped_machine,
                "gcp_cores": mapped_cores,
                "gcp_ram": mapped_ram,
                "gcp_hourly": mapped_hourly,
                "family": mapped_family,
                "mapped": True,
            })

        for fam in candidate_families:
            candidates.append(_pick_candidate(fam, vcpus, mem, rates))

        # choose cheapest hourly; tie-break by candidate_families order
        best = None
        for cand in candidates:
            hourly = cand.get("gcp_hourly")
            if pd.isna(hourly):
                continue
            if best is None or float(hourly) < float(best.get("gcp_hourly")):
                best = cand
            elif best is not None and float(hourly) == float(best.get("gcp_hourly")):
                best_fam = best.get("family") or ""
                cand_fam = cand.get("family") or ""
                if cand_fam in candidate_families and best_fam in candidate_families:
                    if candidate_families.index(cand_fam) < candidate_families.index(best_fam):
                        best = cand

        if best is None and candidates:
            best = candidates[0]

        gcp_machine = best.get("gcp_machine") if best else ""
        gcp_cores = best.get("gcp_cores") if best else ""
        gcp_ram = best.get("gcp_ram") if best else ""
        gcp_hourly = best.get("gcp_hourly") if best else pd.NA
        quantity = pd.NA
        if pd.notna(hours):
            try:
                quantity = float(hours) / hours_per_month
            except Exception:
                quantity = pd.NA
        if pd.isna(gcp_hourly):
            payg = pd.NA
        else:
            payg = float(hours) * float(gcp_hourly)
        cud_1 = (float(payg) * GCP_CUD_1YR) if pd.notna(payg) else pd.NA
        cud_3 = (float(payg) * GCP_CUD_3YR) if pd.notna(payg) else pd.NA
        return (gcp_machine, gcp_cores, gcp_ram, quantity, gcp_hourly, payg, cud_1, cud_3)

    df[["GCP equi Inst", "GCP Cores", "GCP RAM", "Quantity", "GCP Per Hour", "PAYG", "1 Yr CUD", "3 Yr CUD"]] = df.apply(_calc, axis=1, result_type="expand")
    df["PAYG"] = pd.to_numeric(df["PAYG"], errors="coerce")
    df["GCP Per Hour"] = pd.to_numeric(df["GCP Per Hour"], errors="coerce")
    df["1 Yr CUD"] = pd.to_numeric(df["1 Yr CUD"], errors="coerce")
    df["3 Yr CUD"] = pd.to_numeric(df["3 Yr CUD"], errors="coerce")
    if "AWS Per Hour" in df.columns:
        aws_cols = []
        gcp_cols = ["GCP equi Inst", "GCP Cores", "GCP RAM", "Quantity", "GCP Per Hour", "PAYG", "1 Yr CUD", "3 Yr CUD"]
        for col in df.columns:
            if col in gcp_cols:
                continue
            aws_cols.append(col)
        if "AWS Per Hour" in aws_cols:
            pass
        df.insert(len(aws_cols), " ", "")
        df.insert(len(aws_cols) + 1, "  ", "")
        df = df[aws_cols + [" ", "  "] + gcp_cols]
    if {"Region", "OS", "AWS Instance", "PAYG"} <= set(df.columns):
        data_rows = df[(df["AWS Instance"] != "Total") & (df["Region"] != "Total")]
        for (region, os_name), group in data_rows.groupby(["Region", "OS"], dropna=False):
            mask = (df["Region"] == region) & (df["OS"] == os_name) & (df["AWS Instance"] == "Total")
            if mask.any():
                df.loc[mask, "PAYG"] = group["PAYG"].sum(skipna=True)
                df.loc[mask, "GCP Per Hour"] = pd.NA
                df.loc[mask, "1 Yr CUD"] = group["1 Yr CUD"].sum(skipna=True)
                df.loc[mask, "3 Yr CUD"] = group["3 Yr CUD"].sum(skipna=True)
        total_mask = df["Region"] == "Total"
        if total_mask.any():
            df.loc[total_mask, "PAYG"] = data_rows["PAYG"].sum(skipna=True)
            df.loc[total_mask, "GCP Per Hour"] = pd.NA
            df.loc[total_mask, "1 Yr CUD"] = data_rows["1 Yr CUD"].sum(skipna=True)
            df.loc[total_mask, "3 Yr CUD"] = data_rows["3 Yr CUD"].sum(skipna=True)
    return df

# ----------------------------- GCP helpers (lazy) -----------------------------
# These functions are only used when --gcp-use-api is provided. They try to be tolerant and
# best-effort when the catalog SKUs are not obvious.

def _try_import_gcp():
    try:
        from googleapiclient import discovery  # type: ignore
        import google.auth  # type: ignore
        return discovery, google
    except Exception:
        return None, None


AWS_TO_GCP_REGION = {
    "us-east-1": "us-east1",
    "us-east-2": "us-east4",
    "us-west-1": "us-west1",
    "us-west-2": "us-west2",
    "ca-central-1": "northamerica-northeast1",
    "eu-west-1": "europe-west1",
    "eu-west-2": "europe-west2",
    "eu-west-3": "europe-west9",
    "eu-central-1": "europe-west3",
    "eu-central-2": "europe-west10",
    "eu-north-1": "europe-north1",
    "ap-south-1": "asia-south1",
    "ap-south-2": "asia-south2",
    "ap-southeast-1": "asia-southeast1",
    "ap-southeast-2": "australia-southeast1",
    "ap-southeast-3": "asia-southeast2",
    "ap-southeast-4": "australia-southeast2",
    "ap-northeast-1": "asia-northeast1",
    "ap-northeast-2": "asia-northeast3",
    "ap-northeast-3": "asia-northeast2",
    "ap-east-1": "asia-east2",
    "sa-east-1": "southamerica-east1",
    "me-central-1": "me-central1",
    "me-south-1": "me-west1",
    "af-south-1": "africa-south1",
    "il-central-1": "me-west1",
}

COMPUTE_BILLING_SERVICE = "services/6F81-5844-456A"
CLOUDSQL_BILLING_SERVICE = "services/9662-B51E-5089"

GCP_CUD_1YR = 0.72
GCP_CUD_3YR = 0.54

CLOUDSQL_TIER_SPECS = [
    ("db-f1-micro", 1, 0.6),
    ("db-g1-small", 1, 1.7),
    ("db-standard-1", 1, 3.75),
    ("db-standard-2", 2, 7.5),
    ("db-standard-4", 4, 15.0),
    ("db-standard-8", 8, 30.0),
    ("db-standard-16", 16, 60.0),
    ("db-standard-32", 32, 120.0),
    ("db-standard-64", 64, 240.0),
]
CLOUDSQL_TIERS = [t[0] for t in CLOUDSQL_TIER_SPECS]


def map_aws_region_to_gcp(aws_region: str | None, fallback: str | None = None) -> str | None:
    if not aws_region:
        return fallback
    return AWS_TO_GCP_REGION.get(aws_region, fallback)


def fetch_cloud_sql_tier_rates(gcp_region: str) -> dict[str, float]:
    discovery, google = _try_import_gcp()
    if discovery is None:
        return {}
    try:
        creds, _ = google.auth.default(scopes=["https://www.googleapis.com/auth/cloud-platform"])
        billing = discovery.build("cloudbilling", "v1", credentials=creds, cache_discovery=False)
        service_id = None
        req = billing.services().list()
        while req is not None:
            resp = req.execute()
            for svc in resp.get("services", []):
                if svc.get("displayName") == "Cloud SQL":
                    service_id = svc.get("name")
                    break
            if service_id:
                break
            req = billing.services().list_next(previous_request=req, previous_response=resp)
        if not service_id:
            service_id = CLOUDSQL_BILLING_SERVICE
        rates: dict[str, float] = {}
        remaining = set(CLOUDSQL_TIERS)
        def scan(include_region: bool) -> dict[str, float]:
            rates_local: dict[str, float] = {}
            remaining_local = set(CLOUDSQL_TIERS)
            req = billing.services().skus().list(parent=service_id)
            while req is not None:
                resp = req.execute()
                for sku in resp.get("skus", []):
                    desc = str(sku.get("description") or "")
                    if "db-" not in desc:
                        continue
                    if include_region:
                        service_regions = sku.get("serviceRegions", [])
                        if service_regions and gcp_region not in service_regions:
                            continue
                    tier_match = re.search(r"(db-[a-z0-9-]+)", desc)
                    if not tier_match:
                        continue
                    pricing_info = sku.get("pricingInfo", [])
                    if not pricing_info:
                        continue
                    expr = pricing_info[0].get("pricingExpression", {})
                    tiered = expr.get("tieredRates", [])
                    if not tiered:
                        continue
                    unit_price = tiered[0].get("unitPrice", {})
                    units = float(unit_price.get("units", 0))
                    nanos = float(unit_price.get("nanos", 0)) / 1e9
                    rate = units + nanos
                    tier = tier_match.group(1)
                    if rate > 0:
                        rates_local[tier] = min(rate, rates_local.get(tier, rate))
                        if tier in remaining_local:
                            remaining_local.remove(tier)
                            if not remaining_local:
                                return rates_local
                req = billing.services().skus().list_next(previous_request=req, previous_response=resp)
            return rates_local

        rates = scan(include_region=True)
        if not rates:
            rates = scan(include_region=False)
        return rates
    except Exception:
        return {}


def choose_cloudsql_tier(cores: float | None, ram_gib: float | None) -> str | None:
    if cores is None or ram_gib is None:
        return None
    for name, c, m in CLOUDSQL_TIER_SPECS:
        if cores == c and abs(ram_gib - m) < 1e-6:
            return name
    return None


def format_cloudsql_custom(cores: float, ram_gib: float) -> str:
    mem_mb = int(round(ram_gib * 1024))
    return f"db-custom-{int(cores)}-{mem_mb}"


def adjust_gcp_custom_cores(cores: float, ram_gib: float, max_mem_per_core: float = 6.5) -> float:
    if cores <= 0:
        return cores
    required = ram_gib / max_mem_per_core
    if required <= cores:
        return cores
    return float(int(required) if required.is_integer() else int(required) + 1)


def cloudsql_rates_from_compute(cpu_rate: float | None, mem_rate: float | None) -> dict[str, float]:
    if cpu_rate is None or mem_rate is None:
        return {}
    rates = {}
    for name, cores, mem in CLOUDSQL_TIER_SPECS:
        rates[name] = (cpu_rate * cores) + (mem_rate * mem)
    return rates


def list_gcp_machine_types(project: str, zones: list[str]) -> Dict[str, Dict[str, Dict[str, int]]]:
    """
    Return a mapping zone -> {machineName: {"guestCpus": int, "memoryMb": int}}
    """
    discovery, google = _try_import_gcp()
    if discovery is None:
        return {}
    if not zones:
        return {}
    creds, _ = google.auth.default(scopes=["https://www.googleapis.com/auth/cloud-platform"])
    compute = discovery.build("compute", "v1", credentials=creds, cache_discovery=False)
    out = {}
    for zone in zones:
        try:
            items = []
            req = compute.machineTypes().list(project=project, zone=zone)
            while req is not None:
                resp = req.execute(num_retries=3)
                items.extend(resp.get("items", []))
                req = compute.machineTypes().list_next(previous_request=req, previous_response=resp)
            out[zone] = {m["name"]: {"guestCpus": m.get("guestCpus"), "memoryMb": m.get("memoryMb")} for m in items}
        except Exception:
            out[zone] = {}
    return out


def list_gcp_zones(project: str) -> list[str]:
    discovery, google = _try_import_gcp()
    if discovery is None:
        return []
    creds, _ = google.auth.default(scopes=["https://www.googleapis.com/auth/cloud-platform"])
    compute = discovery.build("compute", "v1", credentials=creds, cache_discovery=False)
    zones = []
    req = compute.zones().list(project=project)
    while req is not None:
        resp = req.execute()
        for z in resp.get("items", []):
            name = z.get("name")
            if name:
                zones.append(name)
        req = compute.zones().list_next(previous_request=req, previous_response=resp)
    return zones


def pick_zones_by_regions(zones: list[str], regions: list[str]) -> list[str]:
    picked = []
    remaining = set(regions)
    for zone in zones:
        for region in list(remaining):
            if zone.startswith(f"{region}-"):
                picked.append(zone)
                remaining.remove(region)
                break
        if not remaining:
            break
    return picked


def find_exact_gcp_machine(
    zones_map: Dict[str, Dict[str, Dict[str, int]]],
    vcpus: int | None,
    mem_gib: float | None,
    family_preference: list[str] | None = None,
):
    if vcpus is None or mem_gib is None:
        return None
    mem_mb = int(mem_gib * 1024)
    matches = []
    for zone, machines in zones_map.items():
        for name, attrs in machines.items():
            if attrs.get("guestCpus") == vcpus and attrs.get("memoryMb") == mem_mb:
                matches.append((zone, name))
    if not matches:
        return None
    if not family_preference:
        return matches[0]
    pref_index = {fam: i for i, fam in enumerate(family_preference)}
    def _score(item):
        _, name = item
        family = name.split("-", 1)[0]
        return pref_index.get(family, len(pref_index))
    matches.sort(key=_score)
    return matches[0]
    return None


def is_amd_aws_instance(instance: str) -> bool:
    family = instance.split(".", 1)[0]
    if not family:
        return False
    return family.endswith("a")


def is_small_general_purpose(instance: str) -> bool:
    family = instance.split(".", 1)[0].lower()
    return family.startswith("t2") or family.startswith("t3")


def build_gcp_family_preference(aws_instance: str) -> list[str]:
    instance = (aws_instance or "").strip().lower()
    if not instance:
        return ["e2", "n2d", "n2", "c2d", "c2"]
    if is_amd_aws_instance(instance):
        return ["e2", "n2d", "n2", "c2d", "c2"]
    if is_small_general_purpose(instance):
        return ["e2", "n2d", "n2", "c2d", "c2"]
    return ["e2", "n2d", "n2", "c2d", "c2"]


def load_instance_mapping(csv_path: str) -> dict[str, dict[str, float | str | None]]:
    if not csv_path or not os.path.exists(csv_path):
        return {}
    mapping: dict[str, dict[str, float | str | None]] = {}
    try:
        with open(csv_path, newline="", encoding="utf-8") as handle:
            reader = csv.reader(handle)
            for row in reader:
                for aws_start, gcp_start in ((0, 4), (9, 13)):
                    if len(row) <= max(aws_start + 2, gcp_start + 2):
                        continue
                    aws_inst = (row[aws_start] or "").strip()
                    gcp_inst = (row[gcp_start] or "").strip()
                    if not aws_inst or not gcp_inst:
                        continue
                    if "instance size" in aws_inst.lower():
                        continue
                    if "." not in aws_inst:
                        continue
                    gcp_vcpu_raw = (row[gcp_start + 1] or "").strip()
                    gcp_mem_raw = (row[gcp_start + 2] or "").strip()
                    try:
                        gcp_vcpu = float(gcp_vcpu_raw) if gcp_vcpu_raw else None
                    except Exception:
                        gcp_vcpu = None
                    try:
                        gcp_mem = float(gcp_mem_raw) if gcp_mem_raw else None
                    except Exception:
                        gcp_mem = None
                    mapping[aws_inst.lower()] = {
                        "gcp_inst": gcp_inst,
                        "gcp_vcpu": gcp_vcpu,
                        "gcp_mem": gcp_mem,
                    }
    except Exception:
        return {}
    return mapping


def _sku_hourly_price(sku_obj: dict) -> float | None:
    # Try simple extraction of USD price from pricingInfo
    for pi in sku_obj.get("pricingInfo", []) or []:
        pe = pi.get("pricingExpression", {})
        unit_map = pe.get("pricePerUnit", {})
        usd = unit_map.get("USD")
        if usd is not None:
            try:
                return float(usd)
            except Exception:
                continue
        units = unit_map.get("units")
        nanos = unit_map.get("nanos")
        if units is not None or nanos is not None:
            try:
                return float(units or 0) + float(nanos or 0) / 1e9
            except Exception:
                pass
        tiered = pe.get("tieredRates", []) or []
        if tiered:
            unit_price = tiered[0].get("unitPrice", {})
            usd = unit_price.get("USD")
            if usd is not None:
                try:
                    return float(usd)
                except Exception:
                    continue
            units = unit_price.get("units")
            nanos = unit_price.get("nanos")
            if units is not None or nanos is not None:
                try:
                    return float(units or 0) + float(nanos or 0) / 1e9
                except Exception:
                    pass
    return None


def _usage_unit(sku_obj: dict) -> str:
    for pi in sku_obj.get("pricingInfo", []) or []:
        pe = pi.get("pricingExpression", {})
        unit = pe.get("usageUnit") or ""
        if unit:
            return str(unit)
    return ""


def _normalize_rate_per_hour(rate: float, usage_unit: str) -> float:
    unit = usage_unit.lower()
    if "month" in unit or "mo" in unit:
        return rate / 730.0
    return rate


def _normalize_mem_rate(rate: float, usage_unit: str) -> float:
    unit = usage_unit.lower()
    if "giby" in unit or "gby" in unit:
        return _normalize_rate_per_hour(rate, unit)
    if "miby" in unit or "mby" in unit:
        return _normalize_rate_per_hour(rate * 1024.0, unit)
    if "by" in unit:
        return _normalize_rate_per_hour(rate * (1024.0 ** 3), unit)
    return _normalize_rate_per_hour(rate, unit)


def _normalize_cpu_rate(rate: float, usage_unit: str) -> float:
    return _normalize_rate_per_hour(rate, usage_unit)


def fetch_gcp_cpu_memory_skus(region: str) -> dict:
    discovery, google = _try_import_gcp()
    if discovery is None:
        return {}
    try:
        creds, _ = google.auth.default(scopes=["https://www.googleapis.com/auth/cloud-platform"])
        billing = discovery.build("cloudbilling", "v1", credentials=creds, cache_discovery=False)
        # find compute service id
        service_id = None
        req = billing.services().list()
        while req is not None:
            resp = req.execute()
            for s in resp.get("services", []):
                if "Compute Engine" in s.get("displayName", ""):
                    service_id = s.get("name")
                    break
            if service_id:
                break
            req = billing.services().list_next(previous_request=req, previous_response=resp)
        if not service_id:
            service_id = COMPUTE_BILLING_SERVICE
        def scan(include_region: bool) -> tuple[float | None, float | None, dict[str, dict[str, float]], dict[str, float]]:
            cpu_rate = None
            mem_rate = None
            family_rates: dict[str, dict[str, float]] = {
                "e2": {},
                "n2": {},
                "n2d": {},
                "c2": {},
                "c2d": {},
            }
            predefined_prices: dict[str, float] = {}
            req = billing.services().skus().list(parent=service_id, pageSize=500)
            while req is not None:
                resp = req.execute()
                for sku in resp.get("skus", []):
                    if include_region:
                        regions = sku.get("serviceRegions", []) or []
                        if regions and region not in regions:
                            continue
                    desc = sku.get("description", "").lower()
                    cat = sku.get("category", {})
                    family = str(cat.get("resourceFamily") or "").lower()
                    group = str(cat.get("resourceGroup") or "").lower()
                    usage_type = str(cat.get("usageType") or "").lower()
                    if family != "compute":
                        continue
                    if "sole tenant" in desc or "commitment" in desc:
                        continue
                    price = _sku_hourly_price(sku)
                    if price is None:
                        continue
                    unit = _usage_unit(sku)
                    if usage_type == "ondemand" and "instance running" in desc and "custom" not in desc:
                        match = PREDEFINED_INST_RE.search(desc)
                        if match:
                            inst = match.group(0).lower()
                            rate = _normalize_rate_per_hour(price, unit)
                            if rate > 0:
                                predefined_prices[inst] = min(rate, predefined_prices.get(inst, rate))
                    for fam in family_rates.keys():
                        if "custom" in desc:
                            continue
                        if usage_type == "ondemand" and f"{fam} instance core" in desc:
                            family_rates[fam]["cpu"] = _normalize_cpu_rate(price, unit)
                        if usage_type == "ondemand" and (f"{fam} instance ram" in desc or f"{fam} instance memory" in desc):
                            family_rates[fam]["mem"] = _normalize_mem_rate(price, unit)
                    if cpu_rate is None and usage_type == "ondemand" and ("custom instance core" in desc) and (group in {"cpu"} or "vcpu" in desc or "cpu" in desc):
                        cpu_rate = _normalize_cpu_rate(price, unit)
                    if mem_rate is None and usage_type == "ondemand" and ("custom instance ram" in desc or "custom instance memory" in desc) and (group in {"ram", "memory"} or "memory" in desc or "ram" in desc):
                        mem_rate = _normalize_mem_rate(price, unit)
                    if cpu_rate is None and usage_type == "ondemand" and (group in {"cpu"} or "vcpu" in desc or "cpu" in desc):
                        cpu_rate = _normalize_cpu_rate(price, unit)
                    if mem_rate is None and usage_type == "ondemand" and (group in {"ram", "memory"} or "memory" in desc or "ram" in desc):
                        mem_rate = _normalize_mem_rate(price, unit)
                req = billing.services().skus().list_next(previous_request=req, previous_response=resp)
            return cpu_rate, mem_rate, family_rates, predefined_prices

        cpu_rate, mem_rate, family_rates, predefined_prices = scan(include_region=True)
        if cpu_rate is None or mem_rate is None:
            cpu_rate, mem_rate, family_rates, predefined_prices = scan(include_region=False)
        out = {}
        if cpu_rate is not None:
            out["cpu_per_core_hour"] = cpu_rate
        if mem_rate is not None:
            out["mem_per_gib_hour"] = mem_rate
        if family_rates:
            out["family_rates"] = family_rates
        if predefined_prices:
            out["predefined_prices"] = predefined_prices
        return out
    except Exception:
        return {}

# ----------------------------- main flow (extended) -----------------------------

def load_gcp_pricing_cache(region: str) -> dict | None:
    cache_dir = Path(__file__).resolve().parent / ".gcp_pricing_cache"
    cache_path = cache_dir / f"{region}.json"
    if not cache_path.exists():
        return None
    try:
        data = json.loads(cache_path.read_text(encoding="utf-8"))
    except Exception:
        return None
    if isinstance(data, dict) and "predefined" in data and "custom" in data:
        custom = data.get("custom") if isinstance(data.get("custom"), dict) else {}
        family_rates = data.get("family_rates") if isinstance(data.get("family_rates"), dict) else {}
        return {
            "predefined_prices": data.get("predefined", {}),
            "cpu_per_core_hour": custom.get("cpu_per_core_hour"),
            "mem_per_gib_hour": custom.get("mem_per_gib_hour"),
            "family_rates": family_rates,
        }
    if isinstance(data, dict) and "predefined_prices" in data:
        return data
    return None


def save_gcp_pricing_cache(region: str, data: dict) -> None:
    cache_dir = Path(__file__).resolve().parent / ".gcp_pricing_cache"
    cache_dir.mkdir(parents=True, exist_ok=True)
    cache_path = cache_dir / f"{region}.json"
    payload = {
        "region": region,
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "predefined": data.get("predefined_prices", {}),
        "family_rates": data.get("family_rates", {}),
        "custom": {
            "cpu_per_core_hour": data.get("cpu_per_core_hour"),
            "mem_per_gib_hour": data.get("mem_per_gib_hour"),
        },
    }
    cache_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")

def prompt_family_preference() -> list[str] | None:
    env_choice = os.environ.get("GCP_FAMILY_PREF", "").strip().lower()
    if env_choice:
        if env_choice in {"c2", "c2d"}:
            return ["c2", "c2d"]
        if env_choice in {"n2", "n2d"}:
            return ["n2", "n2d"]
        if env_choice in {"e2", "e2d", "auto"}:
            return ["e2", "n2", "c2"] if env_choice != "auto" else None
    return None


def main() -> None:
    # Load .env so boto3 and GCP libs can pick up credentials/regions from the file
    load_env_from_file()

    def log(msg: str) -> None:
        print(f"[{time.strftime('%H:%M:%S')}] {msg}")

    def auto_widths(df: pd.DataFrame) -> list[float]:
        widths = []
        for col in df.columns:
            max_len = len(str(col))
            for val in df[col]:
                max_len = max(max_len, len(str(val)))
            widths.append(min(max_len + 2, 50))
        return widths

    def format_table(worksheet, startrow: int, df: pd.DataFrame, border_fmt, header_fmt):
        nrows = len(df)
        ncols = len(df.columns)
        end_row = startrow + nrows
        end_col = ncols - 1
        worksheet.conditional_format(startrow, 0, end_row, end_col, {"type": "no_blanks", "format": border_fmt})
        worksheet.conditional_format(startrow, 0, end_row, end_col, {"type": "blanks", "format": border_fmt})
        for col_idx, col_name in enumerate(df.columns):
            worksheet.write(startrow, col_idx, col_name, header_fmt)

    def apply_cud_formulas(worksheet, startrow: int, df: pd.DataFrame, cfg_sheet: str = "CUD Config"):
        if "PAYG" not in df.columns or "1 Yr CUD" not in df.columns or "3 Yr CUD" not in df.columns:
            return
        payg_idx = df.columns.get_loc("PAYG")
        cud1_idx = df.columns.get_loc("1 Yr CUD")
        cud3_idx = df.columns.get_loc("3 Yr CUD")
        for r in range(startrow + 1, startrow + 1 + len(df)):
            payg_cell = f"{xl_col_to_name(payg_idx)}{r+1}"
            worksheet.write_formula(
                r, cud1_idx, f"=IF({payg_cell}=\"\",\"\",{payg_cell}*'{cfg_sheet}'!$C$2)"
            )
            worksheet.write_formula(
                r, cud3_idx, f"=IF({payg_cell}=\"\",\"\",{payg_cell}*'{cfg_sheet}'!$C$3)"
            )

    parser = argparse.ArgumentParser(description="Summarize AWS costs per service and map to GCP if requested.")
    parser.add_argument("--input", default="bill.csv", help="Path to the AWS billing CSV (default: bill_csv.csv).")
    parser.add_argument("--output-dir", default="output", help="Directory to write summaries (default: output).")
    parser.add_argument("--coverage-pct", type=float, default=None, help="If set (0-100), also build a coverage scenario assuming this percent of on-demand is covered by flex CUD.")
    parser.add_argument("--flex-rate", type=float, default=None, help="Effective flex CUD charge as a fraction of covered on-demand (e.g., 0.54). Required if --coverage-pct is set.")
    parser.add_argument("--resource-commit", type=float, default=0.0, help="Additional fixed commitment charge to include in the coverage scenario.")
    parser.add_argument("--no-commitments-sheet", action="store_true", help="Skip detecting Savings Plan / RI lines and writing the Commitments sheet.")
    parser.add_argument("--default-region", default=None, help="Fallback region name for rows that lack a region code in UsageType.")

    # GCP flags
    parser.add_argument("--gcp-use-api", action="store_true", help="Use GCP APIs (google-auth & google-api-python-client). If not set the script will only output AWS-side files.")
    parser.add_argument("--gcp-project", default=os.environ.get("GCP_PROJECT_ID"), help="GCP project used for Compute API listing (required if using GCP API).")
    parser.add_argument("--gcp-zones", default=os.environ.get("GCP_ZONES"), help="Comma-separated list of zones to check for exact machine-type matches (e.g. eu-central1-a,eu-central1-b).")
    parser.add_argument("--gcp-region", default=os.environ.get("GCP_REGION"), help="GCP region name for pricing (e.g. europe-west3). Used when calling Cloud Billing Catalog.")
    parser.add_argument("--refresh-cache", action="store_true", help="Force refresh of instance specs cache.")
    parser.add_argument("--dry-run", action="store_true", help="Dry-run: skip API calls when possible (useful for testing).")

    args = parser.parse_args()

    # Basic validation for GCP flags when requested
    gcp_zones_list = [z.strip() for z in (args.gcp_zones or "").split(",") if z.strip()]
    gcp_use_api = args.gcp_use_api or bool(args.gcp_project)
    if gcp_use_api:
        missing = []
        if not args.gcp_project:
            missing.append("--gcp-project")
        if missing:
            raise SystemExit(f"--gcp-use-api requires the following flags: {', '.join(missing)}")
        # quick import check
        discovery, google = _try_import_gcp()
        if discovery is None:
            raise SystemExit("--gcp-use-api requested but google-api-python-client or google-auth not installed. Install with: pip install google-api-python-client google-auth")
        # quick ADC check
        try:
            creds, _ = google.auth.default(scopes=["https://www.googleapis.com/auth/cloud-platform"])
        except Exception as e:
            raise SystemExit("Failed to acquire GCP credentials via ADC. Run `gcloud auth application-default login` or set GOOGLE_APPLICATION_CREDENTIALS to a service account JSON.")

    input_path = Path(args.input)
    if not input_path.exists():
        raise SystemExit(f"Input file not found: {input_path}")

    out_dir = Path(args.output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    df = pd.read_csv(input_path)
    summary = build_summary(df)

    scenario_df = None
    commitments_df = None

    # ------------------ fast, cache-backed spec discovery ------------------
    def extract_instance_types(frame: pd.DataFrame) -> set[str]:
        types = set()
        if "UsageType" in frame.columns:
            usage = frame["UsageType"].astype(str).dropna()
            box_mask = usage.str.contains("BoxUsage", case=False, na=False)
            spot_mask = usage.str.contains("SpotUsage", case=False, na=False)
            rds_mask = (
                usage.str.contains("InstanceUsage", case=False, na=False)
                | usage.str.contains("Multi-AZUsage", case=False, na=False)
                | usage.str.contains("InstanceUsageIOOptimized", case=False, na=False)
                | usage.str.contains("HeavyUsage", case=False, na=False)
            )
            box_tokens = usage[box_mask].str.extract(r"BoxUsage:([^,]+)")[0]
            spot_tokens = usage[spot_mask].str.extract(r"SpotUsage[:/ -]([^,]+)")[0]
            rds_tokens = usage[rds_mask].str.extract(r"(?:InstanceUsageIOOptimized|InstanceUsage|Multi-AZUsage|HeavyUsage):([^,]+)")[0]
            tokens = pd.concat([box_tokens, spot_tokens, rds_tokens], ignore_index=True).dropna().str.strip().str.lower()
            tokens = tokens.apply(lambda t: normalize_rds_instance_class(t)[1] if t.startswith("db.") else _expand_xlarge_suffix(t))
            types.update(tokens)
        return {t for t in types if isinstance(t, str) and t}

    observed_types = extract_instance_types(df)

    spec_map = dict(BASE_INSTANCE_SPECS)
    SCRIPT_DIR = Path(__file__).resolve().parent
    cache_path = SCRIPT_DIR / ".instance_specs.json"
    cached: Dict[str, Tuple[float | None, float | None]] = {}
    if cache_path.exists() and not args.refresh_cache:
        try:
            raw = json.loads(cache_path.read_text(encoding="utf-8"))
            for k, v in raw.items():
                if isinstance(v, list) and len(v) >= 2:
                    cached[k.lower()] = (None if v[0] is None else float(v[0]), None if v[1] is None else float(v[1]))
            print(f"Loaded {len(cached)} cached instance specs from {cache_path}")
        except Exception as e:
            print(f"Warning: failed to read instance spec cache {cache_path}: {e}")

    missing = sorted([it for it in observed_types if it not in cached])
    fetched: Dict[str, Tuple[float | None, float | None]] = {}
    if missing and boto3 is not None and not args.dry_run:
        print(f"Fetching specs for {len(missing)} instance types (EC2/RDS/Spot)...")
        fetched = fetch_instance_specs(set(missing))
    elif missing:
        print("boto3 not available or dry-run set - skipping instance spec fetch; memory will stay blank for missing types.")

    merged = dict(cached)
    if fetched:
        merged.update({k.lower(): (None if v is None or v[0] is None else float(v[0]), None if v is None or v[1] is None else float(v[1])) for k, v in fetched.items()})
    for k, v in BASE_INSTANCE_SPECS.items():
        merged.setdefault(k.lower(), v)
    spec_map.update(merged)

    try:
        out_cache = {k: [None if v[0] is None else v[0], None if v[1] is None else v[1]] for k, v in merged.items()}
        cache_path.write_text(json.dumps(out_cache, indent=2), encoding="utf-8")
        print(f"Wrote {len(out_cache)} instance specs to cache {cache_path}")
    except Exception as e:
        print(f"Warning: failed to write instance spec cache {cache_path}: {e}")

    # -----------------------------------------------------------------------

    # Build compute / reserved tables using spec_map
    overall_compute_df, region_tables = build_compute_by_region(df, default_region=args.default_region, spec_map=spec_map)
    overall_ri_df, ri_tables = build_reserved_by_region(df, default_region=args.default_region, spec_map=spec_map)
    overall_spot_df, spot_tables = build_spot_by_region(df, default_region=args.default_region, spec_map=spec_map)
    service_region_tables, nat_region_tables = build_service_usage_by_region(df, default_region=args.default_region)

    gcp_region_map: dict[str, str] = {}
    gcp_cpu_mem_by_region: dict[str, dict[str, float]] = {}
    gcp_sql_rates_by_region: dict[str, dict[str, float]] = {}
    zones_map: dict[str, dict[str, dict[str, int]]] = {}
    gcp_family_pref: list[str] | None = None
    if gcp_use_api and not args.dry_run:
        gcp_family_pref = prompt_family_preference()
        aws_region_codes = (
            df["UsageType"].astype(str).str.extract(r"^([A-Z0-9]+)-")[0].map(REGION_MAP).dropna().unique().tolist()
        )
        log(f"GCP: building region map for {len(aws_region_codes)} AWS regions")
        for aws_region in aws_region_codes:
            gcp_region = map_aws_region_to_gcp(aws_region, args.gcp_region)
            if gcp_region:
                gcp_region_map[aws_region] = gcp_region
        gcp_regions = sorted({r for r in gcp_region_map.values() if r})
        if not gcp_zones_list and args.gcp_project:
            log("GCP: listing zones for project")
        zones_map = {}
        regions_to_fetch = []
        for region in gcp_regions:
            if not args.refresh_cache:
                cached = load_gcp_pricing_cache(region)
                if cached:
                    gcp_cpu_mem_by_region[region] = cached
                    log(f"GCP: pricing cache hit for {region}")
                    continue
            regions_to_fetch.append(region)
            log(f"GCP: pricing cache miss for {region}")
        with concurrent.futures.ThreadPoolExecutor(max_workers=min(8, max(2, len(gcp_regions) * 2))) as executor:
            futures = {}
            if args.gcp_project:
                futures["zones"] = executor.submit(list_gcp_zones, args.gcp_project)
            for region in regions_to_fetch:
                futures[f"cpu:{region}"] = executor.submit(fetch_gcp_cpu_memory_skus, region)
                futures[f"sql:{region}"] = executor.submit(fetch_cloud_sql_tier_rates, region)
            for key, fut in futures.items():
                try:
                    result = fut.result()
                except Exception:
                    result = None
                if key == "zones":
                    all_zones = result or []
                    gcp_zones_list = pick_zones_by_regions(all_zones, gcp_regions)
                elif key.startswith("cpu:"):
                    region = key.split(":", 1)[1]
                    data = result or {}
                    gcp_cpu_mem_by_region[region] = data
                    if data:
                        save_gcp_pricing_cache(region, data)
                elif key.startswith("sql:"):
                    region = key.split(":", 1)[1]
                    gcp_sql_rates_by_region[region] = result or {}

        log("GCP: building zones map")
        zones_map = list_gcp_machine_types(args.gcp_project, gcp_zones_list)

        for region in gcp_regions:
            if not gcp_sql_rates_by_region.get(region):
                cpu_mem = gcp_cpu_mem_by_region.get(region, {})
                gcp_sql_rates_by_region[region] = cloudsql_rates_from_compute(
                    cpu_mem.get("cpu_per_core_hour"), cpu_mem.get("mem_per_gib_hour")
                )

        # Always fetch fresh rates; no cache write.

    mapping_path = os.path.join(os.getcwd(), "Service Comparison - Sheet2.csv")
    instance_map = load_instance_mapping(mapping_path)
    if instance_map:
        log(f"Loaded {len(instance_map)} instance mappings from {mapping_path}")
    else:
        log("No instance mapping file found or empty; using dynamic matching")

    rds_region_tables = build_rds_usage_by_region(
        df,
        default_region=args.default_region,
        spec_map=spec_map,
        gcp_region_map=gcp_region_map if gcp_region_map else None,
        gcp_sql_rates=gcp_sql_rates_by_region if gcp_sql_rates_by_region else None,
        gcp_cpu_mem_rates=gcp_cpu_mem_by_region if gcp_cpu_mem_by_region else None,
    )

    # populate AWS on-demand rates for display
    pricing_rates: Dict[Tuple[str, str, str], float] = {}
    if region_tables or ri_tables or spot_tables:
        instances_per_region_os: Dict[Tuple[str, str], set[str]] = {}
        for (region_name, os_name), region_df in (region_tables or {}).items():
            for _, row in region_df.iterrows():
                if row.get("AWS Instance") == "Total":
                    continue
                os_key = "Windows" if "windows" in str(os_name).lower() else "Linux"
                instances_per_region_os.setdefault((region_name, os_key), set()).add(row.get("AWS Instance"))
        for (region_name, os_name), region_df in (ri_tables or {}).items():
            for _, row in region_df.iterrows():
                if row.get("AWS Instance") == "Total":
                    continue
                os_key = "Windows" if "windows" in str(os_name).lower() else "Linux"
                instances_per_region_os.setdefault((region_name, os_key), set()).add(row.get("AWS Instance"))
        for (region_name, os_name), region_df in (spot_tables or {}).items():
            for _, row in region_df.iterrows():
                if row.get("AWS Instance") == "Total":
                    continue
                os_key = "Windows" if "windows" in str(os_name).lower() else "Linux"
                instances_per_region_os.setdefault((region_name, os_key), set()).add(row.get("AWS Instance"))

        if not args.dry_run:
            pricing_rates = fetch_on_demand_rates(instances_per_region_os)
        if overall_compute_df is not None:
            overall_compute_df = apply_on_demand_rates(overall_compute_df, pricing_rates)
        for region in list(region_tables):
            region_tables[region] = apply_on_demand_rates(region_tables[region], pricing_rates)
        if overall_spot_df is not None:
            overall_spot_df = apply_on_demand_rates(overall_spot_df, pricing_rates)
        for region in list(spot_tables or {}):
            spot_tables[region] = apply_on_demand_rates(spot_tables[region], pricing_rates)
        if gcp_region_map and gcp_cpu_mem_by_region and zones_map:
            if overall_compute_df is not None:
                overall_compute_df = add_gcp_compute_mapping(
                    overall_compute_df,
                    gcp_region_map,
                    gcp_cpu_mem_by_region,
                    zones_map,
                    gcp_family_pref,
                    instance_map,
                )
            for region in list(region_tables):
                region_tables[region] = add_gcp_compute_mapping(
                    region_tables[region],
                    gcp_region_map,
                    gcp_cpu_mem_by_region,
                    zones_map,
                    gcp_family_pref,
                    instance_map,
                )
            if overall_spot_df is not None:
                overall_spot_df = add_gcp_compute_mapping(
                    overall_spot_df,
                    gcp_region_map,
                    gcp_cpu_mem_by_region,
                    zones_map,
                    gcp_family_pref,
                    instance_map,
                )
            for region in list(spot_tables or {}):
                spot_tables[region] = add_gcp_compute_mapping(
                    spot_tables[region],
                    gcp_region_map,
                    gcp_cpu_mem_by_region,
                    zones_map,
                    gcp_family_pref,
                    instance_map,
                )

    if args.coverage_pct is not None:
        if args.flex_rate is None:
            raise SystemExit("--flex-rate is required when --coverage-pct is set.")
        if not 0 <= args.coverage_pct <= 100:
            raise SystemExit("--coverage-pct must be between 0 and 100.")
        if args.flex_rate < 0:
            raise SystemExit("--flex-rate must be non-negative.")
        total_on_demand = summary.loc[summary["Service"] == "Total", "Usage Cost"].iloc[0]
        scenario_df = build_coverage_scenario(total_on_demand, args.coverage_pct, args.flex_rate, args.resource_commit)

    if not args.no_commitments_sheet:
        commitments_df = detect_commitments(df)

    # ----------------------------- AWS -> GCP mapping (if requested) -----------------------------
    gcp_mapping_rows = []
    if gcp_use_api and not args.dry_run:
        discovery, google = _try_import_gcp()
        # list machine types across zones
        zones_map = zones_map or list_gcp_machine_types(args.gcp_project, gcp_zones_list)

        for (region_name, os_name), region_df in region_tables.items():
            gcp_region = gcp_region_map.get(region_name)
            gcp_cpu_mem = gcp_cpu_mem_by_region.get(gcp_region or "", {})
            for _, row in region_df.iterrows():
                if row.get("AWS Instance") == "Total":
                    continue
                vcpus = row.get("AWS Cores")
                mem = row.get("AWS Memory")
                hours = float(row.get("No Of Hours") or 0)
                aws_cost = float(row.get("AWS Cost") or 0)

                gcp_zone = None
                gcp_machine = None
                gcp_hourly = None
                def _compute_custom_hourly_map(rates: dict[str, float], cores: float, mem: float):
                    cpu_rate = rates.get("cpu_per_core_hour")
                    mem_rate = rates.get("mem_per_gib_hour")
                    if cpu_rate is None or mem_rate is None or cpu_rate <= 0 or mem_rate <= 0:
                        return None
                    return float(cpu_rate) * float(cores) + float(mem_rate) * float(mem)
                def exact_gcp_hourly_map(machine: str, family: str, cores: float, mem: float, rates: dict[str, float]):
                    predefined = rates.get("predefined_prices", {}) if isinstance(rates, dict) else {}
                    if machine in predefined:
                        return predefined[machine]
                    if "-custom-" in machine:
                        return _compute_custom_hourly_map(rates, cores, mem)
                    if family == "e2":
                        return None
                    family_rates = rates.get("family_rates", {}) if isinstance(rates, dict) else {}
                    fam = family_rates.get(family, {}) if isinstance(family_rates, dict) else {}
                    cpu_rate = fam.get("cpu") if isinstance(fam, dict) else None
                    mem_rate = fam.get("mem") if isinstance(fam, dict) else None
                    if cpu_rate is None:
                        cpu_rate = rates.get("cpu_per_core_hour")
                    if mem_rate is None:
                        mem_rate = rates.get("mem_per_gib_hour")
                    if cpu_rate is None or mem_rate is None or cpu_rate <= 0 or mem_rate <= 0:
                        return None
                    return float(cpu_rate) * float(cores) + float(mem_rate) * float(mem)
                aws_instance = str(row.get("AWS Instance") or "")
                mapping = instance_map.get(aws_instance.lower()) if instance_map else None
                if mapping:
                    gcp_machine = str(mapping.get("gcp_inst") or "")
                    gcp_cores = float(mapping.get("gcp_vcpu") or vcpus or 0)
                    gcp_ram = float(mapping.get("gcp_mem") or mem or 0)
                    family = gcp_machine.split("-", 1)[0] if gcp_machine else ""
                    gcp_hourly = exact_gcp_hourly_map(gcp_machine, family, gcp_cores, gcp_ram, gcp_cpu_mem) if family else None
                else:
                    pref = gcp_family_pref or build_gcp_family_preference(aws_instance)
                    exact = find_exact_gcp_machine(
                        zones_map,
                        int(vcpus) if pd.notna(vcpus) else None,
                        float(mem) if pd.notna(mem) else None,
                        pref,
                    )
                    if exact:
                        gcp_zone, gcp_machine = exact
                        family = gcp_machine.split("-", 1)[0] if gcp_machine else ""
                        gcp_hourly = exact_gcp_hourly_map(gcp_machine, family, float(vcpus), float(mem), gcp_cpu_mem) if family else None
                    else:
                        # fallback to custom machine pricing using CPU+MEM SKUs
                        if pd.notna(vcpus) and pd.notna(mem):
                            mem_mb = int(round(float(mem) * 1024))
                            family = pref[0] if pref else "n2"
                            gcp_machine = f"{family}-custom-{int(vcpus)}-{mem_mb}"
                            if zones_map:
                                gcp_zone = next(iter(zones_map.keys()))
                            gcp_hourly = exact_gcp_hourly_map(gcp_machine, family, float(vcpus), float(mem), gcp_cpu_mem)

                gcp_on_demand_total = (gcp_hourly * hours) if (gcp_hourly is not None and hours) else None
                gcp_3yr_total = None
                gcp_3yr_eff_hour = None
                pct_diff = None

                if gcp_on_demand_total is not None and args.coverage_pct is not None and args.flex_rate is not None:
                    covered = gcp_on_demand_total * (args.coverage_pct / 100.0)
                    flex_charge = covered * args.flex_rate
                    uncovered = gcp_on_demand_total - covered
                    gcp_3yr_total = flex_charge + uncovered + float(args.resource_commit or 0.0)
                    gcp_3yr_eff_hour = (gcp_3yr_total / hours) if hours else None
                elif gcp_on_demand_total is not None:
                    # if coverage not requested show on-demand only
                    gcp_3yr_total = gcp_on_demand_total
                    gcp_3yr_eff_hour = (gcp_on_demand_total / hours) if hours else None

                aws_eff_hour = (aws_cost / hours) if hours else None
                if gcp_3yr_eff_hour and aws_eff_hour:
                    try:
                        pct_diff = ((aws_eff_hour - gcp_3yr_eff_hour) / gcp_3yr_eff_hour) * 100.0
                    except Exception:
                        pct_diff = None

                gcp_mapping_rows.append({
                    "AWS Region": region_name,
                    "AWS OS": os_name,
                    "AWS Instance": row.get("AWS Instance"),
                    "AWS Cores": vcpus,
                    "AWS MemoryGiB": mem,
                    "AWS Hours": hours,
                    "AWS Cost": aws_cost,
                    "AWS Eff Hour": aws_eff_hour,
                    "GCP Zone": gcp_zone,
                    "GCP Machine": gcp_machine,
                    "GCP Hourly": gcp_hourly,
                    "GCP OnDemand Total": gcp_on_demand_total,
                    "GCP 3yr Total": gcp_3yr_total,
                    "GCP 3yr Eff Hour": gcp_3yr_eff_hour,
                    "% Diff AWS vs GCP 3yr": pct_diff,
                })

    # ----------------------------- write Excel output -----------------------------
    xlsx_path = out_dir / "service_summary.xlsx"
    try:
        writer = pd.ExcelWriter(xlsx_path, engine="xlsxwriter")
    except PermissionError:
        ts = time.strftime("%Y%m%d_%H%M%S")
        xlsx_path = out_dir / f"service_summary_{ts}.xlsx"
        print(f"Warning: output file locked; writing to {xlsx_path} instead.")
        writer = pd.ExcelWriter(xlsx_path, engine="xlsxwriter")
    with writer:
        currency_fmt = writer.book.add_format({"num_format": "$#,##0.00"})
        rate_fmt = writer.book.add_format({"num_format": "$0.############"})
        pct_fmt = writer.book.add_format({"num_format": "0.00%"})
        header_fmt = writer.book.add_format({"bold": True, "bg_color": "#DDEBF7", "border": 1})

        # CUD Config sheet (editable)
        cfg_ws = writer.book.add_worksheet("CUD Config")
        cfg_ws.write_row(0, 0, ["Term", "Discount %", "CUD Factor"], header_fmt)
        cfg_ws.write_row(1, 0, ["1 Yr", 0.28, "=1-B2"])
        cfg_ws.write_row(2, 0, ["3 Yr", 0.46, "=1-B3"])
        cfg_ws.set_column(1, 1, None, pct_fmt)

        if scenario_df is not None:
            scenario_df.to_excel(writer, index=False, sheet_name="Coverage Scenario")
            ws = writer.sheets["Coverage Scenario"]
            ws.set_column(1, 1, None, currency_fmt)
            format_table(ws, 0, scenario_df, writer.book.add_format({"border": 1}), header_fmt)
            for idx, width in enumerate(auto_widths(scenario_df)):
                ws.set_column(idx, idx, width)

        if region_tables:
            sheet_name = "Compute By Region"
            startrow = 0
            border_fmt = writer.book.add_format({"border": 1})
            total_fmt = writer.book.add_format({"bold": True})
            for region_key in sorted(region_tables):
                region_df = region_tables[region_key].copy()
                region_df.loc[region_df["AWS Instance"] == "Total", "Region"] = ""
                data_rows = region_df[region_df["AWS Instance"] != "Total"]
                total_rows = region_df[region_df["AWS Instance"] == "Total"]
                region_df = pd.concat([data_rows, total_rows], ignore_index=True)
                region_df.to_excel(writer, index=False, sheet_name=sheet_name, startrow=startrow)
                worksheet = writer.sheets[sheet_name]
                for col_idx, col_name in enumerate(region_df.columns):
                    worksheet.write(startrow, col_idx, col_name, header_fmt)
                rows, cols = region_df.shape
                end_row = startrow + rows
                end_col = cols - 1
                worksheet.conditional_format(startrow, 0, end_row, end_col, {"type": "no_blanks", "format": border_fmt})
                worksheet.conditional_format(startrow, 0, end_row, end_col, {"type": "blanks", "format": border_fmt})
                worksheet.set_row(end_row, None, total_fmt)
                for idx, width in enumerate(auto_widths(region_df)):
                    worksheet.set_column(idx, idx, width)
                    col_name = str(region_df.columns[idx])
                    if col_name in {"AWS Cost", "PAYG", "1 Yr CUD", "3 Yr CUD", "GCP Per Hour"}:
                        worksheet.set_column(idx, idx, width, currency_fmt)
                    elif col_name == "AWS Per Hour":
                        worksheet.set_column(idx, idx, width, rate_fmt)
                apply_cud_formulas(worksheet, startrow, region_df)
                startrow += rows + 2
            if overall_compute_df is not None:
                totals = overall_compute_df[overall_compute_df["Region"] == "Total"].copy()
                if not totals.empty:
                    totals.loc[:, "Region"] = "Grand Total"
                    totals.to_excel(writer, index=False, sheet_name=sheet_name, startrow=startrow, header=False)
                    rows, cols = totals.shape
                    end_row = startrow
                    end_col = cols - 1
                    worksheet = writer.sheets[sheet_name]
                    worksheet.conditional_format(startrow, 0, end_row, end_col, {"type": "no_blanks", "format": border_fmt})
                    worksheet.conditional_format(startrow, 0, end_row, end_col, {"type": "blanks", "format": border_fmt})
                    worksheet.set_row(end_row, None, total_fmt)
                    for idx, width in enumerate(auto_widths(totals)):
                        worksheet.set_column(idx, idx, width)
                        col_name = str(totals.columns[idx])
                        if col_name in {"AWS Cost", "PAYG", "1 Yr CUD", "3 Yr CUD", "GCP Per Hour"}:
                            worksheet.set_column(idx, idx, width, currency_fmt)
                        elif col_name == "AWS Per Hour":
                            worksheet.set_column(idx, idx, width, rate_fmt)
                    apply_cud_formulas(worksheet, startrow, totals)
            worksheet = writer.sheets[sheet_name]

            if service_region_tables:
                startrow += 2
                worksheet = writer.sheets[sheet_name]
                worksheet.write(startrow, 0, "Storage (EBS + Snapshot)", header_fmt)
                startrow += 1
                for region_name in sorted(service_region_tables):
                    region_df = service_region_tables[region_name].copy()
                    region_df.to_excel(writer, index=False, sheet_name=sheet_name, startrow=startrow)
                    worksheet = writer.sheets[sheet_name]
                    for col_idx, col_name in enumerate(region_df.columns):
                        worksheet.write(startrow, col_idx, col_name, header_fmt)
                    rows, cols = region_df.shape
                    end_row = startrow + rows
                    end_col = cols - 1
                    worksheet.conditional_format(startrow, 0, end_row, end_col, {"type": "no_blanks", "format": border_fmt})
                    worksheet.conditional_format(startrow, 0, end_row, end_col, {"type": "blanks", "format": border_fmt})
                    worksheet.conditional_format(startrow + 1, 3, end_row, 3, {"type": "no_blanks", "format": currency_fmt})
                    worksheet.set_row(end_row, None, total_fmt)
                    for idx, width in enumerate(auto_widths(region_df)):
                        worksheet.set_column(idx, idx, width)
                    startrow += rows + 2

            if nat_region_tables:
                worksheet = writer.sheets[sheet_name]
                worksheet.write(startrow, 0, "NAT Gateway", header_fmt)
                startrow += 1
                for region_name in sorted(nat_region_tables):
                    region_df = nat_region_tables[region_name].copy()
                    region_df.to_excel(writer, index=False, sheet_name=sheet_name, startrow=startrow)
                    worksheet = writer.sheets[sheet_name]
                    for col_idx, col_name in enumerate(region_df.columns):
                        worksheet.write(startrow, col_idx, col_name, header_fmt)
                    rows, cols = region_df.shape
                    end_row = startrow + rows
                    end_col = cols - 1
                    worksheet.conditional_format(startrow, 0, end_row, end_col, {"type": "no_blanks", "format": border_fmt})
                    worksheet.conditional_format(startrow, 0, end_row, end_col, {"type": "blanks", "format": border_fmt})
                    worksheet.conditional_format(startrow + 1, 3, end_row, 3, {"type": "no_blanks", "format": currency_fmt})
                    worksheet.set_row(end_row, None, total_fmt)
                    for idx, width in enumerate(auto_widths(region_df)):
                        worksheet.set_column(idx, idx, width)
                    startrow += rows + 2
        elif service_region_tables or nat_region_tables:
            sheet_name = "Compute By Region"
            startrow = 0
            border_fmt = writer.book.add_format({"border": 1})
            total_fmt = writer.book.add_format({"bold": True})
            if service_region_tables:
                worksheet = writer.sheets.get(sheet_name)
                if worksheet is None:
                    worksheet = writer.book.add_worksheet(sheet_name)
                    writer.sheets[sheet_name] = worksheet
                worksheet.write(startrow, 0, "Storage (EBS + Snapshot)", header_fmt)
                startrow += 1
                for region_name in sorted(service_region_tables):
                    region_df = service_region_tables[region_name].copy()
                    region_df.to_excel(writer, index=False, sheet_name=sheet_name, startrow=startrow)
                    worksheet = writer.sheets[sheet_name]
                    for col_idx, col_name in enumerate(region_df.columns):
                        worksheet.write(startrow, col_idx, col_name, header_fmt)
                    rows, cols = region_df.shape
                    end_row = startrow + rows
                    end_col = cols - 1
                    worksheet.conditional_format(startrow, 0, end_row, end_col, {"type": "no_blanks", "format": border_fmt})
                    worksheet.conditional_format(startrow, 0, end_row, end_col, {"type": "blanks", "format": border_fmt})
                    worksheet.conditional_format(startrow + 1, 3, end_row, 3, {"type": "no_blanks", "format": currency_fmt})
                    worksheet.set_row(end_row, None, total_fmt)
                    for idx, width in enumerate(auto_widths(region_df)):
                        worksheet.set_column(idx, idx, width)
                    startrow += rows + 2

            if nat_region_tables:
                worksheet = writer.sheets[sheet_name]
                worksheet.write(startrow, 0, "NAT Gateway", header_fmt)
                startrow += 1
                for region_name in sorted(nat_region_tables):
                    region_df = nat_region_tables[region_name].copy()
                    region_df.to_excel(writer, index=False, sheet_name=sheet_name, startrow=startrow)
                    worksheet = writer.sheets[sheet_name]
                    for col_idx, col_name in enumerate(region_df.columns):
                        worksheet.write(startrow, col_idx, col_name, header_fmt)
                    rows, cols = region_df.shape
                    end_row = startrow + rows
                    end_col = cols - 1
                    worksheet.conditional_format(startrow, 0, end_row, end_col, {"type": "no_blanks", "format": border_fmt})
                    worksheet.conditional_format(startrow, 0, end_row, end_col, {"type": "blanks", "format": border_fmt})
                    worksheet.conditional_format(startrow + 1, 3, end_row, 3, {"type": "no_blanks", "format": currency_fmt})
                    worksheet.set_row(end_row, None, total_fmt)
                    for idx, width in enumerate(auto_widths(region_df)):
                        worksheet.set_column(idx, idx, width)
                    startrow += rows + 2

        if spot_tables:
            sheet_name = "Spot By Region"
            startrow = 0
            border_fmt = writer.book.add_format({"border": 1})
            total_fmt = writer.book.add_format({"bold": True})
            for region_key in sorted(spot_tables):
                region_df = spot_tables[region_key].copy()
                region_df.loc[region_df["AWS Instance"] == "Total", "Region"] = ""
                data_rows = region_df[region_df["AWS Instance"] != "Total"]
                total_rows = region_df[region_df["AWS Instance"] == "Total"]
                region_df = pd.concat([data_rows, total_rows], ignore_index=True)
                region_df.to_excel(writer, index=False, sheet_name=sheet_name, startrow=startrow)
                worksheet = writer.sheets[sheet_name]
                for col_idx, col_name in enumerate(region_df.columns):
                    worksheet.write(startrow, col_idx, col_name, header_fmt)
                rows, cols = region_df.shape
                end_row = startrow + rows
                end_col = cols - 1
                worksheet.conditional_format(startrow, 0, end_row, end_col, {"type": "no_blanks", "format": border_fmt})
                worksheet.conditional_format(startrow, 0, end_row, end_col, {"type": "blanks", "format": border_fmt})
                worksheet.set_row(end_row, None, total_fmt)
                for idx, width in enumerate(auto_widths(region_df)):
                    worksheet.set_column(idx, idx, width)
                    col_name = str(region_df.columns[idx])
                    if col_name in {"AWS Cost", "PAYG", "1 Yr CUD", "3 Yr CUD", "GCP Per Hour"}:
                        worksheet.set_column(idx, idx, width, currency_fmt)
                    elif col_name == "AWS Per Hour":
                        worksheet.set_column(idx, idx, width, rate_fmt)
                    col_name = str(region_df.columns[idx])
                    if col_name in {"AWS Cost", "PAYG", "1 Yr CUD", "3 Yr CUD"}:
                        worksheet.set_column(idx, idx, width, currency_fmt)
                    elif col_name == "AWS Per Hour":
                        worksheet.set_column(idx, idx, width, rate_fmt)
                startrow += rows + 2
            worksheet = writer.sheets[sheet_name]

        if rds_region_tables:
            sheet_name = "RDS By Region"
            startrow = 0
            border_fmt = writer.book.add_format({"border": 1})
            total_fmt = writer.book.add_format({"bold": True})
            for region_name in sorted(rds_region_tables):
                tables = rds_region_tables[region_name]
                for _, region_df in tables:
                    region_df = region_df.copy()
                    region_df.to_excel(writer, index=False, sheet_name=sheet_name, startrow=startrow)
                    worksheet = writer.sheets[sheet_name]
                    for col_idx, col_name in enumerate(region_df.columns):
                        worksheet.write(startrow, col_idx, col_name, header_fmt)
                    rows, cols = region_df.shape
                    end_row = startrow + rows
                    end_col = cols - 1
                    worksheet.conditional_format(startrow, 0, end_row, end_col, {"type": "no_blanks", "format": border_fmt})
                    worksheet.conditional_format(startrow, 0, end_row, end_col, {"type": "blanks", "format": border_fmt})
                    for col_idx, col_name in enumerate(region_df.columns):
                        if str(col_name).lower() in {"cost", "payg", "1 yr cud", "3 yr cud"}:
                            worksheet.conditional_format(startrow + 1, col_idx, end_row, col_idx, {"type": "no_blanks", "format": currency_fmt})
                    worksheet.set_row(end_row, None, total_fmt)
                    for idx, width in enumerate(auto_widths(region_df)):
                        worksheet.set_column(idx, idx, width)
                    apply_cud_formulas(worksheet, startrow, region_df)
                    startrow += rows + 2

        # Write AWS->GCP mapping sheet if available
        if gcp_mapping_rows:
            gcp_mapping_df = pd.DataFrame(gcp_mapping_rows)
            gcp_mapping_df.to_excel(writer, index=False, sheet_name="AWS->GCP Mapping")
            ws = writer.sheets["AWS->GCP Mapping"]
            # try sensible currency formatting columns by name
            for col_idx, col_name in enumerate(gcp_mapping_df.columns):
                if "Cost" in str(col_name) or "Total" in str(col_name) or "AWS Cost" in str(col_name) or "GCP Hourly" in str(col_name):
                    try:
                        ws.set_column(col_idx, col_idx, None, currency_fmt)
                    except Exception:
                        pass
            for idx, width in enumerate(auto_widths(gcp_mapping_df)):
                ws.set_column(idx, idx, width)

    print(f"Wrote {xlsx_path}")


if __name__ == "__main__":
    main()
