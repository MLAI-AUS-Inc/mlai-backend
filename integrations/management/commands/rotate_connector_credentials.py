from django.apps import apps
from django.core.management.base import BaseCommand
from django.db import transaction

from integrations.fields import EncryptedTextField


class Command(BaseCommand):
    help = "Re-encrypt every connector credential with the configured active key ID."

    def add_arguments(self, parser):
        parser.add_argument("--dry-run", action="store_true")

    def handle(self, *args, **options):
        dry_run = bool(options["dry_run"])
        rotated = 0
        with transaction.atomic():
            for model in apps.get_models():
                encrypted_fields = [
                    field
                    for field in model._meta.local_fields
                    if isinstance(field, EncryptedTextField)
                ]
                if not encrypted_fields:
                    continue
                field_names = [field.attname for field in encrypted_fields]
                for instance in model.objects.only(
                    model._meta.pk.attname,
                    *field_names,
                ).iterator(chunk_size=200):
                    populated = [name for name in field_names if getattr(instance, name)]
                    if not populated:
                        continue
                    rotated += len(populated)
                    if not dry_run:
                        instance.save(update_fields=populated)
            if dry_run:
                transaction.set_rollback(True)
        action = "Would rotate" if dry_run else "Rotated"
        self.stdout.write(
            self.style.SUCCESS(
                f"{action} {rotated} connector credential value(s)"
            )
        )
