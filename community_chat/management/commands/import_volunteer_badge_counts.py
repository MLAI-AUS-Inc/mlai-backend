"""Preview, publish or withdraw reviewed counts for existing badge grants."""

import hashlib
import json
import re
from pathlib import Path
from uuid import UUID

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone

from community_chat.volunteer.access import community_id, require_capability
from community_chat.volunteer.badge_counts import (
    COUNT_KIND, COUNT_ORIGIN, PUBLISHED, WITHDRAWN, count_key, grant_fingerprint, valid_value,
)
from community_chat.volunteer.models import VolunteerMemberState, VolunteerOpeningEntry, VolunteerSourceReceipt
from community_chat.volunteer.profile_progression import (
    BADGE_KIND, BADGE_ORIGIN, BADGE_STATUS, PROFILE_KIND, PROFILE_STATUS, badge_catalogue,
    profile_contributions_enabled,
)


def is_digest(value):
    return isinstance(value, str) and re.fullmatch(r"[0-9a-f]{64}", value) is not None


def validate_manifest(raw, expected_digest, scope):
    """Validate the approved bytes and all row identities before any database work."""
    if not is_digest(expected_digest) or hashlib.sha256(raw).hexdigest() != expected_digest:
        raise CommandError("Badge count manifest digest mismatch")
    try:
        data = json.loads(raw)
    except (UnicodeError, ValueError) as exc:
        raise CommandError("Invalid badge count manifest JSON") from exc
    if not isinstance(data, dict) or set(data) != {
        "community", "review_state", "rule_version", "source_manifest_sha256", "rows",
    }:
        raise CommandError("Invalid badge count manifest fields")
    if (data["community"] != scope or data["review_state"] != "approved"
            or not is_digest(data["source_manifest_sha256"]) or not isinstance(data["rule_version"], str)
            or not 1 <= len(data["rule_version"].strip()) <= 100
            or not isinstance(data["rows"], list) or not data["rows"]):
        raise CommandError("An approved, scoped badge count manifest is required")
    catalogue = badge_catalogue()
    seen, grant_ids = set(), set()
    for row in data["rows"]:
        if not isinstance(row, dict) or set(row) != {
            "member_id", "badge_id", "grant_id", "grant_evidence_sha256", "grant_evidence_ref",
            "count", "status", "progress",
        }:
            raise CommandError("Invalid badge count row fields")
        uid, badge_id = row["member_id"], row["badge_id"]
        if (type(uid) is not int or uid < 1 or not isinstance(badge_id, str) or badge_id not in catalogue
                or not isinstance(row["grant_id"], str) or not is_digest(row["grant_evidence_sha256"])
                or not isinstance(row["grant_evidence_ref"], str) or not 3 <= len(row["grant_evidence_ref"]) <= 100
                or not valid_value(badge_id, row["count"], row["status"], row["progress"])):
            raise CommandError("Invalid badge count or grant identity")
        try:
            grant_id = UUID(row["grant_id"])
        except ValueError as exc:
            raise CommandError("Invalid grant receipt UUID") from exc
        if row["grant_id"] != str(grant_id) or (uid, badge_id) in seen or grant_id in grant_ids:
            raise CommandError("Duplicate or noncanonical grant identity")
        grant_ids.add(grant_id)
        seen.add((uid, badge_id))
    return data


class Command(BaseCommand):
    help = "Preview or publish the verified counts on existing, active volunteer patches."

    def add_arguments(self, parser):
        parser.add_argument("manifest", type=Path)
        parser.add_argument("--sha256", required=True)
        parser.add_argument("--reviewer-id", type=int, required=True)
        parser.add_argument("--apply", action="store_true")
        parser.add_argument("--rollback", action="store_true", help="Withdraw only counts owned by this exact publication")

    @transaction.atomic
    def handle(self, *args, **options):
        scope = community_id()
        digest = options["sha256"]
        data = validate_manifest(options["manifest"].read_bytes(), digest, scope)
        apply, rollback = options["apply"], options["rollback"]
        reviewer = get_user_model().objects.filter(pk=options["reviewer_id"], is_active=True).first()
        if reviewer is None:
            raise CommandError("Active reviewer required")
        require_capability(reviewer, "can_correct" if rollback else "can_publish")
        if not rollback and not profile_contributions_enabled():
            raise CommandError("Published contribution profiles must be enabled")

        ids = {r["member_id"] for r in data["rows"]}
        accounts = get_user_model().objects.filter(pk__in=ids).order_by("pk")
        keys = {count_key(r["member_id"], r["badge_id"]) for r in data["rows"]}
        if not rollback:
            keys.update(f"volunteer-profile:{uid}" for uid in ids)
            keys.update(f"volunteer-badge:{r['member_id']}:{r['badge_id']}" for r in data["rows"])
        receipts = VolunteerSourceReceipt.objects.filter(community=scope, source_key__in=keys).order_by("source_key")
        if apply:
            accounts, receipts = accounts.select_for_update(), receipts.select_for_update()
        # Serialize with publishers on canonical account locks, including when a companion does not exist yet.
        active = {uid for uid, enabled in accounts.values_list("pk", "is_active") if enabled}
        current = {r.source_key: r for r in receipts}
        if not rollback:
            if ids != active:
                raise CommandError("A reviewed member is missing or inactive")
            states = VolunteerMemberState.objects.filter(community=scope, user_id__in=ids).values_list(
                "user_id", "historical_microroo"
            )
            openings = set(VolunteerOpeningEntry.objects.filter(
                community=scope, user_id__in=ids
            ).values_list("user_id", flat=True).distinct())
            if {uid for uid, amount in states if amount is not None or uid in openings} != ids:
                raise CommandError("A reviewed member has no reconciled contribution history")
            for uid in ids:
                pub = current.get(f"volunteer-profile:{uid}")
                if (pub is None or pub.actor_id != uid or pub.kind != PROFILE_KIND or pub.origin != BADGE_ORIGIN
                        or pub.status != PROFILE_STATUS):
                    raise CommandError("A reviewed profile is unpublished")

        creates, withdrawals = [], []
        unchanged = 0
        now = timezone.now()
        for row in data["rows"]:
            uid, badge_id = row["member_id"], row["badge_id"]
            key = count_key(uid, badge_id)
            existing = current.get(key)
            fingerprint = grant_fingerprint(row["grant_id"], row["grant_evidence_sha256"], row["grant_evidence_ref"])
            metadata = dict(badge_id=badge_id, grant_fingerprint=fingerprint, count=row["count"], status=row["status"],
                            progress=row["progress"], manifest_sha256=digest, source_manifest_sha256=data["source_manifest_sha256"],
                            rule_version=data["rule_version"])
            if existing is not None and (
                existing.actor_id != uid or existing.origin != COUNT_ORIGIN or existing.kind != COUNT_KIND
                or not isinstance(existing.metadata, dict)
                or any(existing.metadata.get(k) != v for k, v in metadata.items())
            ):
                raise CommandError("Existing badge count differs from the approved publication")
            if rollback:
                if existing is None or existing.status == WITHDRAWN:
                    unchanged += 1
                elif existing.status == PUBLISHED:
                    existing.status = WITHDRAWN
                    existing.metadata = dict(existing.metadata, withdrawn_by=reviewer.pk, withdrawn_at=now.isoformat())
                    existing.updated_at = now
                    withdrawals.append(existing)
                else:
                    raise CommandError("Unexpected badge count status")
                continue

            grant = current.get(f"volunteer-badge:{uid}:{badge_id}")
            if (grant is None or str(grant.pk) != row["grant_id"] or grant.actor_id != uid or grant.origin != BADGE_ORIGIN
                    or grant.kind != BADGE_KIND or grant.status != BADGE_STATUS or not isinstance(grant.metadata, dict)
                    or grant.metadata.get("badge_id") != badge_id
                    or grant.metadata.get("evidence_sha256") != row["grant_evidence_sha256"]
                    or grant.metadata.get("evidence_ref") != row["grant_evidence_ref"]):
                raise CommandError("A reviewed badge grant is missing, withdrawn or changed")
            if existing is None:
                creates.append(VolunteerSourceReceipt(
                    community=scope, actor_id=uid, source_key=key, origin=COUNT_ORIGIN, kind=COUNT_KIND,
                    status=PUBLISHED, metadata=dict(metadata, published_by=reviewer.pk), occurred_at=now,
                ))
            elif existing.status == PUBLISHED:
                unchanged += 1
            else:
                raise CommandError("Previously withdrawn badge count requires a new review")

        if apply and creates:
            VolunteerSourceReceipt.objects.bulk_create(creates)
        if apply and withdrawals:
            VolunteerSourceReceipt.objects.bulk_update(withdrawals, ["status", "metadata", "updated_at"])
        self.stdout.write(json.dumps(dict(rows=len(data["rows"]), new=len(creates), withdraw=len(withdrawals),
                                          unchanged=unchanged, applied=apply, rollback=rollback, manifest_sha256=digest)))
