"""Volunteer integration tests on synthetic data; requires migration approval."""

from datetime import timedelta
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.db import IntegrityError, transaction
from django.test import TestCase, override_settings
from django.utils import timezone
from rest_framework.test import APIClient

from community_chat.models import CommunityChatDevice
from community_chat.volunteer.access import VolunteerError, community_id
from community_chat.volunteer.models import (
    VolunteerAttendance,
    VolunteerMilestone,
    VolunteerOpportunity,
    VolunteerRecognition,
    VolunteerSourceReceipt,
)
from community_chat.volunteer.policy import microroo
from community_chat.volunteer.receipts import (
    ingest_receipt,
    process_receipt,
    record_luma_guest,
)
from community_chat.volunteer.services import (
    contribution_total,
    decision,
    journey,
    request_recognition,
    revise_request,
    state_for,
)
from roo.models import Ledger, PointsAccount
from roo.services import PointsService


@override_settings(
    COMMUNITY_CHAT_VOLUNTEER_ENABLED=True,
    COMMUNITY_CHAT_VOLUNTEER_RECOGNITION_ENABLED=True,
    COMMUNITY_CHAT_VOLUNTEER_AWARDS_ENABLED=True,
    COMMUNITY_CHAT_VOLUNTEER_BONUSES_ENABLED=True,
    COMMUNITY_CHAT_VOLUNTEER_ATTENDANCE_ENABLED=True,
    COMMUNITY_CHAT_VOLUNTEER_COMMUNITY="volunteer-tests",
    COMMUNITY_CHAT_VOLUNTEER_CHANNELS={
        "start_here": "start",
        "boost_startup": "boost",
        "general": "general",
        "monthly_updates": "monthly",
        "bugs": "bugs",
        "help": "help",
        "random": "random",
    },
    COMMUNITY_CHAT_VOLUNTEER_LIKE_REACTIONS=["+"],
    COMMUNITY_CHAT_VOLUNTEER_ACTIVE_FROM="2020-01-01T00:00:00Z",
    COMMUNITY_CHAT_VOLUNTEER_RECEIPT_TOKEN="synthetic-test-service-token",
)
class VolunteerTests(TestCase):
    def setUp(self):
        self.member = self.user("member", "a")
        self.other = self.user("other", "b")
        self.reviewer = self.user("reviewer", "c", admin=True)
        self.override = override_settings(
            COMMUNITY_CHAT_VOLUNTEER_REVIEWER_ID=self.reviewer.pk
        )
        self.override.enable()
        self.addCleanup(self.override.disable)
        self.client = APIClient()
        self.client.force_authenticate(self.member)

    def user(self, name, key, admin=False):
        user = get_user_model().objects.create_user(
            email=f"{name}@example.test",
            first_name=name,
            is_superuser=admin,
            email_verified_at=timezone.now(),
        )
        CommunityChatDevice.objects.create(
            user=user, public_key=key * 64, status="verified"
        )
        return user

    def receipt(
        self,
        key="intro",
        actor="a",
        kind="post",
        channel="start",
        source_id=None,
        metadata=None,
    ):
        return ingest_receipt(
            dict(
                source_key=key,
                origin="relay",
                kind=kind,
                actor_public_key=actor * 64,
                source={
                    "channel_id": channel,
                    "source_id": source_id or key,
                    "message_id": key,
                    "thread_root_id": "root",
                },
                occurred_at=(timezone.now() - timedelta(minutes=1)).isoformat(),
                metadata=(
                    metadata
                    if metadata is not None
                    else dict(original=True, top_level=True, has_text=True)
                ),
            )
        )

    def event(self, key="event-1"):
        now = timezone.now()
        return VolunteerOpportunity.objects.create(
            community=community_id(),
            event_id=key,
            kind="event",
            action_key="volunteer_event",
            title="Synthetic test event",
            purpose="Test scope",
            description="Confirm combined event help",
            guide=self.reviewer,
            reviewer=self.reviewer,
            source={"channel_id": "general", "thread_root_id": key, "source_id": key},
            starts_at=now - timedelta(hours=3),
            ends_at=now - timedelta(hours=2),
            reward_microroo=microroo("6"),
            reward_max_microroo=microroo("18"),
        )

    def checked_in(self, user=None):
        return VolunteerAttendance.objects.create(
            community=community_id(),
            user=user or self.member,
            event_id="first",
            checked_in_at=timezone.now() - timedelta(days=1),
            source_id="verified-test-check-in",
        )

    def request_event(self, event=None, user=None):
        event = event or self.event()
        return request_recognition(
            user or self.member,
            dict(
                action_key="volunteer_event",
                opportunity_id=event.pk,
                source=event.source,
                note="I welcomed attendees",
            ),
        )[0]

    def approve(
        self,
        record,
        amount="6",
        note="Thank you for welcoming new members.",
        key="decision-1",
    ):
        return decision(
            record,
            self.reviewer,
            dict(
                decision="approve",
                version=record.version,
                reward_roo=amount,
                note=note,
                idempotency_key=key,
            ),
        )[0]

    def test_intro_is_four_contribution_six_wallet_and_retry_safe(self):
        receipt = self.receipt()
        self.assertEqual(receipt.status, "processed")
        self.assertEqual(contribution_total(self.member), microroo("4"))
        self.assertEqual(
            PointsService.get_available_microroo(self.member), microroo("6")
        )
        self.assertEqual(PointsService.get_balance(self.member)["lifetime_earned"], 4)
        process_receipt(receipt)
        self.assertEqual(Ledger.objects.filter(user=self.member).count(), 2)
        self.assertEqual(VolunteerMilestone.objects.filter(user=self.member).count(), 1)
        self.assertTrue(journey(self.member)["current_level"]["bonus_awarded"])

    def test_zero_wallet_and_legacy_unknown_are_distinct(self):
        self.assertEqual(journey(self.member)["current_level"]["name"], "MLAI Curious")
        PointsService.award(
            self.other,
            8,
            "MANUAL",
            "Unclassified historical award",
            "system",
            "old-award",
        )
        result = journey(self.other)
        self.assertFalse(result["history_reconciled"])
        self.assertIsNone(result["current_level"])
        self.assertEqual(result["wallet_balance"], "8")

    def test_purchase_spending_and_bonus_do_not_advance_rank(self):
        self.receipt()
        PointsService.credit_purchased_topup(
            self.member, 20, "Test topup", "test-topup"
        )
        PointsService.spend(
            self.member, 2, "TOOLS", "Test spend", "system", "test-spend"
        )
        self.assertEqual(contribution_total(self.member), microroo("4"))
        self.assertEqual(journey(self.member)["current_level"]["level"], 1)

    def test_multi_level_crossing_and_correction_cannot_repay_bonus(self):
        self.checked_in()
        first = self.approve(self.request_event(), amount="18")
        self.assertEqual(VolunteerMilestone.objects.filter(user=self.member).count(), 2)
        record, _ = decision(
            first,
            self.reviewer,
            dict(
                decision="reverse",
                version=first.version,
                note="Corrected duplicate outcome",
                idempotency_key="reverse-1",
            ),
        )
        self.assertEqual(record.status, "reversed")
        self.assertEqual(contribution_total(self.member), 0)
        second = self.approve(
            self.request_event(self.event("event-2")), amount="18", key="decision-2"
        )
        self.assertEqual(VolunteerMilestone.objects.filter(user=self.member).count(), 2)
        self.assertEqual(
            PointsService.get_available_microroo(self.member), microroo("23")
        )

    def test_event_stays_open_and_multiple_people_can_be_recognised(self):
        event = self.event()
        self.checked_in()
        self.checked_in(self.other)
        self.approve(self.request_event(event))
        self.approve(self.request_event(event, self.other), key="other-review")
        event.refresh_from_db()
        self.assertEqual(event.status, "open")
        self.assertEqual(event.recognitions.count(), 2)

    def test_closed_event_allows_post_event_recognition_without_reply(self):
        self.checked_in()
        event = self.event()
        event.status = "closed"
        event.save()
        self.approve(self.request_event(event))
        self.assertEqual(VolunteerSourceReceipt.objects.count(), 0)

    def test_one_event_invitation_database_constraint(self):
        event = self.event()
        with self.assertRaises(IntegrityError), transaction.atomic():
            event.pk = None
            event.save(force_insert=True)

    def test_registration_is_not_attendance(self):
        self.assertIsNone(
            record_luma_guest(
                user=self.member,
                event_id="registered",
                guest={
                    "id": "guest",
                    "approval_status": "approved",
                    "registered_at": timezone.now().isoformat(),
                },
            )
        )
        with self.assertRaisesMessage(VolunteerError, "attendance_required"):
            self.request_event()

    def test_luma_ticket_checkin_persists_when_awards_disabled(self):
        with override_settings(COMMUNITY_CHAT_VOLUNTEER_AWARDS_ENABLED=False):
            record_luma_guest(
                user=self.member,
                event_id="checked",
                guest={
                    "guest": {
                        "id": "guest",
                        "event_tickets": [
                            {
                                "checked_in_at": (
                                    timezone.now() - timedelta(hours=1)
                                ).isoformat()
                            }
                        ],
                    }
                },
            )
        self.assertTrue(VolunteerAttendance.objects.filter(user=self.member).exists())
        self.assertFalse(Ledger.objects.filter(user=self.member).exists())

    def test_cap_source_identity_and_self_reaction(self):
        self.receipt(
            key="self-like",
            kind="reaction",
            channel="boost",
            metadata={"reaction": "+", "target_public_key": "a" * 64},
        )
        self.assertEqual(contribution_total(self.member), 0)
        for index in range(5):
            self.receipt(
                key=f"like-{index}",
                kind="reaction",
                channel="boost",
                metadata={"reaction": "+", "target_public_key": "b" * 64},
            )
        self.assertEqual(
            VolunteerRecognition.objects.filter(
                user=self.member, action_key="boost_startup", status="approved"
            ).count(),
            4,
        )
        duplicate = self.receipt(
            key="toggle",
            source_id="like-0",
            kind="reaction",
            channel="boost",
            metadata={"reaction": "+", "target_public_key": "b" * 64},
        )
        self.assertEqual(duplicate.status, "processed")
        self.assertEqual(contribution_total(self.member), microroo("4"))

    def test_verified_source_required_and_repeated_request_keeps_same_record(self):
        with self.assertRaisesMessage(VolunteerError, "source_unavailable"):
            request_recognition(
                self.member,
                dict(
                    action_key="first_channel_contribution",
                    source={"channel_id": "general", "source_id": "fake"},
                    note="Good post",
                ),
            )
        self.receipt(key="post-1", channel="general")
        first, _ = request_recognition(
            self.member,
            dict(
                action_key="first_channel_contribution",
                source={"channel_id": "general", "source_id": "post-1"},
                note="Useful post",
            ),
        )
        second, outcome = request_recognition(
            self.member,
            dict(
                action_key="first_channel_contribution",
                source={"channel_id": "general", "source_id": "post-1"},
                note="Second tap",
            ),
        )
        self.assertEqual(first.pk, second.pk)
        self.assertEqual(outcome, "existing_request")

    def test_intro_and_generic_alias_cannot_stack_or_capture_deferred_intro(self):
        with override_settings(
            COMMUNITY_CHAT_VOLUNTEER_CHANNELS={
                "start_here": "start",
                "general": "start",
            }
        ):
            with override_settings(COMMUNITY_CHAT_VOLUNTEER_AWARDS_ENABLED=False):
                source = self.receipt(key="reserved-intro")
                with self.assertRaisesMessage(
                    VolunteerError, "source_reserved_for_introduction"
                ):
                    request_recognition(
                        self.member,
                        dict(
                            action_key="first_channel_contribution",
                            source=source.source,
                            note="Another category",
                        ),
                    )
            self.assertFalse(
                VolunteerRecognition.objects.filter(user=self.member).exists()
            )
            process_receipt(source)
            with self.assertRaisesMessage(
                VolunteerError, "source_reserved_for_introduction"
            ):
                request_recognition(
                    self.member,
                    dict(
                        action_key="first_channel_contribution",
                        source=source.source,
                        note="Another category",
                    ),
                )
        self.assertEqual(contribution_total(self.member), microroo("4"))
        self.assertEqual(
            VolunteerRecognition.objects.filter(user=self.member).count(), 1
        )

    def test_monthly_and_generic_aliases_are_mutually_exclusive(self):
        with override_settings(
            COMMUNITY_CHAT_VOLUNTEER_CHANNELS={
                "monthly_updates": "monthly",
                "general": "monthly",
            }
        ):
            source = self.receipt(key="one-monthly-post", channel="monthly")
            monthly, _ = request_recognition(
                self.member,
                dict(
                    action_key="monthly_learning_update",
                    source=source.source,
                    note="Published experiment and learning",
                ),
            )
            with self.assertRaisesMessage(VolunteerError, "source_already_classified"):
                request_recognition(
                    self.member,
                    dict(
                        action_key="first_channel_contribution",
                        source=source.source,
                        note="Also generic",
                    ),
                )
            self.approve(monthly, amount="20")
            with self.assertRaisesMessage(VolunteerError, "source_already_classified"):
                request_recognition(
                    self.member,
                    dict(
                        action_key="first_channel_contribution",
                        source=source.source,
                        note="Also generic",
                    ),
                )
        self.assertEqual(contribution_total(self.member), microroo("20"))
        self.assertEqual(
            Ledger.objects.filter(
                user=self.member, reference_type="VOLUNTEER_CONTRIBUTION"
            ).count(),
            1,
        )

    def test_pending_generic_cannot_be_reclassified_as_a_monthly_award(self):
        with override_settings(
            COMMUNITY_CHAT_VOLUNTEER_CHANNELS={
                "monthly_updates": "monthly",
                "general": "monthly",
            }
        ):
            source = self.receipt(key="generic-first", channel="monthly")
            generic, _ = request_recognition(
                self.member,
                dict(
                    action_key="first_channel_contribution",
                    source=source.source,
                    note="Useful post",
                ),
            )
            with self.assertRaisesMessage(VolunteerError, "source_already_classified"):
                request_recognition(
                    self.member,
                    dict(
                        action_key="monthly_learning_update",
                        source=source.source,
                        note="Different category",
                    ),
                )
        generic.refresh_from_db()
        self.assertEqual(generic.status, "pending")
        self.assertEqual(generic.reward_microroo, microroo("1"))
        self.assertFalse(Ledger.objects.filter(user=self.member).exists())

    def test_self_approval_feedback_and_stale_decision_rejected(self):
        self.checked_in(self.reviewer)
        own = self.request_event(user=self.reviewer)
        with self.assertRaisesMessage(VolunteerError, "self_approval_forbidden"):
            self.approve(own)
        self.checked_in()
        record = self.request_event(self.event("event-2"))
        with self.assertRaisesMessage(VolunteerError, "personal_feedback_required"):
            self.approve(record, note="")
        approved = self.approve(record)
        with self.assertRaisesMessage(VolunteerError, "conflict"):
            self.approve(record, key="different-stale")
        self.assertEqual(approved.status, "approved")

    def test_automatic_intro_does_not_consume_human_feedback_requirement(self):
        self.receipt()
        self.checked_in()
        with self.assertRaisesMessage(VolunteerError, "personal_feedback_required"):
            self.approve(self.request_event(), note="")

    def test_resubmit_preserves_request_and_requires_version(self):
        self.checked_in()
        record = self.request_event()
        record, _ = decision(
            record,
            self.reviewer,
            dict(
                decision="needs_update",
                note="Please describe the help",
                version=record.version,
                idempotency_key="feedback",
            ),
        )
        result = revise_request(
            record, self.member, version=record.version, note="I greeted guests"
        )
        self.assertEqual(result.pk, record.pk)
        self.assertEqual(result.status, "pending")
        self.assertEqual(len(result.review_history), 2)

    def test_pending_checklist_never_self_marks_complete(self):
        self.checked_in()
        record = self.request_event()
        action = next(
            item
            for item in journey(self.member)["actions"]
            if item["key"] == "volunteer_event"
        )
        self.assertFalse(action["completed"])
        self.assertFalse(action["eligible"])
        self.assertEqual(action["recognition_status"], "pending")
        self.assertEqual(action["completion_id"], str(record.pk))

    def test_private_api_and_client_privilege_injection(self):
        self.checked_in(self.other)
        record = self.request_event(user=self.other)
        response = self.client.get(
            f"/api/v1/community-chat/volunteer/contributions/{record.pk}/"
        )
        self.assertEqual(response.status_code, 404)
        response = self.client.post(
            "/api/v1/community-chat/volunteer/requests/",
            dict(
                action_key="first_channel_contribution",
                note="test",
                source={},
                idempotency_key="request",
                member_id=self.other.pk,
                points=100,
                approved=True,
            ),
            format="json",
        )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(
            self.client.get(
                "/api/v1/community-chat/volunteer/manage/reviews/"
            ).status_code,
            403,
        )

    def test_restricted_channels_fail_closed(self):
        with self.assertRaisesMessage(VolunteerError, "source_unavailable"):
            self.receipt(key="private", channel="private-internal")
        response = self.client.get("/api/v1/community-chat/volunteer/journey/")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["community_id"], "volunteer-tests")

    def test_service_endpoint_rejects_member_credential(self):
        response = self.client.post(
            "/api/v1/community-chat/volunteer/internal/receipts/", {}, format="json"
        )
        self.assertEqual(response.status_code, 403)

    def test_direct_award_converges_with_member_request(self):
        self.checked_in()
        event = self.event()
        record = self.request_event(event)
        other, _ = request_recognition(
            self.member,
            dict(
                action_key="volunteer_event",
                opportunity_id=event.pk,
                source=event.source,
                note="Direct confirmation",
            ),
            actor=self.reviewer,
        )
        self.assertEqual(other.pk, record.pk)
        self.approve(other)
        self.assertEqual(
            VolunteerRecognition.objects.filter(user=self.member).count(), 1
        )

    def test_receipt_credit_status_and_snapshot_reward(self):
        self.checked_in()
        record = self.approve(self.request_event(), amount="9")
        response = self.client.get(
            f"/api/v1/community-chat/volunteer/contributions/{record.pk}/"
        )
        self.assertEqual(response.data["credit_status"], "credited")
        self.assertEqual(response.data["reward_roo"], "9")
        self.assertEqual(response.data["reward_max_roo"], "18")

    def test_disable_awards_preserves_receipt_and_can_resume(self):
        with override_settings(COMMUNITY_CHAT_VOLUNTEER_AWARDS_ENABLED=False):
            receipt = self.receipt()
            self.assertEqual(receipt.status, "pending")
            self.assertFalse(Ledger.objects.filter(user=self.member).exists())
        receipt = process_receipt(receipt)
        self.assertEqual(receipt.status, "processed")
        self.assertEqual(contribution_total(self.member), microroo("4"))

    def test_request_key_is_bound_to_normalized_input(self):
        self.checked_in()
        event = self.event()
        payload = dict(
            action_key="volunteer_event",
            opportunity_id=event.pk,
            source=event.source,
            note="Helped the host",
            idempotency_key="stable-request-key",
        )
        first, _ = request_recognition(self.member, payload)
        second, outcome = request_recognition(self.member, payload)
        self.assertEqual(first.pk, second.pk)
        self.assertEqual(outcome, "existing_request")
        with self.assertRaisesMessage(VolunteerError, "conflict"):
            request_recognition(self.member, {**payload, "note": "Different intent"})

    def test_invalidated_attendance_cannot_grant_eligibility(self):
        for key in ("invalidated", "service_account"):
            with self.assertRaisesMessage(VolunteerError, "ineligible_source"):
                ingest_receipt(
                    dict(
                        origin="luma",
                        kind="attendance",
                        actor_id=self.member.pk,
                        source_key=key,
                        source={"event_id": key, "source_id": key},
                        occurred_at=(timezone.now() - timedelta(hours=1)).isoformat(),
                        metadata={
                            "checked_in_at": (
                                timezone.now() - timedelta(hours=1)
                            ).isoformat(),
                            key: True,
                        },
                    )
                )
        self.assertFalse(VolunteerAttendance.objects.filter(user=self.member).exists())

    def test_removed_public_channel_is_not_leaked_by_suggestions(self):
        self.checked_in()
        self.event()
        with override_settings(
            COMMUNITY_CHAT_VOLUNTEER_CHANNELS={"start_here": "start"}
        ):
            result = journey(self.member)
        event_action = next(
            item for item in result["actions"] if item["key"] == "volunteer_event"
        )
        self.assertIsNone(event_action["source"])
        self.assertFalse(event_action["eligible"])

    def test_malformed_amount_is_a_client_error_without_credit(self):
        self.checked_in()
        record = self.request_event()
        self.client.force_authenticate(self.reviewer)
        response = self.client.post(
            f"/api/v1/community-chat/volunteer/manage/contributions/{record.pk}/decision/",
            dict(
                decision="approve",
                version=record.version,
                note="Thanks",
                reward_roo="NaN",
                idempotency_key="invalid-amount",
            ),
            format="json",
        )
        self.assertEqual(response.status_code, 400)
        self.assertFalse(Ledger.objects.filter(user=self.member).exists())

    def test_approval_rechecks_source_after_submission(self):
        receipt = self.receipt(key="useful", channel="general")
        record, _ = request_recognition(
            self.member,
            dict(
                action_key="first_channel_contribution",
                source=receipt.source,
                note="A useful post",
            ),
        )
        receipt.metadata = {**receipt.metadata, "invalidated": True}
        receipt.save()
        with self.assertRaisesMessage(VolunteerError, "source_unavailable"):
            self.approve(record, amount="1")

    def test_legacy_intro_and_native_intro_do_not_double_pay_in_either_order(self):
        from community_chat.volunteer.receipts import award_legacy_intro

        self.receipt()
        self.assertFalse(
            award_legacy_intro(self.member, "synthetic-slack-member", "legacy-start")[0]
        )
        self.assertEqual(
            PointsService.get_available_microroo(self.member), microroo("6")
        )
        self.assertTrue(
            award_legacy_intro(self.other, "synthetic-slack-other", "legacy-start")[0]
        )
        receipt = self.receipt(key="other-intro", actor="b")
        self.assertEqual(receipt.status, "processed")
        self.assertEqual(
            PointsService.get_available_microroo(self.other), microroo("6")
        )

    def test_reviewer_deactivation_is_checked_fresh(self):
        self.checked_in()
        record = self.request_event()
        get_user_model().objects.filter(pk=self.reviewer.pk).update(is_active=False)
        with self.assertRaisesMessage(VolunteerError, "not_authorised"):
            self.approve(record)

    def company(self, suffix="one"):
        from organizations.models import Organization
        from founder_tools.models import VibeRaisingCompany, VibeRaisingProfile

        profile, _ = VibeRaisingProfile.objects.get_or_create(
            user=self.member, defaults={"role": VibeRaisingProfile.ROLE_FOUNDER}
        )
        organisation = Organization.objects.create(
            name=f"Synthetic {suffix}", domain=f"{suffix}.example.test"
        )
        return VibeRaisingCompany.objects.create(
            profile=profile,
            organization=organisation,
            name=f"Synthetic {suffix} Pty Ltd",
            registered=True,
            abn="89000000019",
            acn="000000019",
            abr_verified_at=timezone.now(),
        )

    def test_startup_update_keeps_twenty_and_shared_personal_slot(self):
        from roo.services import StartupUpdateRewardService
        from community_chat.volunteer.policy import MELBOURNE

        month = timezone.now().astimezone(MELBOURNE).date().replace(day=1)
        company = self.company()
        self.assertTrue(
            StartupUpdateRewardService.award_monthly_update_completion(
                self.member, company, month
            )
        )
        self.assertFalse(
            StartupUpdateRewardService.award_monthly_update_completion(
                self.member, company, month
            )
        )
        self.assertFalse(
            StartupUpdateRewardService.award_monthly_update_completion(
                self.member, self.company("two"), month
            )
        )
        self.assertEqual(
            Ledger.objects.filter(user=self.member, source="STARTUP_UPDATE").count(), 1
        )
        self.assertEqual(contribution_total(self.member), microroo("20"))
        self.assertEqual(
            PointsService.get_available_microroo(self.member), microroo("29")
        )

    def test_pending_learning_request_converges_with_verified_startup_update(self):
        from roo.services import StartupUpdateRewardService
        from community_chat.volunteer.policy import MELBOURNE

        source = self.receipt(key="learning", channel="monthly")
        record, _ = request_recognition(
            self.member,
            dict(
                action_key="monthly_learning_update",
                source=source.source,
                note="An experiment and result",
            ),
        )
        month = timezone.now().astimezone(MELBOURNE).date().replace(day=1)
        self.assertTrue(
            StartupUpdateRewardService.award_monthly_update_completion(
                self.member, self.company(), month
            )
        )
        record.refresh_from_db()
        self.assertEqual(record.action_key, "monthly_startup_update")
        self.assertEqual(record.status, "approved")
        self.assertEqual(
            VolunteerRecognition.objects.filter(user=self.member).count(), 1
        )

    def test_awards_kill_switch_also_stops_bonus_mirroring(self):
        from community_chat.volunteer.services import award_milestones

        with override_settings(COMMUNITY_CHAT_VOLUNTEER_BONUSES_ENABLED=False):
            self.receipt()
        with override_settings(
            COMMUNITY_CHAT_VOLUNTEER_AWARDS_ENABLED=False,
            COMMUNITY_CHAT_VOLUNTEER_BONUSES_ENABLED=True,
        ):
            award_milestones(self.member)
        self.assertEqual(
            PointsService.get_available_microroo(self.member), microroo("4")
        )
        self.assertFalse(VolunteerMilestone.objects.filter(user=self.member).exists())

    def test_conversations_paginate_distinct_threads_without_hiding_older_replies(self):
        first, second = self.event("older"), self.event("newer")
        now = timezone.now()
        for opportunity, count in ((first, 1), (second, 202)):
            VolunteerSourceReceipt.objects.bulk_create(
                [
                    VolunteerSourceReceipt(
                        community=community_id(),
                        actor=self.member,
                        origin="relay",
                        kind="reply",
                        source_key=f"reply:{opportunity.pk}:{i}",
                        source=opportunity.source,
                        occurred_at=now
                        - timedelta(minutes=(10 if opportunity == first else 1)),
                        status="recorded",
                    )
                    for i in range(count)
                ]
            )
        endpoint = "/api/v1/community-chat/volunteer/contributions/"
        response = self.client.get(endpoint, {"filter": "conversations", "limit": 1})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["results"][0]["opportunity_id"], str(second.pk))
        self.assertIn("filter=conversations", response.data["next"])
        response = self.client.get(endpoint + response.data["next"])
        self.assertEqual(response.data["results"][0]["opportunity_id"], str(first.pk))
        self.assertIsNone(response.data["next"])

    def test_foreign_community_receipt_cannot_be_replayed_or_modified(self):
        receipt = self.receipt()
        receipt.community = "another-community"
        receipt.status = "pending"
        receipt.save()
        with self.assertRaisesMessage(VolunteerError, "source_unavailable"):
            process_receipt(receipt)
        receipt.refresh_from_db()
        self.assertEqual(receipt.status, "pending")

    def test_organiser_attendance_correction_is_recognised_once(self):
        self.client.force_authenticate(self.reviewer)
        payload = dict(
            member_id=self.member.pk,
            event_id="corrected-event",
            checked_in_at=(timezone.now() - timedelta(hours=1)).isoformat(),
            source_id="host-register",
            reason="Host verified the missed check-in",
        )
        endpoint = "/api/v1/community-chat/volunteer/manage/attendance/"
        first = self.client.post(endpoint, payload, format="json")
        second = self.client.post(endpoint, payload, format="json")
        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.status_code, 200)
        self.assertEqual(contribution_total(self.member), microroo("2"))
        self.assertEqual(Ledger.objects.filter(user=self.member).count(), 1)

    def invalidation(self, source_id, actor="a", deletion_kind=5, channel=None):
        return ingest_receipt(
            dict(
                origin="relay",
                kind="invalidation",
                actor_public_key=actor * 64,
                source_key=f"delete:{source_id}:{actor}",
                source={
                    "source_id": source_id,
                    **({"channel_id": channel} if channel else {}),
                },
                occurred_at=timezone.now().isoformat(),
                metadata={"deletion_kind": deletion_kind, "invalidated": True},
            )
        )

    def test_immutable_deletion_stops_pending_review_without_rewriting_source(self):
        source = self.receipt(key="deleted-work", channel="general")
        record, _ = request_recognition(
            self.member,
            dict(
                action_key="first_channel_contribution",
                source=source.source,
                note="Shared a useful result",
            ),
        )
        invalidation = self.invalidation("deleted-work")
        self.assertEqual(invalidation.status, "recorded")
        source.refresh_from_db()
        self.assertNotIn("invalidated", source.metadata)
        with self.assertRaisesMessage(VolunteerError, "source_unavailable"):
            self.approve(record, amount="1")

    def test_deletion_before_objective_retry_denies_credit(self):
        with override_settings(COMMUNITY_CHAT_VOLUNTEER_AWARDS_ENABLED=False):
            source = self.receipt(key="deferred-intro")
        self.invalidation("deferred-intro")
        result = process_receipt(source)
        self.assertEqual(result.status, "ineligible")
        self.assertFalse(Ledger.objects.filter(user=self.member).exists())

    def test_reaction_or_target_deletion_before_retry_denies_like_credit(self):
        for actor in ("a", "b"):
            target_id, reaction_id = f"startup-{actor}", f"reaction-{actor}"
            self.receipt(key=target_id, actor="b", channel="boost")
            with override_settings(COMMUNITY_CHAT_VOLUNTEER_AWARDS_ENABLED=False):
                reaction = self.receipt(
                    key=reaction_id,
                    source_id=target_id,
                    kind="reaction",
                    channel="boost",
                    metadata={"reaction": "+", "target_public_key": "b" * 64},
                )
            self.invalidation(reaction_id if actor == "a" else target_id, actor=actor)
            self.assertEqual(process_receipt(reaction).status, "ineligible")
        self.assertFalse(Ledger.objects.filter(user=self.member).exists())

    def test_unauthorized_deletion_is_terminal_and_cannot_invalidate_work(self):
        source = self.receipt(key="surviving-work", channel="general")
        foreign = self.invalidation("surviving-work", actor="b")
        moderator = self.invalidation(
            "another-target", actor="b", deletion_kind=9005, channel="general"
        )
        self.assertEqual(foreign.status, "ineligible")
        self.assertEqual(moderator.status, "ineligible")
        record, _ = request_recognition(
            self.member,
            dict(
                action_key="first_channel_contribution",
                source=source.source,
                note="Shared a useful result",
            ),
        )
        self.assertEqual(self.approve(record, amount="1").status, "approved")

    def test_historical_opening_cannot_pay_old_bonuses_on_next_new_award(self):
        self.checked_in()
        state = state_for(self.member)
        state.historical_microroo = microroo("10")
        state.save()
        source = self.receipt(key="new-answer", kind="reply", channel="help")
        record, _ = request_recognition(
            self.member,
            dict(
                action_key="helpful_answer",
                source=source.source,
                note="Explained the fix",
            ),
        )
        self.approve(record, amount="3")
        self.assertEqual(contribution_total(self.member), microroo("13"))
        self.assertFalse(VolunteerMilestone.objects.filter(user=self.member).exists())
        current = journey(self.member)["current_level"]
        self.assertFalse(current["bonus_awarded"])
        self.assertFalse(current["bonus_eligible"])
        self.approve(
            self.request_event(self.event("new-threshold")), amount="7", key="cross-20"
        )
        self.assertEqual(contribution_total(self.member), microroo("20"))
        self.assertEqual(
            list(
                VolunteerMilestone.objects.filter(user=self.member).values_list(
                    "level_key", flat=True
                )
            ),
            ["level_3"],
        )
        self.assertEqual(
            PointsService.get_available_microroo(self.member), microroo("14")
        )
