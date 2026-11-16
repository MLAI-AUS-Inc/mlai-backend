"""Real account authentication, quotes, point holds, coupon issuance and replay."""

import uuid
from datetime import timedelta
from unittest.mock import patch

from django.test import TestCase, override_settings
from django.utils import timezone
from rest_framework.test import APIClient

from community_chat.account_cookies import ACCESS_COOKIE
from community_chat.account_sessions import issue_account_session
from community_chat.models import CommunityChatEmailCodeChallenge
from integrations.services.luma_event_discounts import LumaEventDiscountError
from roo.models import (
    EventDiscountOffer,
    EventDiscountQuote,
    EventDiscountRedemption,
    Ledger,
    PointsAccount,
)


@override_settings(
    ROO_EVENT_DISCOUNTS_ENABLED=True,
    ROO_EVENT_DISCOUNT_WORKER_ENABLED=True,
    LUMA_API_KEY="synthetic",
    COMMUNITY_CHAT_ALLOWED_ORIGINS=["https://chat.mlai.au"],
)
class MemberPerksTests(TestCase):
    def setUp(self):
        # Reuse the established synthetic provider fixture without inheriting
        # or discovering its separate Slack-service tests in this module.
        from roo.tests_event_discount_api import EventDiscountApiTests

        self.snapshot = EventDiscountApiTests.snapshot.__get__(self)
        EventDiscountApiTests.setUp(self)
        gateway_patch = patch(
            "community_chat.member_perks_views.LumaEventDiscountService",
            return_value=self.gateway,
        )
        gateway_patch.start()
        self.addCleanup(gateway_patch.stop)
        self.reward.cost_points = 32
        self.reward.save()
        self.offer.percent_off = 100
        self.offer.save()
        PointsAccount.objects.filter(user=self.owner).update(
            balance=55,
            earned_balance=50,
            balance_microroo=55_000_000,
            earned_balance_microroo=50_000_000,
        )
        self.owner_token = self.token(self.owner)
        self.other_token = self.token(self.other)
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {self.owner_token}")
        self.url = (
            f"/api/v1/community-chat/event-discounts/events/{self.offer.event_id}/"
        )

    def token(self, user):
        challenge = CommunityChatEmailCodeChallenge.objects.create(
            user=user,
            email_digest="a" * 64,
            code_digest="b" * 64,
            client_id="mlai-chat-web",
            installation_id=uuid.uuid4(),
            origin="https://chat.mlai.au",
            platform="web",
            device_name="Synthetic",
            public_key=("c" if user == self.owner else "d") * 64,
            expires_at=timezone.now() + timedelta(minutes=10),
        )
        return issue_account_session(user, challenge).access_token

    def create_coupon(self, snapshot, code):
        coupon = {
            "id": "coupon-" + code,
            "code": code,
            "remaining_count": 1,
            "event_ticket_type_id": snapshot["event_ticket_type_id"],
            "percent_off": snapshot["discount"]["percent_off"],
            "cents_off": None,
            "currency": None,
            "valid_start_at": None,
            "valid_end_at": snapshot["valid_end_at"],
        }
        self.coupons[code] = coupon
        return coupon

    def quote(self, perk="half_price"):
        response = self.client.post(
            self.url, {"action": "quote", "perk": perk}, format="json"
        )
        self.assertEqual(response.status_code, 201, response.data)
        return response.data

    def confirm(self, quote, **overrides):
        return self.client.post(
            self.url,
            {
                "action": "redeem",
                "quote_id": quote["quote_id"],
                "request_id": "member-perks-" + quote["quote_id"],
                "confirmed": True,
                **overrides,
            },
            format="json",
        )

    def test_options_show_server_prices_without_spending_and_need_no_slack_identity(
        self,
    ):
        self.owner.slack_id = ""
        self.owner.save()
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data["eligible_balance"], 55)
        self.assertEqual(
            [option["points_cost"] for option in response.data["options"]], [16, 32]
        )
        self.assertTrue(
            all(option["can_afford"] for option in response.data["options"])
        )
        self.assertEqual(response["Cache-Control"], "private, no-store")
        self.assertFalse(EventDiscountQuote.objects.exists())
        self.assertFalse(EventDiscountRedemption.objects.exists())
        self.gateway.create_coupon.assert_not_called()
        self.assertTrue(
            self.client.get("/api/v1/community-chat/home/").data["feature_flags"][
                "event_discounts"
            ]
        )

    def test_missing_offer_is_clear_availability_without_spending(self):
        self.offer.delete()
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data["options"], [])
        self.assertEqual(response.data["reason_code"], "offer_unavailable")
        self.assertIn("below A$100", response.data["message"])
        self.assertEqual(response["Cache-Control"], "private, no-store")
        self.gateway.validate_offer.assert_not_called()
        self.assertFalse(EventDiscountQuote.objects.exists())

    def test_expired_offer_is_clear_availability_without_provider_calls(self):
        self.offer.valid_end_at = timezone.now() - timedelta(seconds=1)
        self.offer.save()
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data["options"], [])
        self.assertIn("below A$100", response.data["message"])
        self.gateway.validate_offer.assert_not_called()

    def test_ineligible_live_ticket_is_availability_and_cannot_be_quoted(self):
        self.gateway.validate_offer.side_effect = LumaEventDiscountError(
            "ticket_price_limit", "Roo ticket rewards only cover tickets priced below A$100."
        )
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data["options"], [])
        self.assertEqual(response.data["reason_code"], "ticket_price_limit")
        self.assertIn("below A$100", response.data["message"])
        quoted = self.client.post(
            self.url, {"action": "quote", "perk": "free_ticket"}, format="json"
        )
        self.assertEqual(quoted.status_code, 409, quoted.data)
        self.assertFalse(EventDiscountQuote.objects.exists())
        self.assertFalse(EventDiscountRedemption.objects.exists())
        self.gateway.create_coupon.assert_not_called()

    def test_provider_outage_remains_retryable_without_spending(self):
        self.gateway.validate_offer.side_effect = LumaEventDiscountError(
            "luma_unavailable", "Please try again shortly.", retryable=True
        )
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 503, response.data)
        self.assertEqual(response.data["code"], "luma_unavailable")
        self.assertFalse(EventDiscountQuote.objects.exists())
        self.gateway.create_coupon.assert_not_called()

    def test_half_price_confirm_and_lost_ack_replay_spend_once(self):
        quote = self.quote()
        self.assertEqual(quote["points_cost"], 16)
        self.assertIn("50%", quote["discount_description"])
        first, replay = self.confirm(quote), self.confirm(quote)
        self.assertEqual(first.status_code, 200, first.data)
        self.assertEqual(first.data, replay.data)
        self.assertEqual(first.data["status"], "fulfilled")
        self.assertEqual(first.data["remaining_balance"], 39)
        self.assertIn("coupon=", first.data["checkout_url"])
        self.assertEqual(self.gateway.create_coupon.call_count, 1)
        self.assertEqual(
            self.gateway.create_coupon.call_args.args[0]["discount"]["percent_off"], 50
        )
        self.assertEqual(
            Ledger.objects.filter(user=self.owner, source="EVENT").count(), 1
        )
        saved = self.client.get(self.url)
        self.assertEqual(saved.data["redemption"]["id"], first.data["id"])

    def test_free_ticket_costs_32_and_second_perk_for_same_event_is_blocked(self):
        receipt = self.confirm(self.quote("free_ticket"))
        self.assertEqual(receipt.status_code, 200, receipt.data)
        self.assertEqual(receipt.data["points_cost"], 32)
        self.assertEqual(receipt.data["remaining_balance"], 23)
        response = self.client.post(
            self.url, {"action": "quote", "perk": "half_price"}, format="json"
        )
        self.assertEqual(response.status_code, 409)
        self.assertEqual(self.gateway.create_coupon.call_count, 1)

    def test_low_balance_is_displayed_and_cannot_be_bypassed(self):
        PointsAccount.objects.filter(user=self.owner).update(
            balance=15,
            earned_balance=15,
            purchased_topup_balance=0,
            balance_microroo=15_000_000,
            earned_balance_microroo=15_000_000,
            purchased_topup_balance_microroo=0,
        )
        response = self.client.get(self.url)
        self.assertFalse(
            any(option["can_afford"] for option in response.data["options"])
        )
        response = self.client.post(
            self.url, {"action": "quote", "perk": "half_price"}, format="json"
        )
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.data["code"], "insufficient_points")
        self.assertFalse(EventDiscountRedemption.objects.exists())

    def test_price_identity_and_confirmation_cannot_be_forged(self):
        for values in (
            {"points_cost": 1},
            {"user_id": self.other.pk},
            {"scope": "someone-else"},
        ):
            response = self.client.post(
                self.url,
                {"action": "quote", "perk": "free_ticket", **values},
                format="json",
            )
            self.assertEqual(response.status_code, 400, response.data)
        quote = self.quote()
        for value in (False, "true", 1, None):
            response = self.confirm(quote, confirmed=value)
            self.assertEqual(response.status_code, 400, response.data)
        self.assertFalse(EventDiscountRedemption.objects.exists())

    def test_foreign_account_cannot_confirm_quote_or_read_receipt(self):
        quote = self.quote()
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {self.other_token}")
        self.assertEqual(self.confirm(quote).status_code, 404)
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {self.owner_token}")
        receipt = self.confirm(quote)
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {self.other_token}")
        response = self.client.get(self.url)
        self.assertNotIn(receipt.data["coupon_code"], str(response.data))

    def test_changed_and_expired_quotes_never_spend(self):
        quote = self.quote()
        EventDiscountQuote.objects.filter(pk=quote["quote_id"]).update(
            expires_at=timezone.now() - timedelta(seconds=1)
        )
        self.assertEqual(self.confirm(quote).status_code, 409)
        quote = self.quote()
        self.reward.cost_points = 64
        self.reward.save()
        self.assertEqual(self.confirm(quote).status_code, 409)
        self.assertFalse(EventDiscountRedemption.objects.exists())

    def test_uncertain_coupon_retains_one_hold_and_resumes_same_request(self):
        self.gateway.create_coupon.side_effect = LumaEventDiscountError(
            "timeout", "Synthetic timeout", uncertain=True
        )
        quote = self.quote()
        first, replay = self.confirm(quote), self.confirm(quote)
        self.assertEqual(first.status_code, 202, first.data)
        self.assertEqual(first.data["id"], replay.data["id"])
        self.assertIsNone(first.data["coupon_code"])
        self.assertEqual(first.data["remaining_balance"], 39)
        self.assertEqual(self.gateway.create_coupon.call_count, 1)
        self.assertFalse(
            Ledger.objects.filter(user=self.owner, source="EVENT").exists()
        )

    def test_status_poll_is_account_scoped_and_shows_terminal_failure(self):
        self.gateway.create_coupon.side_effect = LumaEventDiscountError(
            "timeout", "Synthetic timeout", uncertain=True
        )
        pending = self.confirm(self.quote()).data
        row = EventDiscountRedemption.objects.get(pk=pending["id"])
        row.status, row.reserved_microroo = "failed", 0
        row.save()
        reads = self.gateway.validate_offer.call_count
        response = self.client.get(self.url, {"redemption_id": pending["id"]})
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data["redemption"]["status"], "failed")
        self.assertIsNone(response.data["redemption"]["coupon_code"])
        self.assertEqual(self.gateway.validate_offer.call_count, reads)
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {self.other_token}")
        self.assertEqual(
            self.client.get(self.url, {"redemption_id": pending["id"]}).status_code, 404
        )

    def test_cookie_write_requires_the_account_origin(self):
        self.client.credentials()
        self.client.cookies[ACCESS_COOKIE] = self.owner_token
        values = {"action": "quote", "perk": "half_price"}
        for origin in (None, "https://attacker.invalid"):
            response = self.client.post(
                self.url,
                values,
                format="json",
                **({"HTTP_ORIGIN": origin} if origin else {}),
            )
            self.assertIn(response.status_code, (401, 403))
        response = self.client.post(
            self.url, values, format="json", HTTP_ORIGIN="https://chat.mlai.au"
        )
        self.assertEqual(response.status_code, 201, response.data)

    def test_invalid_inputs_and_disabled_recovery_are_safe(self):
        for values in (
            {"action": "quote"},
            {"action": "quote", "perk": []},
            {
                "action": "redeem",
                "confirmed": True,
                "quote_id": "not-a-uuid",
                "request_id": "synthetic",
            },
        ):
            response = self.client.post(self.url, values, format="json")
            self.assertEqual(response.status_code, 400, response.data)
        with override_settings(ROO_EVENT_DISCOUNT_WORKER_ENABLED=False):
            self.assertFalse(self.client.get(self.url).data["enabled"])
            response = self.client.post(
                self.url, {"action": "quote", "perk": "half_price"}, format="json"
            )
            self.assertEqual(response.status_code, 503)
        anonymous = APIClient()
        self.assertIn(anonymous.get(self.url).status_code, (401, 403))


@override_settings(
    ROO_EVENT_DISCOUNTS_ENABLED=True,
    ROO_EVENT_DISCOUNT_WORKER_ENABLED=True,
    LUMA_API_KEY="synthetic",
    COMMUNITY_CHAT_ALLOWED_ORIGINS=["https://chat.mlai.au"],
    ROO_EVENT_PERKS_AUTO="on",
)
class AutomaticMemberPerksTests(TestCase):
    """Any MLAI-hosted event qualifies, priced from its ticket (1 point = A$1)."""

    EVENT = "evt-auto-synthetic"
    # Reuse the fixture helpers without re-running the legacy-priced tests.
    token = MemberPerksTests.token
    create_coupon = MemberPerksTests.create_coupon
    quote = MemberPerksTests.quote
    confirm = MemberPerksTests.confirm

    def setUp(self):
        MemberPerksTests.setUp(self)
        self.url = f"/api/v1/community-chat/event-discounts/events/{self.EVENT}/"
        self.ticket_cents = 4000
        self.gateway.member_perk_ticket.side_effect = lambda event_id, now=None: {
            "event_ticket_type_id": "ttype-auto", "cents": self.ticket_cents, "name": "Synthetic Auto Meetup",
            "valid_end_at": (timezone.now() + timedelta(days=10)).replace(microsecond=0).isoformat(),
        }
        base = self.gateway.validate_offer.side_effect

        def priced(terms, **kwargs):
            if "ticket" in terms and terms["ticket"]["cents"] != self.ticket_cents:
                raise LumaEventDiscountError("ticket_terms_changed", "The ticket price changed.")
            snapshot = base(terms, **kwargs)
            snapshot["ticket"]["cents"] = self.ticket_cents
            return snapshot

        self.gateway.validate_offer.side_effect = priced

    def options(self):
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 200, response.data)
        return response

    def test_mlai_hosted_event_gets_half_and_free_at_50_and_90_percent(self):
        response = self.options()
        self.assertEqual([o["points_cost"] for o in response.data["options"]], [20, 36])
        self.assertEqual(response.data["ticket_price_cents"], 4000)
        offer = EventDiscountOffer.objects.get(event_id=self.EVENT)
        self.assertTrue(offer.reward_id.startswith("EVENT_PERK_"))
        self.assertEqual((offer.event_ticket_type_id, offer.percent_off), ("ttype-auto", 100))
        self.options()
        self.assertEqual(EventDiscountOffer.objects.filter(event_id=self.EVENT).count(), 1)
        self.assertFalse(EventDiscountQuote.objects.exists())

    def test_half_price_spends_20_and_issues_a_single_use_50_percent_code(self):
        quote = self.quote("half_price")
        self.assertEqual(quote["points_cost"], 20)
        receipt = self.confirm(quote)
        self.assertEqual(receipt.status_code, 200, receipt.data)
        self.assertEqual(receipt.data["remaining_balance"], 35)
        sent = self.gateway.create_coupon.call_args.args[0]
        self.assertEqual(sent["discount"]["percent_off"], 50)
        self.assertEqual(sent["event_ticket_type_id"], "ttype-auto")
        self.assertEqual(self.client.post(self.url, {"action": "quote", "perk": "free_ticket"}, format="json").status_code, 409)

    def test_free_ticket_spends_36(self):
        receipt = self.confirm(self.quote("free_ticket"))
        self.assertEqual(receipt.data["points_cost"], 36)
        self.assertEqual(receipt.data["remaining_balance"], 19)
        self.assertEqual(self.gateway.create_coupon.call_args.args[0]["discount"]["percent_off"], 100)

    def test_events_mlai_does_not_host_have_no_perk_and_no_offer(self):
        self.gateway.member_perk_ticket.side_effect = LumaEventDiscountError(
            "event_not_managed", "Member perks only cover events MLAI hosts on its Luma calendar."
        )
        response = self.options()
        self.assertEqual((response.data["options"], response.data["reason_code"]), ([], "event_not_managed"))
        self.assertFalse(EventDiscountOffer.objects.filter(event_id=self.EVENT).exists())
        self.gateway.member_perk_ticket.side_effect = LumaEventDiscountError("luma_rejected", "no", status_code=404)
        self.assertEqual(self.options().data["reason_code"], "event_not_managed")

    def test_price_change_after_quote_releases_the_hold_without_spending(self):
        quote = self.quote("half_price")
        self.ticket_cents = 6000
        receipt = self.confirm(quote)
        self.assertEqual(receipt.data["status"], "failed", receipt.data)
        self.assertEqual(receipt.data["remaining_balance"], 55)
        self.gateway.create_coupon.assert_not_called()
        self.assertFalse(Ledger.objects.filter(user=self.owner, source="EVENT").exists())

    @override_settings(ROO_EVENT_PERKS_AUTO="test", ROO_EVENT_PERKS_TEST_EVENT_IDS=(EVENT,))
    def test_test_mode_is_staff_only_and_listed_events_only(self):
        self.assertEqual(self.options().data["options"], [])
        self.gateway.member_perk_ticket.assert_not_called()
        self.owner.is_staff = True
        self.owner.save()
        self.assertEqual([o["points_cost"] for o in self.options().data["options"]], [20, 36])
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {self.other_token}")
        self.assertEqual(self.options().data["options"], [])
        response = self.client.post(self.url, {"action": "quote", "perk": "half_price"}, format="json")
        self.assertEqual(response.status_code, 404, response.data)

    def test_switching_off_hides_automatic_offers(self):
        self.options()
        with override_settings(ROO_EVENT_PERKS_AUTO="off"):
            self.assertEqual(self.options().data["options"], [])
            response = self.client.post(self.url, {"action": "quote", "perk": "half_price"}, format="json")
            self.assertEqual(response.status_code, 404, response.data)

    @override_settings(SYSTEM_EMAIL_RESEND_MESSAGES=("event_perk_code",))
    def test_issued_code_is_emailed_once(self):
        with patch("system_email.api.send") as send:
            receipt = self.confirm(self.quote("half_price"))
        self.assertEqual(send.call_count, 1)
        args, kwargs = send.call_args
        self.assertEqual(args[0], "event_perk_code")
        self.assertEqual(kwargs["to"], self.owner.email)
        self.assertEqual(kwargs["idempotency_key"], f"event-perk/{receipt.data['id']}")
        self.assertEqual(kwargs["context"]["coupon_code"], receipt.data["coupon_code"])
        self.assertEqual(kwargs["context"]["points_cost"], 20)
        self.assertEqual(kwargs["context"]["perk_label"], "Half-price ticket")

    @override_settings(SYSTEM_EMAIL_RESEND_MESSAGES=("event_perk_code",))
    def test_email_failure_never_affects_the_code_or_points(self):
        with patch("system_email.api.send", side_effect=RuntimeError("synthetic")):
            receipt = self.confirm(self.quote("half_price"))
        self.assertEqual(receipt.data["status"], "fulfilled")
        self.assertEqual(receipt.data["remaining_balance"], 35)

    def test_cancel_disables_the_unused_code_and_refunds_exactly(self):
        from django.core.management import call_command
        from django.core.management.base import CommandError

        receipt = self.confirm(self.quote("half_price")).data

        def disable(event_id, code):
            self.coupons[code]["remaining_count"] = 0

        self.gateway.disable_coupon.side_effect = disable
        with patch("roo.management.commands.cancel_event_perk.LumaEventDiscountService", return_value=self.gateway):
            call_command("cancel_event_perk", "--redemption", receipt["id"])
            self.assertEqual(EventDiscountRedemption.objects.get(pk=receipt["id"]).status, "fulfilled")
            call_command("cancel_event_perk", "--redemption", receipt["id"], "--apply")
            with self.assertRaises(CommandError):
                call_command("cancel_event_perk", "--redemption", receipt["id"], "--apply")
        self.assertEqual(EventDiscountRedemption.objects.get(pk=receipt["id"]).status, "cancelled")
        self.assertEqual(self.client.get(self.url).data["eligible_balance"], 55)
        self.assertEqual(Ledger.objects.filter(user=self.owner, kind="REFUND", source="EVENT").count(), 1)
