"""Server presentation boundaries; requires approved disposable migrations."""

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.utils import timezone

from community_chat.models import CommunityChatDevice
from community_chat.volunteer.models import VolunteerRecognition, VolunteerSourceReceipt
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
