from django.apps import AppConfig

from .env import load_local_env

load_local_env()


class InventoryConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'inventory'

    def ready(self):
        # Import tasks directly from the local services package to avoid path ambiguity on different machines.
        from .services import (
            aws_task,  # noqa: F401
            terraform_task,  # noqa: F401
            ecs_terraform_task,  # noqa: F401
            eks_terraform_task,  # noqa: F401
            ecs_manifest_task,  # noqa: F401
            eks_manifest_task,  # noqa: F401
            vm2gke_manifest_task,  # noqa: F401
            classic_vpn_task,  # noqa: F401
            ecr_migration_task,  # noqa: F401
            ha_vpn_task,  # noqa: F401
            aws_security_audit_task,  # noqa: F401
            gcp_security_audit_task,  # noqa: F401
            tco_task,  # noqa: F401
        )
