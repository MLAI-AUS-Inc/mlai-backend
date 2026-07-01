from __future__ import annotations

from django.core.management.base import BaseCommand

from integrations.models import (
    ExternalServiceConnection,
    GoogleConnection,
    UserIntegration,
)

# (model, [EncryptedTextField names]) -- every encrypted secret in integrations.
# Keep in sync with integrations.models.
_TARGETS = [
    (GoogleConnection, ["refresh_token"]),
    (ExternalServiceConnection, ["access_token", "refresh_token"]),
    (UserIntegration, ["github_access_token", "github_refresh_token"]),
]


class Command(BaseCommand):
    help = (
        "Re-encrypt EncryptedTextField secrets (connector OAuth tokens) under the "
        "current primary field-encryption key. Reads decrypt via the legacy "
        "fallback keys; re-saving writes them back under the primary key, so "
        "historical rows stop depending on the old (previously committed) key.\n\n"
        "Run this after setting FIELD_ENCRYPTION_KEY and/or rotating SECRET_KEY. "
        "Dry-run by default; pass --apply to write."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--apply",
            action="store_true",
            help="Persist the re-encryption (default is a dry run that only counts rows).",
        )

    def handle(self, *args, **options):
        apply = options["apply"]
        total = 0
        for model, encrypted_fields in _TARGETS:
            touched = 0
            for obj in model.objects.all().iterator():
                # from_db_value already decrypted the in-memory values; only
                # rows that actually hold a secret need re-writing.
                if not any((getattr(obj, name) or "") for name in encrypted_fields):
                    continue
                touched += 1
                if apply:
                    # save(update_fields=...) re-runs get_prep_value -> encrypts
                    # with the primary key. updated_at is intentionally excluded
                    # so a maintenance re-encrypt does not churn timestamps.
                    obj.save(update_fields=list(encrypted_fields))
            verb = "re-encrypted" if apply else "would re-encrypt"
            self.stdout.write(f"{model.__name__}: {verb} {touched} row(s)")
            total += touched

        mode = "applied" if apply else "dry run (use --apply to write)"
        self.stdout.write(
            self.style.SUCCESS(f"reencrypt_secrets {mode}; {total} row(s) with secrets")
        )
