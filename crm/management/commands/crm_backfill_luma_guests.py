import json
import time
from datetime import datetime, time as day_time
from zoneinfo import ZoneInfo

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from crm.luma_sync import build_luma_service, sync_event_catalog, sync_event_guests
from crm.models import CrmAuditEvent, CrmEvent
from integrations.services.luma import LumaAPIError, LumaConfigurationError

RATE_LIMIT_PAUSE_SECONDS = 60
RATE_LIMIT_RETRIES = 3


class Command(BaseCommand):
    help = (
        "Copy guests for past and future MLAI Luma events into crm contacts. "
        "Resumable: events already synced are skipped unless --force is given."
    )

    def add_arguments(self, parser):
        parser.add_argument("--since", help="Only events starting on or after YYYY-MM-DD (Melbourne).")
        parser.add_argument("--event-id", action="append", default=[], help="Limit to this Luma event id; repeatable.")
        parser.add_argument("--limit", type=int, help="Stop after this many events.")
        parser.add_argument("--force", action="store_true", help="Re-read events that were already synced.")

    def handle(self, *args, **options):
        try:
            service = build_luma_service()
        except LumaConfigurationError as exc:
            raise CommandError(str(exc)) from exc

        catalog = sync_event_catalog(service)
        events = CrmEvent.objects.filter(pk__in=[event.pk for event in catalog])
        if options["since"]:
            try:
                since = datetime.combine(
                    datetime.strptime(options["since"], "%Y-%m-%d").date(),
                    day_time.min,
                    tzinfo=ZoneInfo("Australia/Melbourne"),
                )
            except ValueError as exc:
                raise CommandError("--since must use YYYY-MM-DD format.") from exc
            events = events.filter(start_at__gte=since)
        if options["event_id"]:
            events = events.filter(luma_event_id__in=options["event_id"])
        if not options["force"]:
            events = events.filter(guests_synced_at__isnull=True)
        events = list(events.order_by("start_at"))
        if options["limit"]:
            events = events[: options["limit"]]

        self.stdout.write(f"{len(catalog)} events in the calendar; {len(events)} to sync.")
        totals = {"events_synced": 0, "guests": 0, "contacts_created": 0, "consent_granted": 0}
        failed = []
        for index, event in enumerate(events, start=1):
            result = self._sync_with_retries(service, event)
            if result is None:
                failed.append(event.luma_event_id)
                continue
            totals["events_synced"] += 1
            for key in ("guests", "contacts_created", "consent_granted"):
                totals[key] += result[key]
            self.stdout.write(
                f"[{index}/{len(events)}] {event.luma_event_id} {event.start_at:%Y-%m-%d} "
                f"guests={result['guests']} new_contacts={result['contacts_created']}"
                if event.start_at
                else f"[{index}/{len(events)}] {event.luma_event_id} guests={result['guests']}"
            )

        summary = {**totals, "failed_events": failed}
        CrmAuditEvent.objects.create(
            action=CrmAuditEvent.Action.SYNC, target_type="luma_backfill", detail=summary
        )
        self.stdout.write(json.dumps(summary, sort_keys=True))
        if failed:
            raise CommandError(f"{len(failed)} event(s) failed; re-run to retry them.")

    def _sync_with_retries(self, service, event):
        for attempt in range(RATE_LIMIT_RETRIES + 1):
            try:
                return sync_event_guests(
                    service, event, consent_labels=settings.CRM_LUMA_CONSENT_QUESTION_LABELS
                )
            except LumaAPIError as exc:
                if exc.status_code in (401, 403):
                    raise CommandError("Luma rejected LUMA_API_KEY.") from exc
                if exc.status_code == 429 and attempt < RATE_LIMIT_RETRIES:
                    self.stdout.write(f"Rate limited; pausing {RATE_LIMIT_PAUSE_SECONDS}s.")
                    time.sleep(RATE_LIMIT_PAUSE_SECONDS)
                    continue
                self.stderr.write(f"{event.luma_event_id}: {exc} (status {exc.status_code})")
                return None
        return None
