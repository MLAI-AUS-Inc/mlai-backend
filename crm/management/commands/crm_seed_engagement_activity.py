"""Preserve dated import history before switching to live daily refresh."""

import csv
import hashlib
import json
from collections import Counter
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from community_chat.admin_access import require_mlai_admin
from core.models import User
from crm.models import Contact, CrmAuditEvent


def read_seed_files(registrations, slack, *, registration_sha, slack_sha):
    """Validate source checksums and preserve only dated, minimal activity metadata."""

    paths = [(Path(registrations), registration_sha), (Path(slack), slack_sha)]
    for path, expected in paths:
        if hashlib.sha256(path.read_bytes()).hexdigest() != expected:
            raise ValueError("Activity source checksum mismatch.")
    events = {}
    with paths[0][0].open(encoding="utf-8-sig", newline="") as handle:
        for row in csv.DictReader(handle):
            if row["approval_status"] != "approved":
                continue
            email = row["email_normalized"]
            event = {
                "source": "luma", "kind": "checkin" if row["checked_in_at"] else "registration",
                "event_id": f"luma:{row['event_id']}", "occurred_at": row["event_start_at"],
                "human_verified": bool(row["checked_in_at"]),
            }
            events.setdefault(email, {})[("luma", event["event_id"])] = event
    with paths[1][0].open(encoding="utf-8-sig", newline="") as handle:
        for row in csv.DictReader(handle):
            event = {
                "source": "slack", "kind": "slack_message",
                "event_id": f"slack-day:{row['active_date_melbourne']}",
                "occurred_at": row["active_date_melbourne"], "human_verified": True,
            }
            events.setdefault(row["email"], {})[("slack", event["event_id"])] = event
    return {email: list(values.values()) for email, values in events.items()}


class Command(BaseCommand):
    help = "Seed minimal dated event/Slack import history. No consent or send operations. Defaults to rollback."

    def add_arguments(self, parser):
        parser.add_argument("--registrations", required=True)
        parser.add_argument("--slack", required=True)
        parser.add_argument("--registration-sha256", required=True)
        parser.add_argument("--slack-sha256", required=True)
        parser.add_argument("--actor-email", required=True)
        parser.add_argument("--apply", action="store_true")

    def handle(self, *args, **options):
        actor = User.objects.filter(email__iexact=options["actor_email"].strip()).first()
        require_mlai_admin(actor)
        try:
            evidence = read_seed_files(
                options["registrations"], options["slack"],
                registration_sha=options["registration_sha256"], slack_sha=options["slack_sha256"],
            )
        except (ValueError, OSError, KeyError) as exc:
            raise CommandError(str(exc)) from exc
        counts = Counter()
        with transaction.atomic():
            from core.models import AccountBan
            from django.db.models.functions import Lower
            banned = set(AccountBan.objects.filter(revoked_at__isnull=True).annotate(
                email_key=Lower("email"),
            ).values_list("email_key", flat=True))
            emails = list(evidence)
            for start in range(0, len(emails), 500):
                contacts = list(Contact.objects.select_for_update().filter(email__in=emails[start:start + 500]))
                changed = []
                for contact in contacts:
                    if contact.email in banned:
                        counts["banned_skipped"] += 1
                        continue
                    payload = {
                        "version": "source-events-v1", "as_of": "2026-11-02",
                        "events": evidence[contact.email],
                        "source_sha256": {"registrations": options["registration_sha256"], "slack": options["slack_sha256"]},
                    }
                    attributes = dict(contact.attributes or {})
                    previous = attributes.get("engagement_activity")
                    if previous and previous != payload:
                        counts["existing_seed_preserved"] += 1
                    elif previous == payload:
                        counts["unchanged"] += 1
                    else:
                        attributes["engagement_activity"] = payload
                        contact.attributes = attributes
                        changed.append(contact)
                        counts["seeded"] += 1
                        counts["observations"] += len(payload["events"])
                Contact.objects.bulk_update(changed, ["attributes"], batch_size=250)
            if not options["apply"]:
                transaction.set_rollback(True)
            else:
                CrmAuditEvent.objects.create(
                    actor=actor, action=CrmAuditEvent.Action.IMPORT,
                    target_type="engagement_activity_seed", target_id="2026-11-02",
                    detail={"counts": dict(counts), "registration_sha256": options["registration_sha256"], "slack_sha256": options["slack_sha256"]},
                )
        self.stdout.write(json.dumps({"dry_run": not options["apply"], "counts": dict(counts)}, sort_keys=True))
