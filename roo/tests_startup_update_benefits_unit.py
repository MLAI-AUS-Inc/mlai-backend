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


@override_settings(COMMUNITY_CHAT_VOLUNTEER_ENABLED=False)
class MonthlyRewardTests(SimpleTestCase):
    def setUp(self):
        from startup_updates import rewards
        self.rewards = rewards
        self.user = SimpleNamespace(pk=23, id=23, slack_id="")
        self.company = verified_company()
        self.draft = SimpleNamespace(
            pk=41, id=41, organization_id=7, month=date(2026, 8, 1),
            published_at=NOW, ready_at=NOW, first_published_at=NOW,
        )
        self.ledger = SimpleNamespace(pk=99, user_id=23, delta_microroo=20000000, created_at=NOW, idempotency_key="legacy")
        self.existing = Mock(return_value=None)
        self.has_previous = Mock(return_value=False)
        self.eligibility = Mock(return_value={"eligible": True})
        self.award = Mock(side_effect=self.credit)
        self.org_lock = Mock()
        for replacement in (
            patch.object(rewards, "update_ledger", self.existing),
            patch.object(rewards, "has_monthly_completion", self.has_previous),
            patch("startup_updates.reward_eligibility.startup_reward_eligibility", self.eligibility),
            patch("roo.services.PointsService.award", self.award),
            patch.object(rewards.Organization.objects, "select_for_update", self.org_lock),
        ):
            replacement.start()
            self.addCleanup(replacement.stop)
        self.locked_draft = patch.object(rewards.MonthlyUpdateDraft.objects, "select_for_update")
        lock = self.locked_draft.start()
        lock.return_value.get.return_value = self.draft
        self.addCleanup(self.locked_draft.stop)

    def credit(self, **kwargs):
        self.ledger.delta_microroo = kwargs["delta"] * 1000000
        self.ledger.idempotency_key = kwargs["idempotency_key"]
        return self.ledger, True

    def complete(self):
        return self.rewards.award_completion.__wrapped__(self.user, self.company, self.draft, newly_approved=True)

    def test_first_verified_approval_gets_twenty_in_completion_month(self):
        result = self.complete()
        self.assertEqual(result, dict(points=20, awarded=True, status="awarded", month="2026-09",
            tier="verified_monthly", creditedToCurrentUser=True))
        self.assertEqual(self.award.call_args.kwargs["delta"], 20)
        self.assertEqual(self.award.call_args.kwargs["reference_id"], "41")
        self.org_lock.return_value.get.assert_called_once_with(pk=7)

    def test_second_new_update_in_month_gets_five(self):
        self.has_previous.return_value = True
        self.draft.pk = 42
        self.assertEqual(self.complete()["points"], 5)
        self.eligibility.assert_not_called()

    def test_unverified_startup_gets_five_without_blocking_approval(self):
        self.eligibility.return_value = {"eligible": False}
        self.assertEqual(self.complete()["points"], 5)

    def test_retry_and_new_revision_do_not_pay_again(self):
        self.existing.return_value = self.ledger
        self.assertEqual(self.complete()["status"], "already_awarded")
        self.draft.month = date(2026, 9, 1)
        self.assertFalse(self.complete()["awarded"])
        self.award.assert_not_called()
        self.eligibility.assert_not_called()

    def test_other_founder_sees_same_receipt_without_personal_credit(self):
        self.existing.return_value = self.ledger
        self.user.pk = 24
        self.assertFalse(self.complete()["creditedToCurrentUser"])
        self.award.assert_not_called()

    def test_reporting_month_change_does_not_change_reward_identity(self):
        key = self.rewards.reward_key(self.draft)
        self.draft.month = date(2026, 1, 1)
        self.assertEqual(self.rewards.reward_key(self.draft), key)

    def test_backdated_reporting_period_does_not_change_calendar_bucket(self):
        self.draft.month = date(2025, 1, 1)
        self.complete()
        self.assertEqual(self.has_previous.call_args.args[1].date(), date(2026, 9, 1))

    def test_old_unpaid_reapproval_cannot_mint_points(self):
        result = self.rewards.award_completion.__wrapped__(self.user, self.company, self.draft)
        self.assertEqual(result["points"], 0)
        self.assertEqual(result["status"], "unavailable")
        self.award.assert_not_called()
        self.eligibility.assert_not_called()

    def test_unapproved_or_foreign_update_is_not_rewarded(self):
        for field, value in (("published_at", None), ("organization_id", 99)):
            original = getattr(self.draft, field)
            setattr(self.draft, field, value)
            with self.assertRaises(ValueError):
                self.complete()
            setattr(self.draft, field, original)
        self.award.assert_not_called()

    def test_no_draft_is_not_a_completion(self):
        self.assertFalse(StartupUpdateRewardService.award_monthly_update_completion(
            self.user, self.company, self.draft.month))

    def test_wallet_failure_is_propagated_for_atomic_approval_rollback(self):
        self.award.side_effect = RuntimeError("Synthetic unavailable wallet")
        with self.assertRaisesRegex(RuntimeError, "unavailable wallet"):
            self.complete()

    def test_calendar_boundary_including_daylight_savings(self):
        before = datetime(2026, 9, 30, 13, 59, tzinfo=timezone.utc)
        after = before + timedelta(minutes=1)
        self.assertEqual(self.rewards.reward_month_bounds(before)[0].month, 9)
        start, end = self.rewards.reward_month_bounds(after)
        self.assertEqual(start.month, 10)
        self.assertEqual(start.utcoffset(), timedelta(hours=10))
        self.assertEqual(end.utcoffset(), timedelta(hours=11))

    def test_approval_month_survives_lookup_crossing_midnight(self):
        self.draft.first_published_at = datetime(2026, 9, 30, 13, 59, tzinfo=timezone.utc)
        self.ledger.created_at = datetime(2026, 9, 30, 14, 1, tzinfo=timezone.utc)
        result = self.complete()
        self.assertEqual(result["points"], 20)
        self.assertEqual(result["month"], "2026-09")
        self.assertTrue(self.award.call_args.kwargs["idempotency_key"].endswith(":month:2026-09"))
        self.assertEqual(self.has_previous.call_args.args[1].month, 9)

    def test_new_year_resets_calendar_month(self):
        start, end = self.rewards.reward_month_bounds(datetime(2026, 12, 31, 13, 0, tzinfo=timezone.utc))
        self.assertEqual(start.date(), date(2027, 1, 1))
        self.assertEqual(end.date(), date(2027, 2, 1))

    def test_historical_nullable_microroo_uses_recorded_legacy_credit(self):
        self.ledger.delta_microroo = None
        self.ledger.delta = 20
        self.ledger.points_delta = None
        self.existing.return_value = self.ledger
        self.assertEqual(self.complete()["points"], 20)
        self.ledger.delta = None
        self.ledger.points_delta = 5
        self.assertEqual(self.complete()["points"], 5)
        self.award.assert_not_called()

    def test_zero_exact_ledger_amount_is_not_replaced_by_legacy_value(self):
        self.ledger.delta_microroo = 0
        self.ledger.delta = 20
        self.existing.return_value = self.ledger
        self.assertEqual(self.complete()["points"], 0)
        self.award.assert_not_called()

    def test_owner_read_uses_actual_ledger_and_never_awards(self):
        self.existing.return_value = self.ledger
        receipt = self.rewards.update_reward_receipt(self.draft, user=self.user)
        self.assertFalse(receipt["awarded"])
        self.assertEqual(receipt["points"], 20)
        self.award.assert_not_called()

    @override_settings(COMMUNITY_CHAT_VOLUNTEER_ENABLED=True, COMMUNITY_CHAT_VOLUNTEER_AWARDS_ENABLED=True)
    def test_volunteer_feature_flag_uses_same_five_point_amount(self):
        self.has_previous.return_value = True
        self.ledger.delta_microroo = 5000000
        self.existing.side_effect = [None, self.ledger]
        with patch("community_chat.volunteer.receipts.award_startup_update", return_value=True) as volunteer:
            result = self.complete()
        self.assertEqual(result["points"], 5)
        self.assertEqual(volunteer.call_args.kwargs["reward_amount"], 5)
        self.assertEqual(volunteer.call_args.kwargs["occurred_at"], NOW)
        self.award.assert_not_called()


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
    def award(self, *, capped=False, existing=False, amount=None):
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
            created = receipts.award_startup_update.__wrapped__(user, company, date(2026, 8, 1), reward_amount=amount)
        return created, receipt, award, mirror

    def test_second_startup_still_receives_payment_after_personal_ranking_cap(self):
        created, receipt, award, mirror = self.award(capped=True)
        self.assertTrue(created)
        self.assertEqual(award.call_args.kwargs["delta"], 20)
        self.assertEqual(receipt.status, "recorded")
        self.assertEqual(receipt.error, "monthly_recognition_cap")
        mirror.assert_not_called()

    def test_additional_update_receives_five_even_after_ranking_cap(self):
        created, receipt, award, mirror = self.award(capped=True, amount=5)
        self.assertTrue(created)
        self.assertEqual(award.call_args.kwargs["delta"], 5)
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


class ApprovalRewardReceiptTests(SimpleTestCase):
    """Exercise the actual approval adapter without a database or network."""

    def setUp(self):
        from vibe_raising import views
        self.views = views
        self.user = SimpleNamespace(pk=23)
        self.company = SimpleNamespace(pk=11)
        self.organization = SimpleNamespace(pk=7)
        self.draft = SimpleNamespace(pk=41, month=date(2026, 8, 1), published_at=None)
        self.request = SimpleNamespace(user=self.user, data={
            "revisionId": 12, "revisionHash": "hash", "audienceVisibility": ["just_me"]})
        self.reward = {"points": 20, "awarded": True, "status": "awarded"}
        self.award = Mock(return_value=self.reward)
        for replacement in (
            patch.object(views, "_get_founder_company_context_or_response", return_value=({"domain": "example.test", "company": self.company}, None)),
            patch.object(views, "_resolve_owned_organization", return_value=self.organization),
            patch("organizations.models.Organization.objects.select_for_update"),
            patch.object(views, "get_object_or_404", return_value=self.draft),
            patch("startup_updates.update_identity.resolve_update", return_value=(self.draft, False)),
            patch("startup_updates.revisions.approve_and_publish", return_value=self.draft),
            patch("startup_updates.rewards.award_completion", self.award),
            patch.object(views, "_serialize_monthly_update", return_value={"id": 41}),
        ):
            replacement.start()
            self.addCleanup(replacement.stop)

    def publish(self):
        return self.views.VibeRaisingMonthlyUpdatePublishView.post.__wrapped__(
            self.views.VibeRaisingMonthlyUpdatePublishView(), self.request, 41)

    def test_actual_credit_receipt_is_returned_to_approver(self):
        response = self.publish()
        self.assertEqual(response.data["reward"], self.reward)
        self.assertEqual(response.data["update"]["reward"], self.reward)
        self.award.assert_called_once_with(self.user, self.company, self.draft, newly_approved=True)

    def test_reapproval_cannot_be_mistaken_for_new_completion(self):
        self.draft.published_at = NOW
        self.publish()
        self.assertFalse(self.award.call_args.kwargs["newly_approved"])

    def test_credit_failure_returns_retryable_approval_error(self):
        self.award.side_effect = RuntimeError("Synthetic wallet unavailable")
        with self.assertRaises(self.views.MonthlyUpdateRewardUnavailable) as error:
            self.publish()
        self.assertEqual(error.exception.status_code, 503)
