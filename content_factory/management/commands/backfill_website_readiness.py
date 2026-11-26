"""Backfill the website readiness record from saved state (dry run unless --apply)."""

import json

from django.core.management.base import BaseCommand

from content_factory import website_readiness


class Command(BaseCommand):
    """Derive every website's readiness; report counts and every state that differs from today's answer."""

    help = "Derive WebsiteReadiness for each website (read-only by default). --apply writes rows and a 'backfill' check event."

    def add_arguments(self, parser):
        """Accept --apply, an optional exact domain and a row limit."""
        parser.add_argument("--apply", action="store_true", help="Write the records (default: dry run).")
        parser.add_argument("--domain", help="Only this organisation domain.")
        parser.add_argument("--limit", type=int, default=0, help="At most this many websites (0 = all).")

    def handle(self, *args, **options):
        """Print one JSON summary; with --apply also store each record."""
        summary = backfill(apply=options["apply"], domain=str(options.get("domain") or "").strip().lower() or None,
                           limit=max(0, options["limit"] or 0))
        self.stdout.write(json.dumps(summary, sort_keys=True))


def _configs(domain):
    from content_factory.models import OrganizationContentConfig
    rows = (OrganizationContentConfig.objects.filter(website_connection__isnull=False)
            .select_related("website_connection__organization").order_by("pk"))
    return rows.filter(organization__domain=domain) if domain else rows


def backfill(*, apply=False, domain=None, limit=0):
    """Derive (and with ``apply`` record) each bound website; returns counts and differences."""
    counts = {state: 0 for state in website_readiness.STATES}
    differs, failed, checked = [], [], 0
    rows = _configs(domain)
    for config in (rows[:limit] if limit else rows):
        connection = config.website_connection
        try:
            derived = website_readiness.derive(config, connection)
            if apply:
                website_readiness.record(connection, trigger="backfill", detail={"source": "backfill_website_readiness"})
        except Exception as exc:  # noqa: BLE001 - one bad row must not hide the rest
            failed.append({"connectionId": str(connection.pk), "error": type(exc).__name__})
            continue
        checked += 1
        counts[derived["state"]] += 1
        legacy = derived["legacy"]["state"]
        if legacy != derived["state"]:
            differs.append({"connectionId": str(connection.pk), "domain": connection.organization.domain,
                            "state": derived["state"], "legacy": legacy, "reasonCode": derived["reason_code"],
                            "legacyReasonCode": derived["legacy"].get("reasonCode") or ""})
    return {"applied": bool(apply), "checked": checked, "counts": counts, "differs": differs, "failed": failed}
