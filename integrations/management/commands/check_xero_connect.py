from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from integrations.models import ExternalServiceProvider
from integrations.services.external_connectors import _provider_configuration_error, _xero_oauth_scope_list


class Command(BaseCommand):
    help = "Validate Xero OAuth configuration without printing secret values."
    requires_system_checks = []

    def handle(self, *args, **options):
        configuration_error = _provider_configuration_error(ExternalServiceProvider.XERO)
        if configuration_error:
            raise CommandError(configuration_error)

        self.stdout.write(self.style.SUCCESS("Xero OAuth is configured."))
        self.stdout.write(f"Redirect URI: {settings.XERO_OAUTH_REDIRECT_URI}")
        self.stdout.write(f"Scopes: {', '.join(_xero_oauth_scope_list())}")
        self.stdout.write("Client ID: present")
        self.stdout.write("Client secret: present")
