from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from integrations.models import ExternalServiceProvider
from integrations.services.external_connectors import _provider_configuration_error


class Command(BaseCommand):
    help = "Validate Basiq Bank Feed configuration without printing secret values."
    requires_system_checks = []

    def handle(self, *args, **options):
        configuration_error = _provider_configuration_error(ExternalServiceProvider.BANK_FEED)
        if configuration_error:
            raise CommandError(configuration_error)

        self.stdout.write(self.style.SUCCESS("Basiq Bank Feed is configured."))
        self.stdout.write(f"API base URL: {settings.BASIQ_API_BASE_URL}")
        self.stdout.write(f"Consent UI URL: {settings.BASIQ_CONSENT_UI_URL}")
        self.stdout.write(f"API version: {settings.BASIQ_API_VERSION}")
        self.stdout.write("API key: present")
