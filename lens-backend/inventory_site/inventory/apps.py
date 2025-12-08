from django.apps import AppConfig


class InventoryConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'inventory'

    def ready(self):
        import importlib

        def _import_task(tail: str):
            # Try both the app-relative name and an explicit package path.
            candidates = [
                f"{self.name}.services.{tail}",
                f"inventory_site.inventory.services.{tail}",
            ]
            last_error = None
            for candidate in candidates:
                try:
                    return importlib.import_module(candidate)
                except ModuleNotFoundError as exc:
                    last_error = exc
                    continue
            # If all candidates fail, re-raise the last error for visibility.
            if last_error:
                raise last_error

        for tail in (
            "aws_task",
            "terraform_task",
            "classic_vpn_task",
            "ecr_migration_task",
            "ha_vpn_task",
        ):
            _import_task(tail)
