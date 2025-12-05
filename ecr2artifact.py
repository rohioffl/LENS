#!/usr/bin/env python3
"""
Batch migrate all AWS ECR repositories to GCP Artifact Registry.

- Preserves repo names by default (one Artifact repo per ECR repo, derived safely).
- Supports parallel repo migration AND per-repo image parallelism.
- Skips pushes when the target image tag already exists (checks docker manifest).
"""

from __future__ import annotations

import argparse
import base64
import os
import re
import shutil
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any, Dict, List, Optional

import boto3
from botocore.exceptions import BotoCoreError, ClientError


class CommandExecutionError(RuntimeError):
    """Raised when a shell command fails after exhausting retries."""


# ---------- Defaults ----------
DEFAULT_AWS_REGION = "ap-south-1"
DEFAULT_GCP_REGION = "asia-southeast1"
DEFAULT_GCP_PROJECT = "production-472606"

DOCKER_CMD_RETRIES = 5
DOCKER_RETRY_BASE_DELAY = 8.0  # seconds
CLOUD_CMD_RETRIES = 3
ECR_TOKEN_TTL = 60 * 60 * 8  # 8 hours
GCP_TOKEN_TTL = 60 * 45  # 45 minutes

_LAST_ECR_LOGIN: float = 0.0
_LAST_GCP_LOGIN: float = 0.0

LOGIN_LOCK = threading.Lock()


# ---------- Helpers ----------
def configure_boto3_session(
    access_key: Optional[str] = None,
    secret_key: Optional[str] = None,
    session_token: Optional[str] = None,
    profile_name: Optional[str] = None,
):
    """Configure boto3's default session so downstream clients reuse provided credentials."""
    session_kwargs: Dict[str, Any] = {}
    if profile_name:
        session_kwargs["profile_name"] = profile_name
    if access_key and secret_key:
        session_kwargs["aws_access_key_id"] = access_key
        session_kwargs["aws_secret_access_key"] = secret_key
        if session_token:
            session_kwargs["aws_session_token"] = session_token
    elif any((access_key, secret_key, session_token)):
        raise CommandExecutionError("Both AWS access key and secret key are required when overriding credentials.")

    if session_kwargs:
        boto3.setup_default_session(**session_kwargs)


def run_cmd(
    cmd: List[str],
    *,
    input_text: Optional[str] = None,
    capture_output: bool = False,
    retries: int = 1,
    retry_delay: float = 5.0,
    timeout: Optional[float] = None,
) -> Optional[str]:
    """Run a shell command with retries and optional output capture."""
    attempt = 1
    while True:
        print(f"[cmd] {' '.join(cmd)}")
        try:
            result = subprocess.run(
                cmd,
                check=True,
                input=input_text,
                capture_output=capture_output,
                text=True,
                timeout=timeout,
            )
        except subprocess.CalledProcessError as exc:
            if attempt >= max(1, retries):
                message = f"Failed: {' '.join(cmd)}"
                print(message)
                raise CommandExecutionError(message) from exc
            delay = retry_delay * attempt
            print(f"  Command failed (attempt {attempt}/{retries}); retrying in {delay:.1f}s")
            time.sleep(delay)
            attempt += 1
            continue
        except subprocess.TimeoutExpired as exc:
            if attempt >= max(1, retries):
                message = f"Timed out: {' '.join(cmd)}"
                print(message)
                raise CommandExecutionError(message) from exc
            delay = retry_delay * attempt
            print(f"  Command timed out (attempt {attempt}/{retries}); retrying in {delay:.1f}s")
            time.sleep(delay)
            attempt += 1
            continue
        except FileNotFoundError as exc:
            message = f"Required executable not found: {cmd[0]}. Install it and ensure it is on PATH."
            print(message)
            raise CommandExecutionError(message) from exc
        break

    if capture_output:
        return (result.stdout or "").strip()
    return None


def ensure_cli_tool(binary: str, install_hint: Optional[str] = None):
    """Ensure the required CLI tool exists before running commands."""
    if shutil.which(binary):
        return
    message = f"Required executable '{binary}' not found in PATH."
    if install_hint:
        message = f"{message} {install_hint}"
    print(message)
    sys.exit(1)


def remove_local_docker_images(*refs: str):
    """Remove local Docker images for the provided references without affecting remote registries."""
    for ref in refs:
        if not ref:
            continue
        cmd = ["docker", "image", "rm", ref]
        print(f"[cmd] {' '.join(cmd)}")
        result = subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        if result.returncode == 0:
            print(f"  Removed local Docker cache: {ref}")
        else:
            print(f"  Could not remove local Docker cache for {ref} (might already be absent). Continuing.")


def docker_image_exists(ref: str) -> bool:
    """Return True if the given image reference already exists in a registry."""
    try:
        subprocess.run(
            ["docker", "manifest", "inspect", ref],
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        return True
    except subprocess.CalledProcessError:
        return False
    except FileNotFoundError:
        print("  'docker' CLI not available to check existing images; proceeding without skip.")
        return False


def login_to_ecr(region: str):
    """Authenticate the local Docker client with AWS ECR for the provided region."""
    client = boto3.client("ecr", region_name=region)
    try:
        response = client.get_authorization_token()
    except (BotoCoreError, ClientError) as exc:
        print(f"Unable to obtain ECR authorization token: {exc}")
        sys.exit(1)

    auth_entries = response.get("authorizationData") or []
    if not auth_entries:
        print("No authorization data received from ECR.")
        sys.exit(1)

    success = False
    for entry in auth_entries:
        token = entry.get("authorizationToken")
        proxy = entry.get("proxyEndpoint")
        if not token or not proxy:
            continue

        decoded = base64.b64decode(token).decode("utf-8")
        username, password = decoded.split(":", 1)
        run_cmd(
            ["docker", "login", "-u", username, "--password-stdin", proxy],
            input_text=password + "\n",
            retries=CLOUD_CMD_RETRIES,
            retry_delay=DOCKER_RETRY_BASE_DELAY,
        )
        print(f"  Docker authenticated against ECR registry: {proxy}")
        success = True

    if not success:
        print("Failed to decode ECR authorization token.")
        sys.exit(1)


def login_to_artifact_registry(gcp_region: str):
    """Authenticate the local Docker client with Google Artifact Registry."""
    registry_host = f"{gcp_region}-docker.pkg.dev"
    token = run_cmd(
        ["gcloud", "auth", "print-access-token"],
        capture_output=True,
        retries=CLOUD_CMD_RETRIES,
        retry_delay=DOCKER_RETRY_BASE_DELAY,
    )
    if not token:
        print("Unable to obtain access token from gcloud.")
        sys.exit(1)

    run_cmd(
        ["docker", "login", "-u", "oauth2accesstoken", "--password-stdin", registry_host],
        input_text=token + "\n",
        retries=CLOUD_CMD_RETRIES,
        retry_delay=DOCKER_RETRY_BASE_DELAY,
    )
    print(f"  Docker authenticated against Artifact Registry host: {registry_host}")


def refresh_registry_sessions(aws_region: str, gcp_region: str, force: bool = False):
    """Ensure Docker is authenticated against both AWS ECR and GCP Artifact Registry."""
    global _LAST_ECR_LOGIN, _LAST_GCP_LOGIN
    with LOGIN_LOCK:
        now = time.monotonic()

        if force or (now - _LAST_ECR_LOGIN) >= ECR_TOKEN_TTL:
            login_to_ecr(aws_region)
            _LAST_ECR_LOGIN = time.monotonic()

        if force or (now - _LAST_GCP_LOGIN) >= GCP_TOKEN_TTL:
            login_to_artifact_registry(gcp_region)
            _LAST_GCP_LOGIN = time.monotonic()


def list_ecr_repositories(region: str) -> List[Dict[str, Any]]:
    """Fetch all repositories in AWS ECR."""
    client = boto3.client("ecr", region_name=region)
    repos = []
    paginator = client.get_paginator("describe_repositories")
    for page in paginator.paginate(PaginationConfig={"PageSize": 1000}):
        repos.extend(page["repositories"])
    return repos


def list_ecr_images(region: str, repo_name: str) -> List[Dict[str, Any]]:
    """Fetch all images (tagged and untagged) in a repository."""
    client = boto3.client("ecr", region_name=region)
    images: List[Dict[str, Any]] = []
    paginator = client.get_paginator("describe_images")
    for page in paginator.paginate(repositoryName=repo_name, PaginationConfig={"PageSize": 1000}):
        for detail in page.get("imageDetails", []):
            digest = detail.get("imageDigest")
            if not digest:
                continue

            tags = detail.get("imageTags") or []
            if not tags:
                images.append({"tag": None, "digest": digest})
                continue

            for tag in tags:
                images.append({"tag": tag, "digest": digest})

    return images


def to_artifact_repo_id(repo_name: str) -> str:
    """Convert an ECR repository name into a valid Artifact Registry repo ID."""
    normalized = repo_name.lower().replace("/", "-")
    normalized = re.sub(r"[^a-z0-9-]", "-", normalized)
    normalized = re.sub(r"-+", "-", normalized).strip("-")
    if not normalized:
        normalized = "repo"
    if not normalized[0].isalpha():
        normalized = f"repo-{normalized}"
    return normalized[:63].rstrip("-") or "repo"


def ensure_artifact_repo(
    gcp_project: str, gcp_region: str, repo_name: str, repo_id: Optional[str] = None
) -> str:
    """Ensure the Artifact Registry repository exists before pushing images."""
    repo_id = repo_id or to_artifact_repo_id(repo_name)
    if repo_id != repo_name:
        print(f"  Using Artifact Registry repo ID '{repo_id}' for ECR repo '{repo_name}'")

    describe_cmd = [
        "gcloud",
        "artifacts",
        "repositories",
        "describe",
        repo_id,
        "--project",
        gcp_project,
        "--location",
        gcp_region,
    ]

    try:
        for attempt in range(1, CLOUD_CMD_RETRIES + 1):
            try:
                subprocess.run(describe_cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                print(f"  Artifact Registry repo exists: {repo_id}")
                return repo_id
            except subprocess.CalledProcessError:
                if attempt >= CLOUD_CMD_RETRIES:
                    break
                delay = DOCKER_RETRY_BASE_DELAY * attempt
                print(f"  gcloud describe failed (attempt {attempt}/{CLOUD_CMD_RETRIES}); retrying in {delay:.1f}s")
                time.sleep(delay)
    except FileNotFoundError:
        print("gcloud CLI not found. Install it or ensure it's on PATH.")
        sys.exit(1)

    print(f"  Creating Artifact Registry repo: {repo_id}")

    create_cmd = [
        "gcloud",
        "artifacts",
        "repositories",
        "create",
        repo_id,
        "--repository-format=docker",
        "--project",
        gcp_project,
        "--location",
        gcp_region,
    ]
    run_cmd(
        create_cmd,
        retries=CLOUD_CMD_RETRIES,
        retry_delay=DOCKER_RETRY_BASE_DELAY,
    )
    return repo_id


def match_ecr_repos_to_artifact(repos: List[Dict[str, Any]]) -> Dict[str, str]:
    """Map ECR repository names to Artifact Registry IDs and guard against collisions."""
    mapping: Dict[str, str] = {}
    reverse: Dict[str, str] = {}

    for repo in repos:
        repo_name = repo.get("repositoryName")
        if not repo_name:
            continue

        repo_id = to_artifact_repo_id(repo_name)
        other = reverse.get(repo_id)
        if other and other != repo_name:
            raise CommandExecutionError(
                f"Conflicting ECR repositories map to the same Artifact Registry repo ID: '{repo_name}' and '{other}' -> '{repo_id}'"
            )

        reverse[repo_id] = repo_name
        mapping[repo_name] = repo_id

    return mapping


# ---------- Migration ----------
def migrate_repository(
    region: str,
    gcp_project: str,
    gcp_region: str,
    repo: Dict[str, Any],
    repo_mapping: Dict[str, str],
    image_workers: int,
):
    repo_name = repo["repositoryName"]
    repo_uri = repo["repositoryUri"]
    print(f"\nRepository: {repo_uri}")

    refresh_registry_sessions(region, gcp_region)
    images = list_ecr_images(region, repo_name)
    if not images:
        print("  (No images found)")
        return

    target_repo_id = repo_mapping.get(repo_name)
    target_repo_id = ensure_artifact_repo(gcp_project, gcp_region, repo_name, target_repo_id)
    image_workers = max(1, image_workers)

    def migrate_single_image(img: Dict[str, Any]):
        refresh_registry_sessions(region, gcp_region)
        tag = img.get("tag")
        digest = img.get("digest")

        if tag:
            source_ref = f"{repo_uri}:{tag}"
            target_tag = tag
            display_source = source_ref
        else:
            short_digest = (digest or "").split(":", 1)[-1][:12]
            target_tag = f"digest-{short_digest}" if short_digest else "digest"
            source_ref = f"{repo_uri}@{digest}"
            display_source = f"{source_ref} (untagged)"

        gcp_image = f"{gcp_region}-docker.pkg.dev/{gcp_project}/{target_repo_id}/{repo_name}:{target_tag}"

        if docker_image_exists(gcp_image):
            print(f"  Skip: {display_source} already present as {gcp_image}")
            return

        print(f"  Migrating {display_source} -> {gcp_image}")

        run_cmd(
            ["docker", "pull", source_ref],
            retries=DOCKER_CMD_RETRIES,
            retry_delay=DOCKER_RETRY_BASE_DELAY,
        )
        run_cmd(
            ["docker", "tag", source_ref, gcp_image],
            retries=CLOUD_CMD_RETRIES,
            retry_delay=DOCKER_RETRY_BASE_DELAY,
        )
        run_cmd(
            ["docker", "push", gcp_image],
            retries=DOCKER_CMD_RETRIES,
            retry_delay=DOCKER_RETRY_BASE_DELAY,
        )
        remove_local_docker_images(source_ref, gcp_image)

        print(f"  Done: {display_source} -> {gcp_image}")

    with ThreadPoolExecutor(max_workers=image_workers) as executor:
        future_to_image = {executor.submit(migrate_single_image, img): img for img in images}
        for future in as_completed(future_to_image):
            image = future_to_image[future]
            try:
                future.result()
            except CommandExecutionError as exc:
                for pending in future_to_image:
                    pending.cancel()
                raise CommandExecutionError(
                    f"Image migration failed for repo '{repo_name}' (tag={image.get('tag')}, digest={image.get('digest')}): {exc}"
                ) from exc
            except Exception as exc:
                for pending in future_to_image:
                    pending.cancel()
                raise CommandExecutionError(
                    f"Unexpected error while migrating repo '{repo_name}' image (tag={image.get('tag')}, digest={image.get('digest')})."
                ) from exc


def migrate_all(
    region: str,
    gcp_project: str,
    gcp_region: str,
    yes: bool,
    workers: int,
    image_workers: int,
    repo_names: Optional[List[str]] = None,
):
    repos = list_ecr_repositories(region)
    if repo_names:
        repo_set = {name.strip() for name in repo_names if name and name.strip()}
        repos = [repo for repo in repos if repo.get("repositoryName") in repo_set]
    print(f"Found {len(repos)} ECR repositories in {region}")

    repo_mapping = match_ecr_repos_to_artifact(repos)

    if not yes:
        proceed = input("Proceed with migration of all repos (names preserved)? [y/N]: ").strip().lower()
        if proceed not in {"y", "yes"}:
            print("Migration cancelled.")
            return

    workers = max(1, workers)
    # Use a single knob for both repo-level and image-level concurrency.
    image_workers = workers

    if workers == 1:
        for repo in repos:
            migrate_repository(region, gcp_project, gcp_region, repo, repo_mapping, image_workers)
        return

    future_to_repo = {}
    with ThreadPoolExecutor(max_workers=workers) as executor:
        for repo in repos:
            future = executor.submit(
                migrate_repository,
                region,
                gcp_project,
                gcp_region,
                repo,
                repo_mapping,
                image_workers,
            )
            future_to_repo[future] = repo

        for future in as_completed(future_to_repo):
            repo = future_to_repo[future]
            try:
                future.result()
            except CommandExecutionError as exc:
                for pending_future in future_to_repo:
                    pending_future.cancel()
                raise CommandExecutionError(f"Migration failed for {repo['repositoryName']}: {exc}") from exc
            except Exception as exc:
                for pending_future in future_to_repo:
                    pending_future.cancel()
                raise CommandExecutionError(
                    f"Unexpected error while migrating repository '{repo['repositoryName']}'."
                ) from exc


# ---------- CLI ----------
def main():
    parser = argparse.ArgumentParser(
        description="Migrate all ECR repos -> GCP Artifact Registry (preserve repo names) with repo/image parallelism"
    )
    parser.add_argument("--aws-region", default=DEFAULT_AWS_REGION, help="AWS region")
    parser.add_argument("--gcp-project", default=DEFAULT_GCP_PROJECT, help="Target GCP project ID")
    parser.add_argument("--gcp-region", default=DEFAULT_GCP_REGION, help="Artifact Registry region")
    parser.add_argument("-y", "--yes", action="store_true", help="Skip confirmation prompt")
    parser.add_argument(
        "--workers",
        type=int,
        default=4,
        help="Parallelism for repositories and images (one setting for both; default: 4)",
    )
    args = parser.parse_args()

    ensure_cli_tool("docker", "Install Docker and ensure the 'docker' CLI is available.")
    ensure_cli_tool("gcloud", "Install the Google Cloud CLI and ensure 'gcloud' is available.")

    refresh_registry_sessions(args.aws_region, args.gcp_region, force=True)

    try:
        migrate_all(args.aws_region, args.gcp_project, args.gcp_region, args.yes, args.workers, args.workers)
    except CommandExecutionError as exc:
        print(str(exc))
        sys.exit(1)


if __name__ == "__main__":
    main()
