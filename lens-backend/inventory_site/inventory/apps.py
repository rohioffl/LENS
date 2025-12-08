from django.apps import AppConfig


class InventoryConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'inventory'

    def ready(self):
        # Import tasks directly from the local services package to avoid path ambiguity on different machines.
        from .services import (
            aws_task,  # noqa: F401
            terraform_task,  # noqa: F401
            classic_vpn_task,  # noqa: F401
            ecr_migration_task,  # noqa: F401
            ha_vpn_task,  # noqa: F401
        )
