"""Publish reviewed contribution profiles through the existing receipt store."""

import json
import re
from pathlib import Path

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone

from community_chat.volunteer.access import community_id, require_capability
from community_chat.volunteer.models import VolunteerMemberState, VolunteerSourceReceipt
from community_chat.volunteer.profile_progression import (
    BADGE_KIND, BADGE_ORIGIN, BADGE_STATUS, PROFILE_KIND, PROFILE_STATUS,
    badge_catalogue, profile_contributions_enabled, public_evidence_url,
)
from community_chat.volunteer.services import contribution_total


class Command(BaseCommand):
    help = "Preview or publish a reviewed set of contribution profiles and badge grants."

    def add_arguments(self, parser):
        parser.add_argument("manifest", type=Path)
        parser.add_argument("--reviewer-id", type=int, required=True)
        parser.add_argument("--apply", action="store_true")

    @transaction.atomic
    def handle(self, *args, **options):
        manifest = json.loads(options["manifest"].read_text(encoding="utf-8"))
        scope = community_id()
        digest = manifest.get("evidence_sha256", "")
        rows = manifest.get("grants")
        members = manifest.get("members")
        if (manifest.get("community") != scope or not isinstance(digest, str)
                or not re.fullmatch(r"[a-f0-9]{64}", digest) or not isinstance(rows, list)
                or not isinstance(members, list) or not members
                or any(type(uid) is not int or uid < 1 for uid in members)
                or len(set(members)) != len(members)):
            raise CommandError("Invalid community, reviewed archive digest, member IDs or badge list")
        if not profile_contributions_enabled():
            raise CommandError("Contribution profiles and one-record accounting must be enabled")

        user_model = get_user_model()
        reviewer = user_model.objects.filter(pk=options["reviewer_id"], is_active=True).first()
        if reviewer is None:
            raise CommandError("Reviewer account unavailable")
        require_capability(reviewer, "can_publish")

        accounts = user_model.objects.filter(pk__in=members, is_active=True).order_by("pk")
        if options["apply"]:
            accounts = accounts.select_for_update()
        users = {user.pk: user for user in accounts}
        states = {
            state.user_id: state
            for state in VolunteerMemberState.objects.filter(community=scope, user_id__in=members)
        }
        for uid in members:
            if uid not in users or uid not in states or contribution_total(users[uid], states[uid]) is None:
                raise CommandError(f"No reconciled contribution history for active account {uid}")

        audit = dict(reviewer_id=reviewer.pk, evidence_sha256=digest)
        creates = []

        def prepare(uid, source_key, kind, status, metadata):
            existing = VolunteerSourceReceipt.objects.filter(community=scope, source_key=source_key).first()
            if existing:
                if (existing.actor_id != uid or existing.origin != BADGE_ORIGIN or existing.kind != kind
                        or existing.status != status or (kind == BADGE_KIND and existing.metadata != metadata)):
                    raise CommandError(f"Review required for existing receipt {source_key}")
                return False
            creates.append(VolunteerSourceReceipt(
                community=scope, source_key=source_key, origin=BADGE_ORIGIN, kind=kind,
                actor_id=uid, status=status, metadata=metadata, occurred_at=timezone.now(),
            ))
            return True

        profiles_new = sum(
            prepare(uid, f"volunteer-profile:{uid}", PROFILE_KIND, PROFILE_STATUS, audit)
            for uid in members
        )
        catalogue = badge_catalogue()
        seen = set()
        for row in rows:
            if not isinstance(row, dict):
                raise CommandError("Invalid badge entry")
            uid, key = row.get("member_id"), row.get("badge_id")
            reason, url, anchor = row.get("reason"), row.get("public_url"), row.get("evidence_ref")
            if (type(uid) is not int or uid not in users or not isinstance(key, str)
                    or key not in catalogue or (uid, key) in seen
                    or not isinstance(reason, str) or not 12 <= len(reason.strip()) <= 280
                    or not isinstance(anchor, str) or not 3 <= len(anchor) <= 100
                    or (url and not public_evidence_url(url))):
                raise CommandError(f"Invalid badge entry for account {uid}")
            seen.add((uid, key))
            prepare(
                uid, f"volunteer-badge:{uid}:{key}", BADGE_KIND, BADGE_STATUS,
                dict(**audit, badge_id=key, public_reason=reason.strip(),
                     public_url=url or None, evidence_ref=anchor),
            )
        badges_new = len(creates) - profiles_new
        if options["apply"]:
            VolunteerSourceReceipt.objects.bulk_create(creates)
        self.stdout.write(json.dumps(dict(
            profiles_new=profiles_new, profiles_existing=len(members) - profiles_new,
            new=badges_new, existing=len(seen) - badges_new, applied=options["apply"],
        )))
