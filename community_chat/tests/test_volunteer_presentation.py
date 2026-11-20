"""Server presentation boundaries; requires approved disposable migrations."""

import hashlib
import io
import json
import tempfile
from pathlib import Path

from coincurve import PrivateKey
from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase, override_settings
from django.utils import timezone
from rest_framework.test import APIRequestFactory, force_authenticate

from community_chat.models import CommunityChatDevice
from community_chat.views import PublicProfileBatchView
from community_chat.volunteer.models import (
    VolunteerMemberState,
    VolunteerRecognition,
    VolunteerSourceReceipt,
)
from community_chat.volunteer.serializers import (
    contribution_dto,
    guide_contact,
    member_dto,
)


@override_settings(
    COMMUNITY_CHAT_VOLUNTEER_COMMUNITY="volunteer-presentation-tests",
    COMMUNITY_CHAT_VOLUNTEER_CHANNELS={"general": "public-general"},
)
class VolunteerPresentationTests(TestCase):
    """Expose reachable humans and evidence without inventing availability."""

    def setUp(self):
        self.member = self.user("member", "a")
        self.guide = self.user("guide", "b")
        self.reviewer = self.user("reviewer", "c", admin=True)
        self.source = {
            "channel_id": "public-general",
            "source_id": "post-1",
            "message_id": "post-1",
        }
        self.original = VolunteerSourceReceipt.objects.create(
            community="volunteer-presentation-tests",
            actor=self.member,
            source_key="post-1",
            source=self.source,
            origin="relay",
            kind="post",
            occurred_at=timezone.now(),
            metadata={},
            status="recorded",
        )
        self.record = VolunteerRecognition.objects.create(
            community="volunteer-presentation-tests",
            user=self.member,
            reviewer=self.reviewer,
            action_key="first_channel_contribution",
            outcome_key="post-1",
            source=self.source,
            policy_snapshot={
                "title": "A useful post",
                "reward_roo": "1",
                "reward_max_roo": "1",
            },
            occurred_at=timezone.now(),
            reward_microroo=1_000_000,
        )

    def user(self, name, key, *, admin=False):
        user = get_user_model().objects.create_user(
            email=f"presentation-{name}@example.test",
            first_name=name,
            is_superuser=admin,
        )
        CommunityChatDevice.objects.create(
            user=user, public_key=key * 64, status="verified"
        )
        return user

    def invalidate(self, source_id, actor):
        return VolunteerSourceReceipt.objects.create(
            community="volunteer-presentation-tests",
            actor=actor,
            source_key=f"deletion:{source_id}",
            source={"source_id": source_id},
            origin="relay",
            kind="invalidation",
            occurred_at=timezone.now(),
            metadata={"deletion_kind": 5, "invalidated": True},
            status="recorded",
        )

    def test_reachable_guide_keeps_the_original_contact(self):
        result = guide_contact(self.guide, self.reviewer)
        self.assertEqual(result["guide"]["public_key"], "b" * 64)
        self.assertTrue(result["guide_available"])
        self.assertFalse(result["guide_is_fallback"])
        self.assertNotIn("email", result["guide"])

    def test_revoked_guide_uses_an_authorised_reachable_fallback(self):
        CommunityChatDevice.objects.filter(user=self.guide).update(status="revoked")
        result = guide_contact(self.guide, self.reviewer)
        self.assertEqual(result["guide"]["id"], str(self.reviewer.pk))
        self.assertEqual(result["guide"]["public_key"], "c" * 64)
        self.assertTrue(result["guide_is_fallback"])

    def test_unavailable_contacts_never_advertise_a_revoked_device(self):
        self.guide.is_active = False
        self.guide.save(update_fields=("is_active",))
        self.assertIsNone(member_dto(self.guide)["public_key"])
        result = guide_contact(self.guide, self.member)
        self.assertFalse(result["guide_available"])
        self.assertIsNone(result["guide"]["public_key"])

    def test_deleted_evidence_is_explicitly_unavailable_without_losing_the_receipt(
        self,
    ):
        self.invalidate("post-1", self.member)
        result = contribution_dto(self.record, self.member)
        self.assertEqual(result["source"], {})
        self.assertEqual(result["title"], "A useful post")
        self.assertEqual(result["status"], "pending")

    def test_another_members_removed_reaction_does_not_hide_the_original_post(self):
        VolunteerSourceReceipt.objects.create(
            community="volunteer-presentation-tests",
            actor=self.guide,
            target=self.member,
            source_key="like-1",
            source={**self.source, "message_id": "like-1"},
            origin="relay",
            kind="reaction",
            occurred_at=timezone.now(),
            status="processed",
        )
        self.invalidate("like-1", self.guide)
        self.assertEqual(
            contribution_dto(self.record, self.member)["source"], self.source
        )

    def test_review_history_keeps_automatic_and_member_attribution_explicit(self):
        self.record.review_history = [
            {
                "decision": "approve",
                "note": "Recorded automatically",
                "actor_id": None,
                "automatic": True,
                "at": timezone.now().isoformat(),
            },
            {
                "decision": "resubmitted",
                "note": "My updated evidence",
                "actor_id": str(self.member.pk),
                "at": timezone.now().isoformat(),
            },
        ]
        history = contribution_dto(self.record, self.member)["review_history"]
        self.assertTrue(history[0]["automatic"])
        self.assertIsNone(history[0]["actor"])
        self.assertFalse(history[1]["automatic"])
        self.assertEqual(history[1]["actor"]["id"], str(self.member.pk))
        self.assertEqual(history[1]["decision"], "resubmitted")


@override_settings(
    COMMUNITY_CHAT_VOLUNTEER_COMMUNITY="profile-publication-tests",
    COMMUNITY_CHAT_VOLUNTEER_ENABLED=True,
    COMMUNITY_CHAT_VOLUNTEER_ONE_RECORD_ENABLED=True,
    COMMUNITY_CHAT_VOLUNTEER_PROFILE_CONTRIBUTIONS_ENABLED=True,
)
class ContributionProfilePublicationTests(TestCase):
    def setUp(self):
        self.scope = "profile-publication-tests"
        self.admin = get_user_model().objects.create_user(
            email="profile-admin@example.test", is_superuser=True
        )
        self.member, self.key = self.create_member("member", "a", opening=18_599_000_000)
        self.other, self.other_key = self.create_member("other", "b", opening=0)
        self.pending, _ = self.create_member("pending", "c", opening=None)
        self.manifest = {
            "community": self.scope,
            "evidence_sha256": "c" * 64,
            "members": [self.member.pk],
            "grants": [{
                "member_id": self.member.pk,
                "badge_id": "event.lead",
                "reason": "Led a public community workshop.",
                "public_url": "https://luma.com/example",
                "evidence_ref": "private-email-reference",
            }],
        }

    def create_member(self, name, key, *, opening):
        user = get_user_model().objects.create_user(email=f"profile-{name}@example.test")
        VolunteerMemberState.objects.create(
            community=self.scope, user=user, historical_ledger_cutoff=0,
            historical_microroo=opening,
        )
        secret = hashlib.sha256(f"profile-publication-{key}".encode()).digest()
        key = PrivateKey(secret).public_key_xonly.format().hex()
        CommunityChatDevice.objects.create(
            user=user, public_key=key, status="verified", verified_at=timezone.now(),
        )
        return user, key

    def publish(self, *, apply=False):
        output = io.StringIO()
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "review.json"
            path.write_text(json.dumps(self.manifest))
            call_command(
                "import_volunteer_badges", str(path), reviewer_id=self.admin.pk,
                apply=apply, stdout=output,
            )
        return json.loads(output.getvalue())

    def profiles(self, *, include=True, authenticate=True):
        request = APIRequestFactory().post(
            "/", {"public_keys": [self.key, self.other_key], "include_contributions": include},
            format="json",
        )
        if authenticate:
            force_authenticate(request, user=self.other)
        return PublicProfileBatchView.as_view()(request)

    def test_publication_and_rollback_are_scoped_to_reviewed_accounts(self):
        before = self.profiles().data["profiles"]
        self.assertNotIn("contributions", before[self.key])
        self.assertNotIn("contributions", before[self.other_key])
        self.assertEqual(self.publish()["profiles_new"], 1)
        self.assertFalse(VolunteerSourceReceipt.objects.filter(community=self.scope).exists())

        self.assertEqual(self.publish(apply=True)["new"], 1)
        self.assertEqual(self.publish(apply=True)["new"], 0)
        result = self.profiles().data["profiles"]
        self.assertEqual(before[self.other_key], result[self.other_key])
        profile = result[self.key]["contributions"]
        self.assertEqual(profile["rank"]["level"], 181)
        self.assertEqual(profile["contribution_roo"], "18599")
        self.assertEqual(profile["badges"][0]["evidence_url"], "https://luma.com/example")
        self.assertNotIn("evidence_ref", profile["badges"][0])
        self.assertNotIn("contributions", self.profiles(include=False).data["profiles"][self.key])
        self.assertIn(self.profiles(authenticate=False).status_code, (401, 403))
        with override_settings(COMMUNITY_CHAT_VOLUNTEER_PROFILE_CONTRIBUTIONS_ENABLED=False):
            self.assertEqual(before, self.profiles().data["profiles"])
        VolunteerSourceReceipt.objects.filter(community=self.scope, kind="profile_publication").update(
            status="withdrawn"
        )
        self.assertEqual(before, self.profiles().data["profiles"])

    def test_batch_rejects_unreconciled_or_out_of_cohort_accounts(self):
        self.manifest["members"].append(self.pending.pk)
        with self.assertRaises(CommandError):
            self.publish(apply=True)
        self.manifest["members"].pop()
        self.manifest["grants"][0]["member_id"] = self.other.pk
        with self.assertRaises(CommandError):
            self.publish(apply=True)
        self.assertFalse(VolunteerSourceReceipt.objects.filter(community=self.scope).exists())

        self.manifest["grants"] = []
        self.publish(apply=True)
        self.assertEqual(self.profiles().data["profiles"][self.key]["contributions"]["badges"], [])
