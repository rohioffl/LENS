from django.apps import AppConfig


class InventoryConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'inventory'

    def ready(self):
        # Import task modules to ensure they register with the automation registry.
        import importlib

        for module in (
            "inventory.services.aws_task",
            "inventory.services.terraform_task",
            "inventory.services.classic_vpn_task",
            "inventory.services.ecr_migration_task",
            "inventory.services.ha_vpn_task",
        ):
            importlib.import_module(module)
