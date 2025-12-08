from django.apps import AppConfig


class InventoryConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'inventory'

    def ready(self):
        from importlib import import_module

        # Always use the canonical package path for this app to avoid environment-specific prefixes.
        for tail in (
            "aws_task",
            "terraform_task",
            "classic_vpn_task",
            "ecr_migration_task",
            "ha_vpn_task",
        ):
            import_module(f"inventory.services.{tail}")
