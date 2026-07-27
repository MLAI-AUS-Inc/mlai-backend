from django.apps import AppConfig


class OrgMemoryConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "org_memory"
    verbose_name = "Organisational Memory"

    def ready(self):
        from . import signals  # noqa: F401
