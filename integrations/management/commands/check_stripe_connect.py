from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from integrations.models import ExternalServiceProvider
from integrations.services.external_connectors import _provider_configuration_error


class Command(BaseCommand):
    help = "Validate Stripe Connect OAuth configuration without printing secret values."
    requires_system_checks = []

    def handle(self, *args, **options):
        configuration_error = _provider_configuration_error(ExternalServiceProvider.STRIPE)
        if configuration_error:
            raise CommandError(configuration_error)

        self.stdout.write(self.style.SUCCESS("Stripe Connect OAuth is configured."))
        self.stdout.write(f"Redirect URI: {settings.STRIPE_OAUTH_REDIRECT_URI}")
        self.stdout.write(f"Scopes: {', '.join(settings.STRIPE_OAUTH_SCOPES)}")
        self.stdout.write("Client ID: present")
        key_source = (
            "STRIPE_VIBE_RAISING_KEY"
            if str(getattr(settings, "STRIPE_VIBE_RAISING_KEY", "") or "").strip()
            else "STRIPE_SECRET_KEY"
        )
        self.stdout.write(f"Secret key: present (from {key_source})")
