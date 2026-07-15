from django.apps import AppConfig


class ContentAnalyticsConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "content_analytics"
    verbose_name = "Content Analytics"

    def ready(self):
        from content_analytics import signals  # noqa: F401
