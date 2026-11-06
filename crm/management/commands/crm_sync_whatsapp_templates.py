import json
from collections import Counter

from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from crm.models import Channel, CrmAuditEvent, MessageTemplate
from crm.twilio import TwilioContentError, list_content_templates, parse_content_template
from integrations import http_client


def inferred_variables(indexes):
    return [{"index": index, "source": "custom", "label": f"Variable {index}"} for index in indexes]


def merged_variables(existing, indexes):
    """Keep every existing slot mapping and add any slot it lacks as custom."""

    kept = [slot for slot in existing or [] if isinstance(slot, dict)]
    known = {str(slot.get("index")) for slot in kept}
    added = [index for index in indexes if str(index) not in known]
    return kept + inferred_variables(added)


def sync_templates(templates, *, now):
    """Upsert templates by Content SID. ``enabled`` is never changed here."""

    counts = Counter()
    for template in templates:
        row = MessageTemplate.objects.filter(content_sid=template.sid).first()
        if row is None:
            MessageTemplate.objects.create(
                channel=Channel.WHATSAPP,
                name=template.name,
                content_sid=template.sid,
                category=template.category or MessageTemplate.Category.UTILITY,
                body=template.body,
                variables=inferred_variables(template.variable_indexes),
                approved=template.approved,
                last_synced_at=now,
            )
            counts["created"] += 1
            continue
        row.name = template.name
        row.body = template.body
        if template.category:
            row.category = template.category
        row.approved = template.approved
        row.variables = merged_variables(row.variables, template.variable_indexes)
        row.last_synced_at = now
        row.save(
            update_fields=["name", "body", "category", "approved", "variables", "last_synced_at", "updated_at"]
        )
        counts["updated"] += 1
    return {"created": counts["created"], "updated": counts["updated"]}


class Command(BaseCommand):
    help = (
        "Refresh WhatsApp templates from Twilio's Content API (ContentAndApprovals). "
        "Updates wording, category and approval; a person enables templates in admin."
    )

    def handle(self, *args, **options):
        try:
            contents = list_content_templates()
        except TwilioContentError as exc:
            if str(exc) == "not_configured":
                raise CommandError("Set TWILIO_ACCOUNT_SID and TWILIO_AUTH_TOKEN first.") from exc
            raise CommandError(f"Twilio's Content API could not be read ({exc}).") from exc
        except http_client.RequestException as exc:
            raise CommandError(
                f"Twilio's Content API could not be reached ({exc.__class__.__name__})."
            ) from exc

        templates = [template for template in map(parse_content_template, contents) if template]
        summary = {
            **sync_templates(templates, now=timezone.now()),
            "approved": sum(1 for template in templates if template.approved),
        }
        CrmAuditEvent.objects.create(
            action=CrmAuditEvent.Action.SYNC, target_type="whatsapp_templates", detail=summary
        )
        self.stdout.write(json.dumps(summary, sort_keys=True))
