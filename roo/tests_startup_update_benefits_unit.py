"""Monthly-update benefit regressions without database construction or network."""
from contextlib import nullcontext
from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import Mock, patch

from django.test import SimpleTestCase, override_settings

from roo.services import CoworkingService, StartupUpdateRewardService


NOW = datetime(2026, 9, 27, 2, 0, tzinfo=timezone.utc)


def verified_company(**overrides):
    return SimpleNamespace(**{
        "id": 11, "pk": 11, "organization_id": 7,
        "registered": True, "abn": "89000000019", "acn": None,
        "entity_type_code": "OIE", "abr_verified_at": NOW - timedelta(days=50),
        **overrides,
    })


class ApprovedUpdateDiscountTests(SimpleTestCase):
    def setUp(self):
        self.company = verified_company()
        self.draft = SimpleNamespace(
            organization_id=7, status="ready", ready_at=NOW - timedelta(days=29),
            published_at=NOW - timedelta(days=29),
        )
        self.clock = patch("roo.services.timezone.now", return_value=NOW)
        self.clock.start()
        self.addCleanup(self.clock.stop)
        self.bindings = Mock()
        self.bindings.filter.return_value.values_list.return_value = [7]

    def discount(self, booking_date=None):
        def matching_drafts(**conditions):
            row = self.draft
            matches = row.organization_id in conditions["organization_id__in"]
            matches &= (row.published_at is None) == conditions["published_at__isnull"]
            # Deliberately honour a status filter if reintroduced: editing an
            # already-approved update must retain its earned benefit.
            matches &= "status" not in conditions or conditions["status"] == row.status
            query = Mock()
            query.annotate.return_value.filter.return_value = [row] if matches else []
            return query

        with (
            patch("core.slack_founder_links.coworking_eligibility_user_ids", return_value=[23]),
            patch("startup_updates.models.UserStartupBinding.objects.filter", return_value=self.bindings),
            patch("founder_tools.models.VibeRaisingCompany.objects.filter", return_value=[self.company]),
            patch("startup_updates.models.MonthlyUpdateDraft.objects.filter", side_effect=matching_drafts),
            patch("startup_updates.benefits.approved_update_at", side_effect=lambda row: row.ready_at),
            patch.object(CoworkingService, "get_standard_coworking_cost", return_value=8),
        ):
            return CoworkingService.get_coworking_cost(
                user=SimpleNamespace(pk=23), booking_date=booking_date or date(2026, 9, 27),
            )

    def test_abr_verified_nonprofit_without_acn_gets_four_point_price(self):
        self.assertEqual(self.discount(), 4)

    def test_unverified_or_invalid_abn_gets_eight_point_price(self):
        for changes in ({"abr_verified_at": None}, {"registered": False}, {"abn": "12345678901"}):
            with self.subTest(changes=changes):
                self.company = verified_company(**changes)
                self.assertEqual(self.discount(), 8)

    def test_generated_ready_update_without_approval_gets_no_discount(self):
        self.draft.published_at = None
        self.assertEqual(self.discount(), 8)

    def test_editing_approved_update_preserves_discount(self):
        self.draft.status = "draft"
        self.assertEqual(self.discount(), 4)

    def test_discount_expires_at_exact_thirtieth_day(self):
        self.draft.ready_at = NOW - timedelta(days=30) + timedelta(microseconds=1)
        self.assertEqual(self.discount(), 4)
        self.draft.ready_at -= timedelta(microseconds=1)
        self.assertEqual(self.discount(), 8)

    def test_future_approval_cannot_unlock_discount(self):
        self.draft.ready_at = NOW + timedelta(seconds=1)
        self.assertEqual(self.discount(), 8)

    def test_missing_approval_timestamp_cannot_unlock_discount(self):
        self.draft.ready_at = None
        self.assertEqual(self.discount(), 8)

    def test_advance_booking_outside_window_has_full_price(self):
        self.assertEqual(self.discount(date(2026, 9, 29)), 8)

    def test_past_booking_cannot_use_current_benefit(self):
        self.assertEqual(self.discount(date(2026, 9, 26)), 8)

    def test_window_crossing_daylight_savings_uses_elapsed_time(self):
        # 5 October begins at 13:00 UTC on 4 October after Melbourne DST starts.
        expiry = datetime(2026, 10, 4, 13, 0, tzinfo=timezone.utc)
        self.draft.ready_at = expiry - timedelta(days=30)
        self.assertEqual(self.discount(date(2026, 10, 5)), 8)
        self.draft.ready_at += timedelta(microseconds=1)
        self.assertEqual(self.discount(date(2026, 10, 5)), 4)

    def test_no_eligible_founder_binding_has_no_discount(self):
        self.bindings.filter.return_value.values_list.return_value = []
        self.assertEqual(self.discount(), 8)


@override_settings(COMMUNITY_CHAT_VOLUNTEER_ENABLED=False, ROO_POINTS_MONTHLY_UPDATE_REWARD=20)
class MonthlyRewardTests(SimpleTestCase):
    def setUp(self):
        self.user = SimpleNamespace(pk=23, id=23, slack_id="")
        self.company = verified_company()
        self.draft = SimpleNamespace(
            pk=41, id=41, organization_id=7, month=date(2026, 8, 1),
            published_at=NOW, ready_at=NOW, current_revision_id=100, save=Mock(),
        )
        self.award = Mock(return_value=(SimpleNamespace(pk=99), True))
        self.duplicate = Mock()
        self.duplicate.first.return_value = None
        for replacement in (
            patch("roo.services.transaction.atomic", side_effect=lambda: nullcontext()),
            patch("roo.services.PointsService.award", self.award),
            patch("startup_updates.benefits.monthly_reward_history", return_value=self.duplicate),
            patch("startup_updates.models.MonthlyUpdateDraft.objects.select_for_update"),
            patch("roo.services.timezone.now", return_value=NOW),
        ):
            replacement.start()
            self.addCleanup(replacement.stop)

    def complete(self, **kwargs):
        return StartupUpdateRewardService.award_monthly_update_completion(
            self.user, self.company, self.draft.month, self.draft, **kwargs,
        )

    def test_approved_nonprofit_receives_twenty_points(self):
        self.assertTrue(self.complete())
        self.assertEqual(self.award.call_args.kwargs["delta"], 20)
        self.assertEqual(self.award.call_args.kwargs["reference_id"], "41")

    def test_duplicate_and_new_revision_use_the_same_company_month_key(self):
        self.assertTrue(self.complete())
        key = self.award.call_args.kwargs["idempotency_key"]
        self.award.return_value = (SimpleNamespace(pk=99), False)
        self.draft.current_revision_id = 101
        self.assertFalse(self.complete())
        self.assertEqual(self.award.call_args.kwargs["idempotency_key"], key)
        self.assertEqual(key, "monthly_update_reward:organization:7:2026-08")

    def test_next_reporting_month_gets_a_new_reward_key(self):
        self.complete()
        key = self.award.call_args.kwargs["idempotency_key"]
        self.draft.month = date(2026, 9, 1)
        self.complete()
        self.assertNotEqual(self.award.call_args.kwargs["idempotency_key"], key)

    def test_unapproved_update_is_not_rewarded(self):
        self.draft.published_at = None
        self.assertFalse(self.complete())
        self.award.assert_not_called()

    def test_foreign_startup_draft_is_not_rewarded(self):
        self.draft.organization_id = 8
        self.assertFalse(self.complete())
        self.award.assert_not_called()

    def test_unverified_company_is_not_rewarded(self):
        self.company.abr_verified_at = None
        self.assertFalse(self.complete())
        self.award.assert_not_called()

    def test_future_month_is_not_rewarded(self):
        self.draft.month = date(2026, 10, 1)
        self.assertFalse(self.complete())
        self.award.assert_not_called()

    def test_non_month_bucket_is_not_rewarded(self):
        self.draft.month = date(2026, 9, 2)
        self.assertFalse(self.complete())
        self.award.assert_not_called()

    def test_second_company_wrapper_cannot_reward_same_startup_month(self):
        self.duplicate.first.return_value = SimpleNamespace(
            idempotency_key="monthly_update_reward:11:2026-08",
            reference_id="41", user_id=24,
        )
        self.assertFalse(self.complete())
        self.award.assert_not_called()

    def test_recreated_draft_preserves_original_paid_window_without_another_credit(self):
        original = NOW - timedelta(days=40)
        self.duplicate.first.return_value = SimpleNamespace(
            idempotency_key="monthly_update_reward:11:2026-08",
            reference_id="old-deleted-draft", user_id=23, created_at=original,
        )
        self.assertFalse(self.complete(strict=True))
        self.assertEqual(self.draft.ready_at, original)
        self.award.assert_not_called()

    def test_legacy_company_month_payment_reuses_its_existing_key(self):
        key = "monthly_update_reward:11:2026-08"
        self.duplicate.first.return_value = SimpleNamespace(
            idempotency_key=key, reference_id="41", user_id=23,
        )
        self.award.return_value = (SimpleNamespace(pk=99), False)
        self.assertFalse(self.complete(strict=True))
        self.assertEqual(self.award.call_args.kwargs["idempotency_key"], key)

    def test_strict_approval_does_not_hide_payment_failure(self):
        self.award.side_effect = RuntimeError("Synthetic unavailable wallet")
        with self.assertLogs("roo.services", level="ERROR"):
            with self.assertRaisesRegex(RuntimeError, "unavailable wallet"):
                self.complete(strict=True)

    def test_legacy_best_effort_caller_returns_false_on_failure(self):
        self.award.side_effect = RuntimeError("Synthetic unavailable wallet")
        with self.assertLogs("roo.services", level="ERROR"):
            self.assertFalse(self.complete())


class FirstApprovalClockTests(SimpleTestCase):
    def publish(self, draft):
        from startup_updates.revisions import approve_and_publish
        with (
            patch("startup_updates.revisions.MonthlyUpdateDraft.objects.select_for_update") as locked,
            patch("startup_updates.revisions.MonthlyUpdateApproval.objects.get_or_create", return_value=(Mock(), True)),
            patch("startup_updates.revisions.timezone.now", return_value=NOW),
        ):
            locked.return_value.get.return_value = draft
            # Skip only the database transaction wrapper; exercise approval,
            # disclosure checks and timestamp changes in the real function.
            return approve_and_publish.__wrapped__(draft, actor=Mock(), revision_id=101,
                revision_hash="reviewed-hash", audience_visibility=["just_me"])

    def draft(self, **kwargs):
        revision = SimpleNamespace(pk=101, content_hash="reviewed-hash",
            snapshot=SimpleNamespace(payload={}),
            structured_memo={"_audience_visibility": ["just_me"]},
            validation={"groundedness_status": "founder_asserted"})
        return SimpleNamespace(**{
            "pk": 41, "month": date(2026, 8, 1), "current_revision": revision, "published_revision_id": None,
            "published_at": None, "first_published_at": None, "ready_at": None, "save": Mock(), **kwargs,
        })

    def test_first_human_approval_replaces_provisional_generation_clock(self):
        draft = self.draft(ready_at=NOW - timedelta(days=45))
        self.publish(draft)
        self.assertEqual(draft.ready_at, NOW)
        self.assertIn("ready_at", draft.save.call_args.kwargs["update_fields"])

    def test_future_month_cannot_be_approved(self):
        from rest_framework.exceptions import ValidationError

        draft = self.draft(month=date(2026, 10, 1))
        with self.assertRaisesRegex(ValidationError, "future month"):
            self.publish(draft)
        draft.save.assert_not_called()

    def test_reapproval_does_not_restart_window(self):
        first = NOW - timedelta(days=29)
        draft = self.draft(published_revision_id=101, published_at=first, ready_at=first)
        self.publish(draft)
        self.assertEqual(draft.ready_at, first)

        self.assertEqual(draft.published_at, first)

    def test_new_revision_does_not_restart_window(self):
        first = NOW - timedelta(days=29)
        draft = self.draft(published_revision_id=99, published_at=first, ready_at=first)
        self.publish(draft)
        self.assertEqual(draft.ready_at, first)
        self.assertEqual(draft.published_at, NOW)

    def test_legacy_publication_without_clock_keeps_historical_approval(self):
        first = NOW - timedelta(days=50)
        draft = self.draft(published_at=first)
        self.publish(draft)
        self.assertEqual(draft.ready_at, first)


class VolunteerMonthlyWalletTests(SimpleTestCase):
    def award(self, *, capped=False, existing=False):
        from community_chat.volunteer import receipts
        from community_chat.volunteer.access import VolunteerError

        company_type = type("CompanyStub", (), {"objects": Mock()})
        company = company_type()
        company.pk = 11
        user = SimpleNamespace(pk=23, slack_id="")
        ledger = SimpleNamespace(pk=99, user_id=23)
        receipt = SimpleNamespace(
            status="recorded" if existing and capped else "pending",
            error="monthly_recognition_cap" if existing and capped else "",
            save=Mock(),
        )
        with (
            patch.object(receipts, "lock_member", return_value=user),
            patch.object(receipts, "state_for"),
            patch.object(receipts, "active_policy", return_value={"monthly_startup_update": {"key": "monthly_startup_update"}}),
            patch.object(receipts, "enforce_cap", side_effect=VolunteerError("cap_reached") if capped else None),
            patch.object(receipts.Ledger.objects, "filter") as history,
            patch.object(receipts.PointsService, "award", return_value=(ledger, True)) as award,
            patch.object(receipts.VolunteerSourceReceipt.objects, "get_or_create", return_value=(receipt, not existing)),
            patch.object(receipts, "_mirror_monthly_receipt") as mirror,
            patch.object(receipts, "community_id", return_value="test-community"),
            patch.object(receipts.transaction, "atomic", side_effect=lambda: nullcontext()),
        ):
            history.return_value.first.return_value = ledger if existing else None
            created = receipts.award_startup_update.__wrapped__(user, company, date(2026, 8, 1))
        return created, receipt, award, mirror

    def test_second_startup_still_receives_payment_after_personal_ranking_cap(self):
        created, receipt, award, mirror = self.award(capped=True)
        self.assertTrue(created)
        self.assertEqual(award.call_args.kwargs["delta"], 20)
        self.assertEqual(receipt.status, "recorded")
        self.assertEqual(receipt.error, "monthly_recognition_cap")
        mirror.assert_not_called()

    def test_retry_capped_startup_neither_pays_nor_mirrors_twice(self):
        created, receipt, award, mirror = self.award(capped=True, existing=True)
        self.assertFalse(created)
        award.assert_not_called()
        mirror.assert_not_called()

    def test_first_monthly_payment_mirrors_existing_ledger(self):
        created, receipt, award, mirror = self.award()
        self.assertTrue(created)
        award.assert_called_once()
        mirror.assert_called_once()


class HistoricalApprovalAnchorTests(SimpleTestCase):
    def test_approval_annotation_resolves_real_model_fields_without_database(self):
        from django.db.models import Min
        from startup_updates.models import MonthlyUpdateDraft

        query = MonthlyUpdateDraft.objects.annotate(first_approved_at=Min("revisions__approval__approved_at"))
        self.assertIn("first_approved_at", query.query.annotations)

    def anchor(self, *, first=None, original=None):
        from startup_updates.benefits import approved_update_at

        draft = SimpleNamespace(pk=41, organization_id=7, month=date(2026, 8, 1),
            published_at=NOW, ready_at=NOW - timedelta(days=45), first_approved_at=first)
        with patch("startup_updates.benefits.monthly_reward_history") as history:
            history.return_value.exclude.return_value.first.return_value = (
                SimpleNamespace(created_at=original) if original else None
            )
            return approved_update_at(draft)

    def test_real_approval_overrides_old_generation_timestamp(self):
        first = NOW - timedelta(days=4)
        self.assertEqual(self.anchor(first=first), first)

    def test_legacy_publication_without_revision_retains_recorded_timestamp(self):
        self.assertEqual(self.anchor(), NOW - timedelta(days=45))

    def test_deleted_and_recreated_month_cannot_renew_original_reward_window(self):
        original = NOW - timedelta(days=40)
        self.assertEqual(self.anchor(first=NOW, original=original), original)
