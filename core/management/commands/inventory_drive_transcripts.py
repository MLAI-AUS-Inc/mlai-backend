from __future__ import annotations

import json
import os
import stat
from datetime import date
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError

from integrations.models import (
    ExternalServiceConnection,
    ExternalServiceConnectionStatus,
    ExternalServiceProvider,
)
from org_memory.drive_inventory import (
    DEFAULT_ALLOWED_MIME_TYPES,
    DriveInventoryError,
    DriveInventoryLimits,
    GoogleDriveMetadataClient,
    build_drive_service,
    inventory_drive_metadata,
    validate_drive_id,
)
from org_memory.governance import (
    GovernancePolicyError,
    assert_provider_inventory_allowed,
)


class Command(BaseCommand):
    help = "Create an approval-gated, metadata-only inventory of selected Drive transcript folders."

    def add_arguments(self, parser):
        parser.add_argument("--connection-id", required=True)
        parser.add_argument(
            "--folder-id",
            action="append",
            required=True,
            help="Approved Google Drive folder ID. Repeat for multiple roots.",
        )
        parser.add_argument(
            "--modified-after",
            required=True,
            help="Australia/Sydney historical cutoff using YYYY-MM-DD.",
        )
        parser.add_argument("--output", required=True, help="New absolute JSON output path.")
        parser.add_argument(
            "--allowed-mime-type",
            action="append",
            default=None,
            help="Allowed transcript MIME type. Repeat to override defaults.",
        )
        parser.add_argument("--max-files", type=int, default=10_000)
        parser.add_argument("--max-pages", type=int, default=1_000)
        parser.add_argument("--max-seconds", type=int, default=300)
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Required safety acknowledgement; the command never downloads file bodies.",
        )

    def handle(self, *args, **options):
        if not options.get("dry_run"):
            raise CommandError("--dry-run is required; content ingestion is not implemented in this step.")

        output_path = Path(str(options["output"])).expanduser()
        if not output_path.is_absolute():
            raise CommandError("--output must be an absolute path.")
        if output_path.exists():
            raise CommandError(f"Refusing to overwrite existing output: {output_path}")
        if not output_path.parent.is_dir():
            raise CommandError(f"Output directory does not exist: {output_path.parent}")

        try:
            modified_after = date.fromisoformat(str(options["modified_after"]))
        except ValueError as exc:
            raise CommandError("--modified-after must use YYYY-MM-DD.") from exc

        try:
            folder_ids = sorted({validate_drive_id(value) for value in options["folder_id"]})
            limits = DriveInventoryLimits(
                max_files=options["max_files"],
                max_pages=options["max_pages"],
                max_seconds=options["max_seconds"],
            )
            limits.validate()
        except DriveInventoryError as exc:
            raise CommandError(str(exc)) from exc

        try:
            connection = (
                ExternalServiceConnection.objects.select_related("organization")
                .filter(pk=options["connection_id"])
                .first()
            )
        except (TypeError, ValueError) as exc:
            raise CommandError("--connection-id is invalid.") from exc
        if connection is None:
            raise CommandError("Google Drive connection was not found.")
        if connection.provider != ExternalServiceProvider.GOOGLE_DRIVE:
            raise CommandError("Selected connection is not a Google Drive connection.")
        if not connection.organization_id:
            raise CommandError("Google Drive connection must be bound to an organisation.")
        if connection.status != ExternalServiceConnectionStatus.CONNECTED:
            raise CommandError("Google Drive connection must be connected before inventory.")

        selectors = {
            f"connection:{connection.pk}",
            f"organization:{connection.organization_id}",
            *(f"folder:{folder_id}" for folder_id in folder_ids),
        }
        try:
            assert_provider_inventory_allowed(
                ExternalServiceProvider.GOOGLE_DRIVE,
                selectors,
                requested_max_files=limits.max_files,
            )
            service = build_drive_service(connection)
            result = inventory_drive_metadata(
                GoogleDriveMetadataClient(service),
                organization_id=str(connection.organization_id),
                connection_id=str(connection.pk),
                folder_ids=folder_ids,
                modified_after=modified_after,
                allowed_mime_types=options.get("allowed_mime_type") or DEFAULT_ALLOWED_MIME_TYPES,
                limits=limits,
            )
        except (DriveInventoryError, GovernancePolicyError) as exc:
            raise CommandError(str(exc)) from exc

        try:
            output_fd = os.open(output_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(output_fd, "w", encoding="utf-8") as output_file:
                json.dump(result, output_file, indent=2, sort_keys=True)
                output_file.write("\n")
                output_file.flush()
                os.fsync(output_file.fileno())
        except FileExistsError as exc:
            raise CommandError(f"Refusing to overwrite existing output: {output_path}") from exc
        except OSError as exc:
            raise CommandError(f"Unable to write inventory output: {exc}") from exc

        if stat.S_IMODE(output_path.stat().st_mode) != 0o600:
            output_path.chmod(0o600)

        counts = result["counts"]
        self.stdout.write(
            self.style.SUCCESS(
                f"Drive inventory {result['inventory_id']} wrote {output_path}: "
                f"{counts['candidate_transcripts']} candidates, "
                f"{counts['duplicates']} duplicates, partial={result['partial']}."
            )
        )
