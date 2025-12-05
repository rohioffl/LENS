from django.apps import AppConfig


class InventoryConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'inventory'

    def ready(self):
        from .services import aws_task, terraform_task, classic_vpn_task, ecr_migration_task  # noqa: F401
