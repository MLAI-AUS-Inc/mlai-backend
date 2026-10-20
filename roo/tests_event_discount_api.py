"""Actual HTTP trust boundaries and persisted event-redemption API workflows.

Only Luma is mocked. Run through scripts/test_event_discounts.py against the
specifically approved disposable migration graph, never a configured database.
"""

from datetime import datetime, timedelta
import hashlib
from unittest.mock import Mock, patch

from django.core.cache import cache
from django.test import TestCase, override_settings
from django.utils import timezone
from rest_framework.test import APIClient
from rest_framework_simplejwt.tokens import AccessToken

from core.models import User
from integrations.services.luma_event_discounts import (
    LumaEventDiscountError,
    LumaEventDiscountService,
)
from roo.models import (
    EventDiscountOffer,
    EventDiscountQuote,
    EventDiscountRedemption,
    Ledger,
    PointsAccount,
    RewardsCatalog,
)
from roo.services import PointsService


ROOT = "/api/v1/points/event-discounts/"
CHAT = "/api/v1/community-chat/event-discounts/"
ROO_KEY = "synthetic-roo-isolated-service-key"
INTERNAL_KEY = "synthetic-internal-admin-key"
MLAI_KEY = "synthetic-legacy-service-key"
IDENTITY = {
    "slack_user_id": "UEVENTOWNER",
    "slack_team_id": "TTEST",
    "slack_channel_id": "DTEST",
    "slack_thread_ts": "1792454400.000100",
}


@override_settings(
    ROO_EVENT_DISCOUNTS_ENABLED=True,
    ROO_EVENT_DISCOUNTS_SLACK_TEAM_ID="TTEST",
    ROO_API_KEY=ROO_KEY,
    INTERNAL_API_KEY=INTERNAL_KEY,
    MLAI_API_KEY=MLAI_KEY,
    LUMA_API_KEY="synthetic-provider-key",
)
class EventDiscountApiTests(TestCase):
    """Exercise resolved URLs, DRF permissions, domain services and the real DB."""

    def setUp(self):
        cache.clear()
        self.addCleanup(cache.clear)
        self.client = APIClient()
        self.owner = User.objects.create_user(
            email="api-event-owner@example.invalid", slack_id=IDENTITY["slack_user_id"],
        )
        self.other = User.objects.create_user(
            email="api-event-other@example.invalid", slack_id="UEVENTOTHER",
        )
        self.admin = User.objects.create_superuser(
            email="api-event-admin@example.invalid", password="synthetic-test-only-password",
            slack_id="UEVENTADMIN",
        )
        for user in (self.owner, self.other, self.admin):
            PointsAccount.objects.create(
                user=user, balance=25, earned_balance=20, purchased_topup_balance=5,
                balance_microroo=25_000_000, earned_balance_microroo=20_000_000,
                purchased_topup_balance_microroo=5_000_000, microroo_initialized=True,
            )
        self.reward = RewardsCatalog.objects.create(
            code="api-synthetic-event", name="Synthetic API Meetup discount",
            description="Synthetic API test fixture", cost_points=10,
            fulfillment="auto", is_active=True, stock_remaining=5, max_per_user=1,
        )
        self.offer = EventDiscountOffer.objects.create(
            reward=self.reward, event_id="evt-api-synthetic",
            event_ticket_type_id="ttype-api-synthetic", discount_type="percent",
            percent_off=20, currency="aud", is_active=True,
            valid_end_at=timezone.now() + timedelta(days=14),
        )
        self.gateway = Mock(spec=LumaEventDiscountService)
        self.gateway.validate_offer.side_effect = self.snapshot
        self.coupons = {}
        self.gateway.find_coupon.side_effect = lambda event_id, code: self.coupons.get(code)
        self.gateway.matches_coupon.side_effect = LumaEventDiscountService.matches_coupon
        self.gateway.create_coupon.side_effect = self.create_coupon
        gateway_patch = patch("roo.event_discounts.LumaEventDiscountService", return_value=self.gateway)
        gateway_class = gateway_patch.start()
        gateway_class.coupon_url.side_effect = LumaEventDiscountService.coupon_url
        self.addCleanup(gateway_patch.stop)

    def snapshot(self, terms, **kwargs):
        expiry = datetime.fromisoformat(terms["valid_end_at"].replace("Z", "+00:00"))
        expiry = expiry.replace(microsecond=(expiry.microsecond // 1000) * 1000)
        return {
            **terms, "id": terms["event_id"], "name": "Synthetic API Meetup",
            "valid_end_at": expiry.isoformat(),
            "url": "https://luma.com/api-synthetic-meetup", "timezone": "Australia/Sydney",
            "starts_at": (timezone.now() + timedelta(days=15)).isoformat(),
            "ticket": {"id": terms["event_ticket_type_id"], "name": "General", "cents": 5000, "currency": "aud"},
        }

    def create_coupon(self, snapshot, code):
        coupon = {
            "id": "coupon-" + code, "code": code, "remaining_count": 1,
            "event_ticket_type_id": snapshot["event_ticket_type_id"],
            "percent_off": 20, "cents_off": None, "currency": None,
            "valid_start_at": None, "valid_end_at": snapshot["valid_end_at"],
        }
        self.coupons[code] = coupon
        return coupon

    def post(self, suffix, values, *, key=ROO_KEY, **headers):
        if key is not None:
            headers["HTTP_X_API_KEY"] = key
        return self.client.post(ROOT + suffix, values, format="json", **headers)

    def get(self, suffix, values=None, *, key=ROO_KEY, **headers):
        if key is not None:
            headers["HTTP_X_API_KEY"] = key
        return self.client.get(ROOT + suffix, values or IDENTITY, **headers)

    def quote(self, *, identity=None):
        response = self.post("quote/", {
            **(identity or IDENTITY), "event_query": self.offer.event_id,
        })
        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual(response["Cache-Control"], "private, no-store")
        return response.data

    def confirmation(self, quote, *, identity=None):
        identity = identity or IDENTITY
        digest = hashlib.sha256(
            f"{identity['slack_team_id']}:{identity['slack_user_id']}:{quote['quote_id']}".encode()
        ).hexdigest()
        return {
            **identity, "quote_id": quote["quote_id"], "confirmed": True,
            "request_id": "roo-event-discount-" + digest,
        }

    def redeem(self, quote, *, identity=None):
        values = self.confirmation(quote, identity=identity)
        return self.post("redeem/", values, HTTP_IDEMPOTENCY_KEY=values["request_id"])

    def fulfilled(self, *, identity=None):
        quote = self.quote(identity=identity)
        response = self.redeem(quote, identity=identity)
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data["status"], "fulfilled")
        self.assertEqual(response["Cache-Control"], "private, no-store")
        return quote, response.data

    def assert_no_mutation(self):
        self.assertFalse(EventDiscountRedemption.objects.exists())
        self.assertFalse(Ledger.objects.exists())
        self.gateway.create_coupon.assert_not_called()
        account = PointsAccount.objects.get(user=self.owner)
        self.assertEqual(account.balance_microroo, 25_000_000)

    def test_http_quote_confirm_receipt_and_history_are_exactly_once(self):
        quote = self.quote()
        self.assertEqual(quote["event"]["id"], self.offer.event_id)
        self.assertEqual(quote["points_cost"], 10)
        self.assertEqual(quote["eligible_balance"], 25)
        self.assert_no_mutation()
        first = self.redeem(quote)
        self.assertEqual(first.status_code, 200, first.data)
        self.assertEqual(first.data["status"], "fulfilled")
        self.assertRegex(first.data["coupon_code"], r"^ROO-[A-Z2-7]{16}$")
        self.assertEqual(first.data["checkout_url"],
                         "https://luma.com/api-synthetic-meetup?coupon=" + first.data["coupon_code"])
        self.assertEqual(first.data["remaining_balance"], 15)
        second = self.redeem(quote)
        self.assertEqual(second.status_code, 200, second.data)
        self.assertEqual(first.data["id"], second.data["id"])
        self.assertEqual(first.data["checkout_url"], second.data["checkout_url"])
        self.assertEqual(EventDiscountRedemption.objects.count(), 1)
        self.assertEqual(Ledger.objects.count(), 1)
        self.gateway.create_coupon.assert_called_once()
        self.assertEqual(PointsAccount.objects.get(user=self.owner).balance_microroo, 15_000_000)
        history = self.get("redemptions/")
        self.assertEqual(history.status_code, 200, history.data)
        self.assertEqual([row["id"] for row in history.data["redemptions"]], [first.data["id"]])
        detail = self.get(f"redemptions/{first.data['id']}/")
        self.assertEqual(detail.status_code, 200, detail.data)
        self.assertEqual(detail.data["coupon_code"], first.data["coupon_code"])
        for response in (first, second, history, detail):
            self.assertEqual(response["Cache-Control"], "private, no-store")

    def test_only_isolated_roo_key_can_quote_or_read_history(self):
        for key in (None, "invalid", INTERNAL_KEY, MLAI_KEY):
            with self.subTest(key=key):
                response = self.post("quote/", {**IDENTITY, "event_query": self.offer.event_id}, key=key)
                self.assertEqual(response.status_code, 403)
                self.assertEqual(response["Cache-Control"], "private, no-store")
                response = self.get("redemptions/", key=key)
                self.assertEqual(response.status_code, 403)
        self.assertFalse(EventDiscountQuote.objects.exists())
        self.assert_no_mutation()

    def test_admin_account_jwt_cannot_replace_roo_service_credential(self):
        token = str(AccessToken.for_user(self.admin))
        response = self.post("quote/", {**IDENTITY, "event_query": self.offer.event_id},
                             key=None, HTTP_AUTHORIZATION="Bearer " + token)
        self.assertEqual(response.status_code, 403)
        self.assert_no_mutation()

    def test_aliased_roo_internal_or_legacy_credentials_fail_closed(self):
        for aliases in ({"INTERNAL_API_KEY": ROO_KEY}, {"MLAI_API_KEY": ROO_KEY},
                        {"INTERNAL_API_KEY": ""}):
            with self.subTest(aliases=aliases), override_settings(**aliases):
                response = self.post("quote/", {**IDENTITY, "event_query": self.offer.event_id})
                self.assertEqual(response.status_code, 403)
        self.assertFalse(EventDiscountQuote.objects.exists())
        self.assert_no_mutation()

    def test_service_target_identity_overrides_are_rejected(self):
        for key in ("user_id", "target_user_id", "target_slack_user_id"):
            with self.subTest(key=key):
                response = self.post("quote/", {
                    **IDENTITY, "event_query": self.offer.event_id, key: self.other.pk,
                })
                self.assertEqual(response.status_code, 403)
                self.assertEqual(response.data["code"], "invalid_actor")
        self.assert_no_mutation()

    def test_unlinked_and_inactive_slack_actors_cannot_issue_quotes(self):
        self.other.is_active = False
        self.other.save(update_fields=("is_active",))
        for actor in ("UNOTLINKED", self.other.slack_id, "<@UEVENTOWNER>", ""):
            with self.subTest(actor=actor):
                response = self.post("quote/", {
                    **IDENTITY, "slack_user_id": actor, "event_query": self.offer.event_id,
                })
                self.assertEqual(response.status_code, 403)
        self.assert_no_mutation()

    def test_public_channel_or_wrong_workspace_cannot_quote(self):
        for changes in ({"slack_channel_id": "CPUBLIC"}, {"slack_team_id": "TOTHER"},
                        {"slack_thread_ts": "not-a-slack-timestamp"}):
            with self.subTest(changes=changes):
                response = self.post("quote/", {**IDENTITY, **changes, "event_query": self.offer.event_id})
                self.assertEqual(response.status_code, 403)
                self.assertEqual(response.data["code"], "private_conversation_required")
        self.assert_no_mutation()

    def test_confirmation_requires_json_true_not_truthy_values(self):
        quote = self.quote()
        for confirmation in (None, False, "true", "yes", 1, 0, [], {}):
            with self.subTest(confirmation=confirmation):
                values = {**self.confirmation(quote), "confirmed": confirmation}
                response = self.post("redeem/", values, HTTP_IDEMPOTENCY_KEY=values["request_id"])
                self.assertEqual(response.status_code, 400, response.data)
                self.assertEqual(response.data["code"], "confirmation_required")
        self.assert_no_mutation()

    def test_idempotency_header_must_exactly_match_body_request_id(self):
        quote = self.quote()
        values = self.confirmation(quote)
        for header in (None, "another-operation"):
            with self.subTest(header=header):
                headers = {"HTTP_IDEMPOTENCY_KEY": header} if header else {}
                response = self.post("redeem/", values, **headers)
                self.assertEqual(response.status_code, 400)
                self.assertEqual(response.data["code"], "invalid_request_id")
        self.assert_no_mutation()

    def test_other_private_thread_or_channel_cannot_confirm_quote(self):
        quote = self.quote()
        for changes in ({"slack_channel_id": "DOTHER"},
                        {"slack_thread_ts": "1792454400.000200"}):
            with self.subTest(changes=changes):
                response = self.redeem(quote, identity={**IDENTITY, **changes})
                self.assertEqual(response.status_code, 403)
                self.assertEqual(response.data["code"], "quote_scope_mismatch")
        self.assert_no_mutation()

    def test_foreign_member_cannot_confirm_quote_or_read_receipt(self):
        quote = self.quote()
        foreign = {**IDENTITY, "slack_user_id": self.other.slack_id}
        denied = self.redeem(quote, identity=foreign)
        self.assertEqual(denied.status_code, 404)
        self.assertEqual(denied.data["code"], "quote_not_found")
        self.assert_no_mutation()
        receipt = self.redeem(quote).data
        denied = self.get(f"redemptions/{receipt['id']}/", foreign)
        self.assertEqual(denied.status_code, 404)
        self.assertNotIn(receipt["coupon_code"], str(denied.data))
        history = self.get("redemptions/", foreign)
        self.assertEqual(history.status_code, 200)
        self.assertEqual(history.data["redemptions"], [])

    def test_existing_receipt_detail_still_requires_original_private_thread(self):
        _, receipt = self.fulfilled()
        response = self.get(f"redemptions/{receipt['id']}/", {
            **IDENTITY, "slack_thread_ts": "1792454400.000200",
        })
        self.assertEqual(response.status_code, 404)
        self.assertNotIn(receipt["coupon_code"], str(response.data))

    def test_disabled_issuance_keeps_own_history_without_new_writes(self):
        quote, receipt = self.fulfilled()
        creations = self.gateway.create_coupon.call_count
        with override_settings(ROO_EVENT_DISCOUNTS_ENABLED=False):
            denied_quote = self.post("quote/", {**IDENTITY, "event_query": self.offer.event_id})
            self.assertEqual(denied_quote.status_code, 503)
            self.assertEqual(denied_quote.data["code"], "event_discounts_disabled")
            denied_redeem = self.redeem(quote)
            self.assertEqual(denied_redeem.status_code, 503)
            history = self.get("redemptions/")
            self.assertEqual(history.status_code, 200)
            self.assertFalse(history.data["enabled"])
            self.assertEqual(history.data["redemptions"][0]["coupon_code"], receipt["coupon_code"])
            detail = self.get(f"redemptions/{receipt['id']}/")
            self.assertEqual(detail.status_code, 200)
            self.assertEqual(detail.data["coupon_code"], receipt["coupon_code"])
        self.assertEqual(self.gateway.create_coupon.call_count, creations)
        self.assertEqual(Ledger.objects.count(), 1)

    def test_upstream_timeout_returns_processing_and_retry_recovers_once(self):
        quote = self.quote()
        def create_but_timeout(snapshot, code):
            self.create_coupon(snapshot, code)
            raise LumaEventDiscountError("luma_unavailable", "Synthetic timeout", uncertain=True, retryable=True)
        self.gateway.create_coupon.side_effect = create_but_timeout
        first = self.redeem(quote)
        self.assertEqual(first.status_code, 202, first.data)
        self.assertEqual(first.data["status"], "processing")
        self.assertIsNone(first.data["coupon_code"])
        self.assertIsNone(first.data["checkout_url"])
        self.assertFalse(Ledger.objects.exists())
        self.assertEqual(PointsService.get_available_microroo(self.owner), 15_000_000)
        duplicate = self.redeem(quote)
        self.assertEqual(duplicate.status_code, 202, duplicate.data)
        self.assertEqual(duplicate.data["id"], first.data["id"])
        self.gateway.create_coupon.assert_called_once()
        # Respect the worker's durable retry backoff rather than forcing a new
        # remote attempt when a member immediately repeats confirmation.
        EventDiscountRedemption.objects.filter(pk=first.data["id"]).update(
            next_attempt_at=timezone.now() - timedelta(seconds=1),
        )
        second = self.redeem(quote)
        self.assertEqual(second.status_code, 200, second.data)
        self.assertEqual(second.data["status"], "fulfilled")
        self.assertEqual(second.data["id"], first.data["id"])
        self.gateway.create_coupon.assert_called_once()
        self.assertEqual(Ledger.objects.count(), 1)

    def test_chat_authentication_required_and_service_key_cannot_read_member_receipts(self):
        for headers in ({}, {"HTTP_X_API_KEY": ROO_KEY}, {"HTTP_X_API_KEY": INTERNAL_KEY}):
            with self.subTest(headers=headers):
                response = self.client.get(CHAT, **headers)
                self.assertEqual(response.status_code, 401)
                self.assertNotIn("coupon_code", str(response.data))

    def test_chat_jwt_session_only_reads_its_owner_despite_target_parameters(self):
        _, owner_receipt = self.fulfilled()
        other_identity = {**IDENTITY, "slack_user_id": self.other.slack_id}
        _, other_receipt = self.fulfilled(identity=other_identity)
        token = str(AccessToken.for_user(self.owner))
        response = self.client.get(CHAT, {
            "user_id": self.other.pk, "target_user_id": self.other.pk,
            "slack_user_id": self.other.slack_id, "redemption_id": other_receipt["id"],
        }, HTTP_AUTHORIZATION="Bearer " + token)
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual([row["id"] for row in response.data["redemptions"]], [owner_receipt["id"]])
        self.assertNotIn(other_receipt["coupon_code"], str(response.data))
        self.assertEqual(response["Cache-Control"], "private, no-store")

    def test_chat_is_read_only_and_disabled_feature_retains_own_history(self):
        _, receipt = self.fulfilled()
        token = str(AccessToken.for_user(self.owner))
        headers = {"HTTP_AUTHORIZATION": "Bearer " + token}
        denied = self.client.post(CHAT, {"confirmed": True, "event_query": self.offer.event_id}, format="json", **headers)
        self.assertEqual(denied.status_code, 405)
        with override_settings(ROO_EVENT_DISCOUNTS_ENABLED=False):
            history = self.client.get(CHAT, **headers)
        self.assertEqual(history.status_code, 200, history.data)
        self.assertFalse(history.data["enabled"])
        self.assertEqual(history.data["redemptions"][0]["coupon_code"], receipt["coupon_code"])
        self.assertEqual(history["Cache-Control"], "private, no-store")
        self.assertEqual(EventDiscountRedemption.objects.count(), 1)
        self.gateway.create_coupon.assert_called_once()
