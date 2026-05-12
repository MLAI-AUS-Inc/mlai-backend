from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from integrations.models import ExternalServiceProvider
from integrations.services.external_connectors import _provider_configuration_error, _xero_oauth_scope_list
from integrations.services.xero_scopes import XERO_REQUIRED_REPORT_SCOPES, xero_can_request_report_scopes


class Command(BaseCommand):
    help = "Validate Xero OAuth configuration without printing secret values."
    requires_system_checks = []

    def handle(self, *args, **options):
        configuration_error = _provider_configuration_error(ExternalServiceProvider.XERO)
        if configuration_error:
            raise CommandError(configuration_error)

        self.stdout.write(self.style.SUCCESS("Xero OAuth is configured."))
        self.stdout.write(f"Redirect URI: {settings.XERO_OAUTH_REDIRECT_URI}")
        scopes = _xero_oauth_scope_list()
        self.stdout.write(f"Scopes: {', '.join(scopes)}")
        if not xero_can_request_report_scopes(scopes):
            self.stdout.write(
                self.style.WARNING(
                    "Report metrics are disabled. To enable them later, configure report scopes: "
                    f"{', '.join(XERO_REQUIRED_REPORT_SCOPES)}"
                )
            )
        self.stdout.write("Client ID: present")
        self.stdout.write("Client secret: present")
