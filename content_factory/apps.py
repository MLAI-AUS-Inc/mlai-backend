from django.apps import AppConfig


class ContentFactoryConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'content_factory'
    verbose_name = 'Content Factory'

    def ready(self):
        from . import checks  # noqa: F401
