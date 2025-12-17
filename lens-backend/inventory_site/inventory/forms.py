import base64
import json
import os

from django import forms

from inventory.env import load_local_env
from inventory.services.aws_inventory import RESOURCE_MAP

load_local_env()


def _resource_choices():
    yield ("all", "All resources")
    for value in RESOURCE_MAP.values():
        label = value.replace('_', ' ').title()
        yield (value, label)


class AutomationTaskForm(forms.Form):
    task_id = forms.CharField(widget=forms.HiddenInput)


class AwsInventoryForm(AutomationTaskForm):
    profile_name = forms.CharField(required=False, label="AWS Profile")
    access_key = forms.CharField(required=False, label="Access Key ID")
    secret_key = forms.CharField(required=False, widget=forms.PasswordInput, label="Secret Access Key")
    session_token = forms.CharField(required=False, label="Session Token")

    regions = forms.CharField(
        initial="us-east-1",
        help_text="Comma-separated AWS regions (e.g., us-east-1,eu-west-1).",
    )
    resources = forms.MultipleChoiceField(
        choices=tuple(_resource_choices()),
        help_text="Select at least one AWS resource type.",
        initial=['ec2'],
        widget=forms.CheckboxSelectMultiple,
    )
    from_date = forms.CharField(required=False, label="From Date", help_text="Supports YYYY-MM-DD or phrases like 'last 30 days'.")
    to_date = forms.CharField(required=False, label="To Date")

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields['task_id'].initial = self.initial.get('task_id') or 'aws_inventory'

    def clean_regions(self):
        value = self.cleaned_data["regions"]
        regions = [region.strip() for region in value.split(",") if region.strip()]
        if not regions:
            raise forms.ValidationError("Enter at least one AWS region.")
        return regions

    def clean(self):
        cleaned = super().clean()
        access_key = cleaned.get("access_key")
        secret_key = cleaned.get("secret_key")
        resources = cleaned.get("resources") or []

        if access_key and not secret_key:
            self.add_error("secret_key", "Secret key is required when an access key is provided.")
        if secret_key and not access_key:
            self.add_error("access_key", "Access key is required when a secret key is provided.")

        if "all" in resources:
            resources = list(RESOURCE_MAP.values())
            cleaned["resources"] = resources

        if "cost" in resources and not (cleaned.get("from_date") or cleaned.get("to_date")):
            raise forms.ValidationError("Cost reports require at least one date (from/to).")

        return cleaned


class TerraformVpcForm(AutomationTaskForm):
    access_key = forms.CharField(label="AWS Access Key ID")
    secret_key = forms.CharField(widget=forms.PasswordInput, label="AWS Secret Access Key")
    aws_region = forms.CharField(label="AWS Region")
    aws_vpc_id = forms.CharField(label="AWS VPC ID")
    gcp_project = forms.CharField(label="GCP Project ID")
    gcp_network = forms.CharField(label="GCP VPC Network Name")
    gcp_region_fallback = forms.CharField(label="GCP Region Fallback", help_text="Used when AZ → region mapping is ambiguous.")
    subnet_name_map = forms.CharField(
        required=False,
        label="Subnet Name Overrides",
        widget=forms.Textarea(attrs={"rows": 3}),
        help_text="Optional JSON object mapping subnet IDs to desired names.",
    )
    subnet_cidr_map = forms.CharField(
        required=False,
        label="Subnet CIDR Overrides",
        widget=forms.Textarea(attrs={"rows": 3}),
        help_text="Optional JSON object mapping subnet IDs to CIDR blocks.",
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields['task_id'].initial = self.initial.get('task_id') or 'terraform_vpc'

    def clean(self):
        cleaned = super().clean()
        if not cleaned.get("access_key") or not cleaned.get("secret_key"):
            raise forms.ValidationError("AWS access key and secret key are required.")

        required = ["aws_region", "aws_vpc_id", "gcp_project", "gcp_network", "gcp_region_fallback"]
        for field in required:
            if not cleaned.get(field):
                self.add_error(field, "This field is required.")

        return cleaned


class ClassicVpnForm(AutomationTaskForm):
    access_key = forms.CharField(label="AWS Access Key ID")
    secret_key = forms.CharField(widget=forms.PasswordInput, label="AWS Secret Access Key")
    aws_region = forms.CharField(label="AWS Region")
    aws_vpc_id = forms.CharField(label="AWS VPC ID")
    aws_asn = forms.IntegerField(label="AWS ASN", initial=64513, min_value=1)

    gcp_service_key = forms.CharField(widget=forms.Textarea, label="GCP Service Account JSON/Base64")
    gcp_project = forms.CharField(required=False, label="GCP Project ID")
    gcp_region = forms.CharField(label="GCP Region")
    gcp_network = forms.CharField(label="GCP VPC Network Name")
    gcp_asn = forms.IntegerField(label="GCP ASN", initial=64512, min_value=1)
    name_prefix = forms.CharField(required=False, label="Resource Name Prefix (optional)")
    ike_version = forms.IntegerField(label="IKE Version", initial=1, min_value=1, max_value=2)
    aws_subnets = forms.JSONField(required=False, label="Selected AWS Subnets")
    gcp_subnets = forms.JSONField(required=False, label="Selected GCP Subnets")

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["task_id"].initial = self.initial.get("task_id") or "classic_vpn"
        self._service_key_info: dict | None = None

    def clean_gcp_service_key(self):
        value = (self.cleaned_data.get("gcp_service_key") or "").strip()
        if not value:
            raise forms.ValidationError("Provide the service-account JSON.")
        info = None
        try:
            info = json.loads(value)
        except json.JSONDecodeError:
            try:
                decoded = base64.b64decode(value).decode("utf-8")
                info = json.loads(decoded)
            except Exception as exc:
                raise forms.ValidationError("Service key must be valid JSON or base64 encoded JSON.") from exc
        if not isinstance(info, dict):
            raise forms.ValidationError("Service key JSON must be an object.")
        self._service_key_info = info
        return value

    def clean(self):
        cleaned = super().clean()
        required_fields = ["access_key", "secret_key", "aws_region", "aws_vpc_id", "gcp_region", "gcp_network"]
        for field in required_fields:
            if not cleaned.get(field):
                self.add_error(field, "This field is required.")
        if not cleaned.get("gcp_project"):
            project = (self._service_key_info or {}).get("project_id")
            if project:
                cleaned["gcp_project"] = project
            else:
                self.add_error("gcp_project", "GCP project ID is required.")
        for list_field in ("aws_subnets", "gcp_subnets"):
            value = cleaned.get(list_field) or []
            if isinstance(value, str):
                try:
                    value = json.loads(value)
                except json.JSONDecodeError:
                    self.add_error(list_field, "Expected a JSON array.")
                    continue
            if value and not isinstance(value, list):
                self.add_error(list_field, "Provide a JSON array.")
                continue
            cleaned[list_field] = value
        # Enforce ASN constraints
        aws_asn = cleaned.get("aws_asn")
        gcp_asn = cleaned.get("gcp_asn")
        asn_min, asn_max = 64512, 65534
        for field, value in (("aws_asn", aws_asn), ("gcp_asn", gcp_asn)):
            if value is None:
                continue
            if value < asn_min or value > asn_max:
                self.add_error(field, f"ASN must be between {asn_min} and {asn_max}.")
        if aws_asn and gcp_asn and aws_asn == gcp_asn:
            self.add_error("gcp_asn", "AWS ASN and GCP ASN must differ.")
        return cleaned


class EcrMigrationForm(AutomationTaskForm):
    access_key = forms.CharField(label="AWS Access Key ID")
    secret_key = forms.CharField(widget=forms.PasswordInput, label="AWS Secret Access Key")
    aws_region = forms.CharField(label="AWS Region", initial="ap-south-1")
    gcp_service_key = forms.CharField(widget=forms.Textarea, label="GCP Service Account JSON/Base64")
    gcp_project = forms.CharField(label="GCP Project ID")
    gcp_region = forms.CharField(label="Artifact Registry Region", initial="asia-southeast1")
    aws_repos = forms.JSONField(required=False, label="Selected ECR Repos")
    workers = forms.IntegerField(
        label="Parallel Workers",
        initial=4,
        min_value=1,
        help_text="Controls parallelism for both repositories and images.",
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["task_id"].initial = self.initial.get("task_id") or "ecr_migration"
        self._service_key_info: dict | None = None

    def clean_gcp_service_key(self):
        value = (self.cleaned_data.get("gcp_service_key") or "").strip()
        if not value:
            raise forms.ValidationError("Provide the service-account JSON.")
        info = None
        try:
            info = json.loads(value)
        except json.JSONDecodeError:
            try:
                decoded = base64.b64decode(value).decode("utf-8")
                info = json.loads(decoded)
            except Exception as exc:
                raise forms.ValidationError("Service key must be valid JSON or base64 encoded JSON.") from exc
        if not isinstance(info, dict):
            raise forms.ValidationError("Service key JSON must be an object.")
        self._service_key_info = info
        return value

    def clean(self):
        cleaned = super().clean()
        required = ["access_key", "secret_key", "aws_region", "gcp_service_key", "gcp_project", "gcp_region"]
        for field in required:
            if not cleaned.get(field):
                self.add_error(field, "This field is required.")
        workers = cleaned.get("workers")
        if workers is not None and workers < 1:
            self.add_error("workers", "Workers must be at least 1.")
        repos = cleaned.get("aws_repos") or []
        if repos and not isinstance(repos, list):
            self.add_error("aws_repos", "Expected a JSON array of repository names.")
        cleaned["aws_repos"] = repos
        return cleaned


class HaVpnForm(AutomationTaskForm):
    access_key = forms.CharField(label="AWS Access Key ID")
    secret_key = forms.CharField(widget=forms.PasswordInput, label="AWS Secret Access Key")
    aws_region = forms.CharField(label="AWS Region")
    aws_vpc_id = forms.CharField(label="AWS VPC ID")

    gcp_service_key = forms.CharField(label="GCP Service Account JSON/Base64")
    gcp_project = forms.CharField(required=False, label="GCP Project ID")
    gcp_region = forms.CharField(label="GCP Region")
    gcp_network = forms.CharField(label="GCP VPC Network Name")

    aws_asn = forms.IntegerField(label="AWS ASN", initial=64513, min_value=1)
    gcp_asn = forms.IntegerField(label="GCP ASN", initial=64512, min_value=1)
    name_prefix = forms.CharField(required=False, label="Resource Name Prefix")
    aws_subnets = forms.JSONField(required=False, label="AWS Subnets for Propagation")
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["task_id"].initial = self.initial.get("task_id") or "ha_vpn"
        self._service_key_info: dict | None = None

    def clean_gcp_service_key(self):
        value = (self.cleaned_data.get("gcp_service_key") or "").strip()
        if not value:
            raise forms.ValidationError("Provide the service-account JSON.")
        info = None
        try:
            info = json.loads(value)
        except json.JSONDecodeError:
            try:
                decoded = base64.b64decode(value).decode("utf-8")
                info = json.loads(decoded)
            except Exception as exc:
                raise forms.ValidationError("Service key must be valid JSON or base64 encoded JSON.") from exc
        if not isinstance(info, dict):
            raise forms.ValidationError("Service key JSON must be an object.")
        self._service_key_info = info
        return value

    def clean(self):
        cleaned = super().clean()
        required_fields = ["access_key", "secret_key", "aws_region", "aws_vpc_id", "gcp_region", "gcp_network"]
        for field in required_fields:
            if not cleaned.get(field):
                self.add_error(field, "This field is required.")
        if not cleaned.get("gcp_project"):
            project = (self._service_key_info or {}).get("project_id")
            if project:
                cleaned["gcp_project"] = project
            else:
                self.add_error("gcp_project", "GCP project ID is required.")
        value = cleaned.get("aws_subnets")
        if value is None:
            cleaned["aws_subnets"] = None
        else:
            if isinstance(value, str):
                try:
                    value = json.loads(value)
                except json.JSONDecodeError:
                    self.add_error("aws_subnets", "Expected a JSON array of subnet IDs.")
                    return cleaned
            if value and not isinstance(value, list):
                self.add_error("aws_subnets", "Provide a JSON array of subnet IDs.")
                return cleaned
            cleaned["aws_subnets"] = value
        return cleaned


class EcsTerraformForm(AutomationTaskForm):
    access_key = forms.CharField(label="AWS Access Key ID")
    secret_key = forms.CharField(widget=forms.PasswordInput, label="AWS Secret Access Key")
    aws_region = forms.CharField(label="AWS Region")
    cluster_name = forms.CharField(label="ECS Cluster Name")

    gcp_project = forms.CharField(label="GCP Project ID")
    gcp_location = forms.CharField(label="GCP Location")
    gke_cluster_name = forms.CharField(required=False, label="GKE Cluster Name")

    machine_type = forms.CharField(required=False, label="Node Machine Type")
    node_cpu = forms.FloatField(required=False, label="Node CPU (vCPU)")
    node_memory = forms.IntegerField(required=False, label="Node Memory (MB)")
    min_nodes = forms.IntegerField(required=False, label="Min Nodes")
    max_nodes = forms.IntegerField(required=False, label="Max Nodes")
    node_locations = forms.CharField(required=False, label="Node Locations", help_text="Comma-separated list of GCP zones.")

    network = forms.CharField(required=False, label="GCP Network")
    subnetwork = forms.CharField(required=False, label="GCP Subnetwork")
    service_account = forms.CharField(required=False, label="Node Service Account")
    release_channel = forms.CharField(required=False, label="GKE Release Channel")

    private_nodes = forms.BooleanField(required=False, initial=True, label="Use Private Nodes")
    private_endpoint = forms.BooleanField(required=False, label="Enable Private Control-Plane Endpoint")
    master_ipv4_cidr = forms.CharField(required=False, label="Master IPv4 /28 CIDR")

    node_pool_name = forms.CharField(required=False, label="Node Pool Name")
    node_pool_subnet = forms.CharField(required=False, label="Node Pool Subnetwork")
    node_pool_zones = forms.CharField(required=False, label="Node Pool Zones", help_text="Comma-separated zones for this pool.")
    node_pools = forms.JSONField(required=False, label="Node Pools JSON Override")

    services = forms.JSONField(required=False, label="Selected ECS Services")

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["task_id"].initial = self.initial.get("task_id") or "ecs_terraform"

    def clean(self):
        cleaned = super().clean()
        required_fields = ["access_key", "secret_key", "aws_region", "cluster_name", "gcp_project", "gcp_location"]
        for field in required_fields:
            if not cleaned.get(field):
                self.add_error(field, "This field is required.")

        for list_field in ("services", "node_pools"):
            value = cleaned.get(list_field)
            if value in (None, ""):
                cleaned[list_field] = None if list_field == "node_pools" else []
                continue
            if isinstance(value, str):
                try:
                    value = json.loads(value)
                except json.JSONDecodeError:
                    self.add_error(list_field, "Expected a JSON array.")
                    continue
            if value and not isinstance(value, list):
                self.add_error(list_field, "Provide a JSON array.")
                continue
            cleaned[list_field] = value

        return cleaned


class EksTerraformForm(AutomationTaskForm):
    access_key = forms.CharField(label="AWS Access Key ID")
    secret_key = forms.CharField(widget=forms.PasswordInput, label="AWS Secret Access Key")
    aws_region = forms.CharField(label="AWS Region")
    cluster_name = forms.CharField(label="EKS Cluster Name")

    gcp_project = forms.CharField(label="GCP Project ID")
    gcp_location = forms.CharField(label="GCP Location")
    gke_cluster_name = forms.CharField(required=False, label="GKE Cluster Name")

    machine_type = forms.CharField(required=False, label="Node Machine Type")
    node_cpu = forms.FloatField(required=False, label="Node CPU (vCPU)")
    node_memory = forms.IntegerField(required=False, label="Node Memory (MB)")
    min_nodes = forms.IntegerField(required=False, label="Min Nodes")
    max_nodes = forms.IntegerField(required=False, label="Max Nodes")
    node_locations = forms.CharField(required=False, label="Node Locations", help_text="Comma-separated list of GCP zones.")

    network = forms.CharField(required=False, label="GCP Network")
    subnetwork = forms.CharField(required=False, label="GCP Subnetwork")
    service_account = forms.CharField(required=False, label="Node Service Account")
    release_channel = forms.CharField(required=False, label="GKE Release Channel")

    private_nodes = forms.BooleanField(required=False, initial=True, label="Use Private Nodes")
    private_endpoint = forms.BooleanField(required=False, label="Enable Private Control-Plane Endpoint")
    master_ipv4_cidr = forms.CharField(required=False, label="Master IPv4 /28 CIDR")

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["task_id"].initial = self.initial.get("task_id") or "eks_terraform"

    def clean(self):
        cleaned = super().clean()
        required_fields = ["access_key", "secret_key", "aws_region", "cluster_name", "gcp_project", "gcp_location"]
        for field in required_fields:
            if not cleaned.get(field):
                self.add_error(field, "This field is required.")
        return cleaned


class EcsManifestForm(AutomationTaskForm):
    access_key = forms.CharField(label="AWS Access Key ID")
    secret_key = forms.CharField(widget=forms.PasswordInput, label="AWS Secret Access Key")
    aws_region = forms.CharField(label="AWS Region")
    cluster_name = forms.CharField(label="ECS Cluster Name")

    namespace = forms.CharField(required=False, label="Kubernetes Namespace")
    services = forms.JSONField(required=False, label="Selected ECS Services")

    aws_credentials_mode = forms.ChoiceField(
        choices=(("auto", "Auto"), ("yes", "Always inject placeholders"), ("no", "Skip AWS placeholders")),
        initial="auto",
        label="Inject AWS Credential Secrets",
        required=False,
    )
    gemini_model = forms.CharField(required=False, label="Gemini Model")
    gemini_fallbacks = forms.CharField(required=False, label="Gemini Fallbacks", help_text="Comma-separated list.")
    gemini_api_key = forms.CharField(required=False, widget=forms.PasswordInput, label="Gemini API Key Override")

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["task_id"].initial = self.initial.get("task_id") or "ecs_manifests"

    def clean(self):
        cleaned = super().clean()
        required_fields = ["access_key", "secret_key", "aws_region", "cluster_name"]
        for field in required_fields:
            if not cleaned.get(field):
                self.add_error(field, "This field is required.")

        value = cleaned.get("services")
        if value in (None, ""):
            cleaned["services"] = []
        elif isinstance(value, str):
            try:
                cleaned["services"] = json.loads(value)
            except json.JSONDecodeError:
                self.add_error("services", "Expected a JSON array.")
        elif not isinstance(value, list):
            self.add_error("services", "Provide a JSON array.")

        defaults = {
            "aws_credentials_mode": os.environ.get("ECS_MANIFEST_AWS_CREDENTIAL_MODE"),
            "gemini_model": os.environ.get("ECS_MANIFEST_GEMINI_MODEL"),
            "gemini_fallbacks": os.environ.get("ECS_MANIFEST_GEMINI_FALLBACKS"),
            "gemini_api_key": os.environ.get("ECS_MANIFEST_GEMINI_API_KEY_OVERRIDE"),
        }
        if not cleaned.get("aws_credentials_mode"):
            cleaned["aws_credentials_mode"] = defaults["aws_credentials_mode"] or "auto"
        if not cleaned.get("gemini_model") and defaults["gemini_model"]:
            cleaned["gemini_model"] = defaults["gemini_model"]
        if not cleaned.get("gemini_fallbacks") and defaults["gemini_fallbacks"]:
            cleaned["gemini_fallbacks"] = defaults["gemini_fallbacks"]
        if not cleaned.get("gemini_api_key") and defaults["gemini_api_key"]:
            cleaned["gemini_api_key"] = defaults["gemini_api_key"]

        return cleaned


class EksManifestForm(AutomationTaskForm):
    access_key = forms.CharField(label="AWS Access Key ID")
    secret_key = forms.CharField(widget=forms.PasswordInput, label="AWS Secret Access Key")
    aws_region = forms.CharField(label="AWS Region")
    cluster_name = forms.CharField(label="EKS Cluster Name")
    namespaces = forms.JSONField(required=False, label="Namespaces", help_text="Optional JSON array of namespaces to export.")
    resource_types = forms.CharField(
        required=False,
        label="Resource types",
        help_text="Comma-separated kubectl resource types. Leave blank to auto-discover.",
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["task_id"].initial = self.initial.get("task_id") or "eks_manifests"

    def clean(self):
        cleaned = super().clean()
        required_fields = ["access_key", "secret_key", "aws_region", "cluster_name"]
        for field in required_fields:
            if not cleaned.get(field):
                self.add_error(field, "This field is required.")

        namespaces = cleaned.get("namespaces")
        if namespaces in (None, ""):
            cleaned["namespaces"] = []
        elif isinstance(namespaces, str):
            try:
                decoded = json.loads(namespaces)
            except json.JSONDecodeError:
                self.add_error("namespaces", "Expected a JSON array.")
            else:
                if isinstance(decoded, list):
                    cleaned["namespaces"] = [str(entry).strip() for entry in decoded if str(entry).strip()]
                else:
                    self.add_error("namespaces", "Provide a JSON array.")
        elif isinstance(namespaces, list):
            cleaned["namespaces"] = [str(entry).strip() for entry in namespaces if str(entry).strip()]
        else:
            self.add_error("namespaces", "Provide a JSON array.")

        return cleaned


class BoxProjectForm(AutomationTaskForm):
    cloud_provider = forms.ChoiceField(
        choices=(("aws", "AWS"), ("gcp", "GCP")),
        label="Cloud Provider",
    )
    aws_region = forms.CharField(required=False, label="AWS Region", initial="ap-south-1")
    gcp_project = forms.CharField(required=False, label="GCP Project ID")
    gcp_region = forms.CharField(required=False, label="GCP Region", initial="us-central1")
    services = forms.JSONField(label="Selected Services")
    service_inputs = forms.JSONField(required=False, label="Service Inputs")

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["task_id"].initial = self.initial.get("task_id") or "box_project"

    def clean(self):
        cleaned = super().clean()
        provider = (cleaned.get("cloud_provider") or "").lower()
        if provider not in {"aws", "gcp"}:
            self.add_error("cloud_provider", "Select either AWS or GCP.")
        if provider == "aws":
            if not cleaned.get("aws_region"):
                self.add_error("aws_region", "AWS region is required.")
            cleaned["gcp_project"] = ""
            cleaned["gcp_region"] = ""
        elif provider == "gcp":
            if not cleaned.get("gcp_project"):
                self.add_error("gcp_project", "GCP project ID is required.")
            if not cleaned.get("gcp_region"):
                self.add_error("gcp_region", "GCP region is required.")
        services = cleaned.get("services")
        if isinstance(services, str):
            try:
                services = json.loads(services)
            except json.JSONDecodeError:
                self.add_error("services", "Services must be a JSON array.")
                services = []
        if not isinstance(services, list) or not services:
            self.add_error("services", "Select at least one service.")
            services = []
        cleaned["services"] = [str(service).strip() for service in services if str(service).strip()]
        inputs = cleaned.get("service_inputs")
        if not inputs:
            cleaned["service_inputs"] = {}
        elif isinstance(inputs, str):
            try:
                data = json.loads(inputs)
            except json.JSONDecodeError:
                self.add_error("service_inputs", "Service inputs must be a JSON object.")
            else:
                if not isinstance(data, dict):
                    self.add_error("service_inputs", "Service inputs must be an object.")
                else:
                    cleaned["service_inputs"] = data
        elif not isinstance(inputs, dict):
            self.add_error("service_inputs", "Service inputs must be an object.")

        return cleaned
