from django.apps import AppConfig


class JobsConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "jobs"
    verbose_name = "Roo Jobs Daily"

    def ready(self):
        from . import checks  # noqa: F401
