"""Bounded, preview-first projection of explicitly mapped, already-ingested audience data."""

import json
from collections import Counter
from pathlib import Path
import uuid

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone

from community_chat.admin_access import require_mlai_admin
from crm.audience_profiles import AudienceProfileError, parse_instant, with_profile
from crm.audience_sources import (
    activity_projection, merge_activity, project_records, retract_mapping_sources, validate_mapping,
)
from crm.engagement import RESERVED_ATTRIBUTE_KEYS
from crm.models import Contact, CrmAuditEvent


def mapped_contact_records(contact, mappings, *, observed_at=None):
    """Read only exact fields/questions; absence and removed registrations are retractable."""

    records = []
    for mapping in mappings:
        key, kind = mapping["source_key"], mapping["source_kind"]
        prefix = f"mapping:{mapping['id']}:"
        if kind == "luma_answer":
            for registration in contact.registrations.all():
                if registration.removed_at is not None:
                    continue
                for answer in registration.registration_answers or []:
                    if not isinstance(answer, dict) or answer.get("question_id") != key:
                        continue
                    if registration.registered_at is None:
                        continue
                    records.append({"mapping_id": mapping["id"],
                        "source_record_id": f"{prefix}registration:{registration.pk}:{key}",
                        "observed_at": registration.registered_at.isoformat(), "value": answer.get("value")})
        elif kind == "csv":
            if observed_at is None:
                raise CommandError("CSV mappings require --observed-at naming the original source observation time.")
            if key.startswith("attributes."):
                attribute = key.removeprefix("attributes.")
                if attribute in RESERVED_ATTRIBUTE_KEYS or "." in attribute or "__" in attribute:
                    raise CommandError("CSV mappings cannot read protected or nested attributes.")
                value = (contact.attributes or {}).get(attribute)
            elif key in ("city", "job_title", "company"):
                value = getattr(contact, key)
            else:
                raise CommandError("CSV source_key must be city, job_title, company or an ordinary attributes key.")
            records.append({"mapping_id": mapping["id"], "source_record_id": f"{prefix}contact:{contact.pk}:{key}",
                            "observed_at": observed_at, "value": value})
        elif kind == "community_profile":
            if key not in ("city", "interests"):
                raise CommandError("Community profile mappings support explicit city or interests only.")
            profile = getattr(contact.user, "community_member_profile", None) if contact.user_id else None
            if profile:
                records.append({"mapping_id": mapping["id"], "source_record_id": f"{prefix}profile:{profile.pk}:{key}",
                                "observed_at": profile.updated_at.isoformat(), "value": getattr(profile, key)})
        else:
            raise CommandError("This refresh supports Luma answers, CSV fields and community profile mappings only.")
    return records


class Command(BaseCommand):
    """Project up to one bounded page; defaults to reporting aggregate changes without writes."""

    help = "Preview reviewed audience mappings; use --apply and an MLAI admin actor for explicit writes."

    def add_arguments(self, parser):
        parser.add_argument("--mapping-file", required=True)
        parser.add_argument("--limit", type=int, default=250)
        parser.add_argument("--after-id")
        parser.add_argument("--observed-at")
        parser.add_argument("--include-activity", action="store_true")
        parser.add_argument("--apply", action="store_true")
        parser.add_argument("--actor-id", type=int)

    def handle(self, *args, **options):
        if not 1 <= options["limit"] <= 1000:
            raise CommandError("--limit must be between 1 and 1000.")
        try:
            body = json.loads(Path(options["mapping_file"]).read_text())
            if not isinstance(body, dict) or set(body) != {"mappings"} or not isinstance(body["mappings"], list):
                raise AudienceProfileError("invalid_mapping_manifest")
            if len(body["mappings"]) > 50:
                raise AudienceProfileError("too_many_mappings")
            mappings = [validate_mapping(item) for item in body["mappings"]]
            if len({item["id"] for item in mappings}) != len(mappings):
                raise AudienceProfileError("duplicate_mapping_id")
            now = timezone.now()
            if options["observed_at"] and parse_instant(options["observed_at"]) > now:
                raise AudienceProfileError("future_observation")
            after = uuid.UUID(options["after_id"]) if options["after_id"] else None
        except (OSError, ValueError, TypeError) as exc:
            raise CommandError("Invalid mapping manifest, cursor or observation timestamp.") from exc
        actor = None
        if options["apply"]:
            if not options["actor_id"]:
                raise CommandError("--apply requires --actor-id for an authorized MLAI admin.")
            actor = get_user_model().objects.get(pk=options["actor_id"])
            require_mlai_admin(actor)
        contacts = Contact.objects.order_by("pk")
        if after:
            contacts = contacts.filter(pk__gt=after)
        ids = list(contacts.values_list("pk", flat=True)[:options["limit"] + 1])
        has_more, ids = len(ids) > options["limit"], ids[:options["limit"]]
        collected = None
        if options["include_activity"] and ids:
            from crm.engagement_sources import collect_activity
            collected = collect_activity(list(Contact.objects.filter(pk__in=ids)), as_of=now)
        counts = Counter()
        for contact_id in ids:
            with transaction.atomic():
                rows = Contact.objects.select_for_update(of=("self",)) if options["apply"] else Contact.objects
                contact = rows.select_related("user", "user__community_member_profile").prefetch_related("registrations").get(pk=contact_id)
                records = mapped_contact_records(contact, mappings, observed_at=options["observed_at"])
                profile, observed_counts = project_records(contact.attributes, records, mappings, now=now)
                for mapping in mappings:
                    active = {record["source_record_id"] for record in records if record["mapping_id"] == mapping["id"]}
                    profile = retract_mapping_sources(profile, mapping, active, now=now)
                if collected is not None:
                    observations = collected["activities"].get(contact.email, [])
                    for source, evidence in activity_projection(observations, collected["coverage"], now=now).items():
                        profile = merge_activity(profile, source, evidence)
                attributes = with_profile(contact.attributes, profile)
                changed = attributes != contact.attributes
                counts.update(observed_counts)
                counts["contacts"] += 1
                counts["changed"] += int(changed)
                if changed and options["apply"]:
                    contact.attributes = attributes
                    contact.save(update_fields=["attributes", "updated_at"])
                    CrmAuditEvent.objects.create(actor=actor, action=CrmAuditEvent.Action.EDIT,
                        target_type="contact", target_id=str(contact.pk), detail={
                            "operation": "audience_profile_refresh",
                            "mapping_ids": [item["id"] for item in mappings],
                            "fields": sorted({item["facet"] for item in mappings}),
                        })
        self.stdout.write(json.dumps({"applied": options["apply"], "counts": dict(counts),
            "next_after_id": str(ids[-1]) if ids and has_more else None}, sort_keys=True))
