"""Pure validation and member-visible contracts for historical badge counts."""

import hashlib
import json
from types import SimpleNamespace
from uuid import uuid4

from django.core.management.base import CommandError
from django.test import SimpleTestCase

from community_chat.management.commands.import_volunteer_badge_counts import validate_manifest
from community_chat.volunteer.badge_counts import grant_fingerprint, public_count


class BadgeCountContractTests(SimpleTestCase):
    def setUp(self):
        self.grant = SimpleNamespace(
            pk=uuid4(), actor_id=12, metadata={"evidence_sha256": "b" * 64, "evidence_ref": "private:row"},
        )
        self.row = dict(member_id=12, badge_id="event.series", grant_id=str(self.grant.pk),
                        grant_evidence_sha256="b" * 64, grant_evidence_ref="private:row",
                        count=1, status="exact", progress={"months": 4, "events_led": 7})
        self.payload = dict(community="example", review_state="approved", rule_version="test-v1",
                            source_manifest_sha256="a" * 64, rows=[self.row])

    def validate(self, *, digest=None):
        raw = json.dumps(self.payload).encode()
        return validate_manifest(raw, digest or hashlib.sha256(raw).hexdigest(), "example")

    def test_approved_hash_required_and_grant_identity_unique(self):
        self.assertEqual(self.validate()["rows"], [self.row])
        with self.assertRaises(CommandError):
            self.validate(digest="0" * 64)
        self.payload["review_state"] = "draft"
        with self.assertRaises(CommandError):
            self.validate()
        self.payload["review_state"] = "approved"
        self.payload["rows"].append(dict(self.row, member_id=33))
        with self.assertRaises(CommandError):
            self.validate()

    def test_invalid_numbers_and_private_progress_fields_are_rejected(self):
        for change in [
            {"count": True}, {"count": 0}, {"count": 2**53}, {"status": "estimated"},
            {"progress": {"members": 3}}, {"progress": {"events_led": "private"}},
            {"progress": {"months": -1}}, {"progress": {"months": False}},
        ]:
            with self.subTest(change=change):
                self.payload["rows"] = [dict(self.row, **change)]
                with self.assertRaises(CommandError):
                    self.validate()

    def test_read_returns_only_public_fields_and_fails_closed_on_changed_evidence(self):
        meta = dict(badge_id="event.series", count=1, status="exact", progress=self.row["progress"],
                    grant_fingerprint=grant_fingerprint(self.grant.pk, "b" * 64, "private:row"),
                    private_note="restricted evidence", published_by=55)
        receipt = SimpleNamespace(actor_id=12, metadata=meta)
        self.assertEqual(public_count(self.grant, "event.series", receipt), {
            "earn_count": 1, "earn_count_status": "exact", "earn_count_progress": {"months": 4, "events_led": 7},
        })
        unresolved = {"earn_count": None, "earn_count_status": "unresolved", "earn_count_progress": {}}
        self.assertEqual(public_count(self.grant, "event.series", None), unresolved)
        self.assertEqual(public_count(self.grant, "event.lead", receipt), unresolved)
        self.grant.metadata["evidence_ref"] = "replacement:row"
        self.assertEqual(public_count(self.grant, "event.series", receipt), unresolved)
