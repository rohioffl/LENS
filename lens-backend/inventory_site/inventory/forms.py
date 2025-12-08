import base64
import json

from django import forms

from inventory.services.aws_inventory import RESOURCE_MAP


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

    aws_subnets = forms.JSONField(required=False, label="Selected AWS Subnets")
    gcp_subnets = forms.JSONField(required=False, label="Selected GCP Subnets")

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
        for list_field in ("aws_subnets", "gcp_subnets"):
            value = cleaned.get(list_field) or []
            if isinstance(value, str):
                try:
                    value = json.loads(value)
                except json.JSONDecodeError:
                    self.add_error(list_field, "Expected a JSON array of subnet identifiers.")
                    continue
            if value and not isinstance(value, list):
                self.add_error(list_field, "Provide a JSON array of subnet identifiers.")
                continue
            cleaned[list_field] = value
        return cleaned
