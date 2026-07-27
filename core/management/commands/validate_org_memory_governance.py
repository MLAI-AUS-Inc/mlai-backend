from __future__ import annotations

import os
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError

from org_memory.governance import (
    DEFAULT_POLICY_PATH,
    configured_enabled_providers,
    load_policy_manifest,
    parse_enabled_providers,
    validate_policy_manifest,
)


class Command(BaseCommand):
    help = "Validate the fail-closed organisational-memory provider governance manifest."

    def add_arguments(self, parser):
        parser.add_argument(
            "--manifest",
            default=str(DEFAULT_POLICY_PATH),
            help="Path to the provider policy JSON manifest.",
        )
        parser.add_argument(
            "--environment",
            choices=("local", "development", "test", "production"),
            default=None,
            help="Deployment environment; defaults to APP_ENV.",
        )
        parser.add_argument(
            "--enabled-provider",
            action="append",
            default=[],
            help="Provider requested for ingestion. May be repeated.",
        )

    def handle(self, *args, **options):
        environment = (
            options.get("environment") or os.getenv("APP_ENV", "local") or "local"
        ).strip().lower()
        production = environment == "production"
        enabled = configured_enabled_providers() | parse_enabled_providers(
            options.get("enabled_provider")
        )
        manifest_path = Path(options["manifest"])

        try:
            manifest = load_policy_manifest(manifest_path)
        except Exception as exc:
            raise CommandError(str(exc)) from exc

        errors = validate_policy_manifest(
            manifest,
            enabled_providers=enabled,
            production=production,
        )
        if errors:
            details = "\n - ".join(errors)
            raise CommandError(f"Organisational-memory governance is invalid:\n - {details}")

        enabled_label = ", ".join(sorted(enabled)) if enabled else "none"
        self.stdout.write(
            self.style.SUCCESS(
                "Organisational-memory governance is valid "
                f"for {environment}; requested providers: {enabled_label}."
            )
        )
