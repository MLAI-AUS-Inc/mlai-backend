"""Database-free Luma discount boundary tests; all network calls are mocked."""

import copy
from datetime import datetime, timezone
import unittest
from unittest.mock import Mock

import requests

from integrations.services.luma_event_discounts import (
    LumaEventDiscountError,
    LumaEventDiscountService,
)


NOW = datetime(2026, 10, 20, 0, tzinfo=timezone.utc)
EXPIRY = "2026-10-29T23:00:00Z"
TERMS = {
    "event_id": "evt-test", "event_ticket_type_id": "ttype-test",
    "discount": {"discount_type": "percent", "percent_off": 20},
    "valid_end_at": EXPIRY,
}
EVENT = {
    "id": "evt-test", "platform": "luma", "access": "manage",
    "name": "Synthetic MLAI event", "url": "https://luma.com/synthetic-event",
    "start_at": "2026-10-30T07:00:00Z", "timezone": "Australia/Sydney",
    "registration_open": True, "can_register_for_multiple_tickets": False,
    "visibility": "public", "spots_remaining": 20,
}
TICKET = {
    "id": "ttype-test", "name": "General", "type": "paid", "cents": 5000,
    "currency": "aud", "is_hidden": False, "require_approval": False,
    "is_flexible": False, "max_capacity": None,
    "valid_start_at": None, "valid_end_at": None,
}
COUPON = {
    "id": "coupon-test", "code": "ROO-SYNTHETIC", "remaining_count": 1,
    "valid_start_at": None, "valid_end_at": EXPIRY,
    "event_ticket_type_id": "ttype-test", "percent_off": 20,
    "cents_off": None, "currency": None,
}


def response(payload=None, *, status=200, malformed=False):
    result = Mock(status_code=status)
    result.json.side_effect = ValueError("sensitive provider body") if malformed else None
    result.json.return_value = copy.deepcopy(payload)
    return result


class LumaEventDiscountTests(unittest.TestCase):
    def setUp(self):
        self.session = Mock()
        self.service = LumaEventDiscountService(
            api_key="test-not-a-real-key", base_url="https://public-api.luma.com",
            session=self.session, max_pages=3,
        )

    def validate(self, *, event=None, ticket=None, terms=None, extra=()):
        self.session.request.side_effect = [
            response(EVENT if event is None else event),
            response({"entries": [TICKET if ticket is None else ticket]}),
            *extra,
        ]
        return self.service.validate_offer(TERMS if terms is None else terms, now=NOW)

    def assert_code(self, code, callable, *args, **kwargs):
        with self.assertRaises(LumaEventDiscountError) as error:
            callable(*args, **kwargs)
        self.assertEqual(error.exception.code, code)
        return error.exception

    def test_public_snapshot_contains_only_event_ticket_and_configured_terms(self):
        snapshot = self.validate(event={**EVENT, "hosts": [{"email": "private@example.invalid"}]})
        self.assertEqual(snapshot["url"], "https://luma.com/synthetic-event")
        self.assertEqual(snapshot["discount"], TERMS["discount"])
        self.assertEqual(snapshot["ticket"]["cents"], 5000)
        self.assertNotIn("private@", repr(snapshot))
        self.assertNotIn("hosts", snapshot)

    def test_listed_event_cannot_issue_coupons(self):
        self.assert_code("event_not_managed", self.validate, event={**EVENT, "access": "view"})
        self.assertEqual(self.session.request.call_count, 1)

    def test_wrong_event_is_rejected(self):
        self.assert_code("invalid_luma_event", self.validate, event={**EVENT, "id": "evt-other"})

    def test_group_registration_requires_explicit_false(self):
        for value in (True, None, "false", 0):
            with self.subTest(value=value):
                self.assert_code("group_registration_enabled", self.validate, event={**EVENT, "can_register_for_multiple_tickets": value})

    def test_closed_private_or_sold_out_event_is_rejected(self):
        for changes, code in (({"registration_open": False}, "event_registration_closed"),
                              ({"visibility": "private"}, "event_registration_closed"),
                              ({"spots_remaining": 0}, "event_sold_out"),
                              ({"spots_remaining": None, "max_capacity": 20}, "event_sold_out"),
                              ({"spots_remaining": "unknown"}, "event_sold_out")):
            with self.subTest(changes=changes):
                self.assert_code(code, self.validate, event={**EVENT, **changes})

    def test_event_and_offer_deadlines_are_rechecked(self):
        self.assert_code("event_offer_expired", self.validate, event={**EVENT, "start_at": "2026-10-19T07:00:00Z"})
        for expiry in ("2026-10-19T00:00:00Z", "2026-10-31T00:00:00Z"):
            self.assert_code("event_offer_expired", self.validate, terms={**TERMS, "valid_end_at": expiry})

    def test_only_paid_fixed_aud_tickets_are_eligible(self):
        for changes, code in (({"type": "free"}, "ticket_not_paid_aud"),
                              ({"currency": "usd"}, "ticket_not_paid_aud"),
                              ({"cents": 0}, "ticket_not_paid_aud"),
                              ({"cents": True}, "ticket_not_paid_aud"),
                              ({"cents": 10.5}, "ticket_not_paid_aud"),
                              ({"is_hidden": True}, "ticket_not_supported"),
                              ({"require_approval": True}, "ticket_not_supported"),
                              ({"is_flexible": True}, "ticket_not_supported")):
            with self.subTest(changes=changes):
                self.assert_code(code, self.validate, ticket={**TICKET, **changes})

    def test_ticket_must_be_from_exact_event_listing(self):
        self.assert_code("ticket_unavailable", self.validate, ticket={**TICKET, "id": "ttype-other"})

    def test_ticket_price_must_be_strictly_below_one_hundred_aud(self):
        snapshot = self.validate(ticket={**TICKET, "cents": 9999})
        self.assertEqual(snapshot["ticket"]["cents"], 9999)
        for cents in (10000, 10001, 50000):
            with self.subTest(cents=cents):
                self.assert_code("ticket_price_limit", self.validate, ticket={**TICKET, "cents": cents})

    def test_full_ticket_reward_preserves_one_use_and_exact_ticket_scope(self):
        snapshot = self.validate(terms={**TERMS, "discount": {"discount_type": "percent", "percent_off": 100}})
        self.session.request.side_effect = None
        self.session.request.return_value = response({**COUPON, "percent_off": 100})
        self.service.create_coupon(snapshot, "ROO-SYNTHETIC")
        body = self.session.request.call_args.kwargs["json"]
        self.assertEqual(body["remaining_count"], 1)
        self.assertEqual(body["event_ticket_type_id"], "ttype-test")
        self.assertEqual(body["discount"], {"discount_type": "percent", "percent_off": 100.0})

    def test_price_increase_to_limit_invalidates_an_existing_snapshot(self):
        snapshot = self.validate(ticket={**TICKET, "cents": 9999})
        self.assert_code("ticket_price_limit", self.validate, ticket={**TICKET, "cents": 10000}, terms=snapshot)
        self.assertTrue(all(call.args[0] == "GET" for call in self.session.request.call_args_list))

    def test_ticket_sales_window_and_coupon_expiry_are_checked(self):
        for changes, code in (({"valid_start_at": "2026-10-21T00:00:00Z"}, "ticket_sales_closed"),
                              ({"valid_end_at": "2026-10-19T00:00:00Z"}, "ticket_sales_closed"),
                              ({"valid_end_at": "2026-10-28T00:00:00Z"}, "invalid_coupon_expiry"),
                              ({"valid_end_at": "2026-10-28"}, "invalid_provider_time")):
            with self.subTest(changes=changes):
                self.assert_code(code, self.validate, ticket={**TICKET, **changes})

    def test_capped_ticket_counts_approved_tickets_and_deduplicates_pages(self):
        guest = {"approval_status": "approved", "user_email": "private@example.invalid", "event_tickets": [
            {"id": "ticket-one", "event_ticket_type_id": "ttype-test"},
            {"id": "ticket-other", "event_ticket_type_id": "ttype-other"},
        ]}
        snapshot = self.validate(ticket={**TICKET, "max_capacity": 2}, extra=[
            response({"entries": [guest], "has_more": True, "next_cursor": "next"}),
            response({"entries": [guest], "has_more": False}),
        ])
        self.assertNotIn("private@", repr(snapshot))
        self.assertEqual(self.session.request.call_args.kwargs["params"]["approval_status"], "approved")
        self.assertEqual(self.session.request.call_args.kwargs["params"]["pagination_cursor"], "next")

    def test_capped_ticket_sold_out_and_missing_ticket_evidence_fail_closed(self):
        guest = {"approval_status": "approved", "event_tickets": [{"id": "ticket-one", "event_ticket_type_id": "ttype-test"}]}
        self.assert_code("ticket_sold_out", self.validate, ticket={**TICKET, "max_capacity": 1},
                         extra=[response({"entries": [guest], "has_more": False})])
        self.assert_code("ticket_capacity_unverified", self.validate, ticket={**TICKET, "max_capacity": 1},
                         extra=[response({"entries": [{"approval_status": "approved"}], "has_more": False})])

    def test_amount_discount_is_aud_and_cannot_exceed_ticket_price(self):
        terms = {**TERMS, "discount": {"discount_type": "amount", "cents_off": 1000, "currency": "aud"}}
        self.assertEqual(self.validate(terms=terms)["discount"], terms["discount"])
        for discount in ({"discount_type": "amount", "cents_off": 5001, "currency": "aud"},
                         {"discount_type": "amount", "cents_off": 500, "currency": "usd"},
                         {"discount_type": "percent", "percent_off": True},
                         {"discount_type": "percent", "percent_off": float("nan")},
                         {"discount_type": "percent", "percent_off": 101}):
            self.assert_code("invalid_discount", self.validate, terms={**TERMS, "discount": discount})

    def test_create_is_one_ticket_one_use_with_persisted_code_and_expiry(self):
        self.session.request.return_value = response(COUPON)
        self.service.create_coupon(TERMS, "roo-synthetic")
        self.session.request.assert_called_once()
        args = self.session.request.call_args
        self.assertEqual(args.args[:2], ("POST", "https://public-api.luma.com/v1/events/coupons/create"))
        self.assertEqual(args.kwargs["json"], {**TERMS, "code": "ROO-SYNTHETIC", "remaining_count": 1})
        self.assertIs(args.kwargs["allow_redirects"], False)

    def test_create_timeout_never_retries_and_is_uncertain(self):
        self.session.request.side_effect = requests.Timeout("sensitive request")
        error = self.assert_code("luma_unavailable", self.service.create_coupon, TERMS, "ROO-SYNTHETIC")
        self.assertTrue(error.uncertain)
        self.assertTrue(error.retryable)
        self.session.request.assert_called_once()
        self.assertNotIn("sensitive", str(error))

    def test_5xx_conflict_and_malformed_success_writes_require_reconciliation(self):
        for item in (response(status=503), response(status=409), response(status=429), response(malformed=True), response([])):
            with self.subTest(item=item):
                self.session.request.return_value = item
                with self.assertRaises(LumaEventDiscountError) as error:
                    self.service.create_coupon(TERMS, "ROO-SYNTHETIC")
                self.assertTrue(error.exception.uncertain)

    def test_definitive_rejected_write_is_not_marked_uncertain(self):
        self.session.request.return_value = response({"error": "sensitive"}, status=422)
        error = self.assert_code("luma_rejected", self.service.create_coupon, TERMS, "ROO-SYNTHETIC")
        self.assertFalse(error.uncertain)
        self.assertNotIn("sensitive", str(error))

    def test_unexpected_created_coupon_terms_are_uncertain(self):
        self.session.request.return_value = response({**COUPON, "remaining_count": 1000000})
        error = self.assert_code("coupon_terms_unverified", self.service.create_coupon, TERMS, "ROO-SYNTHETIC")
        self.assertTrue(error.uncertain)

    def test_reconciled_coupon_must_match_exact_saved_terms(self):
        self.assertTrue(self.service.matches_coupon(COUPON, TERMS, "roo-synthetic"))
        self.assertTrue(self.service.matches_coupon({**COUPON, "remaining_count": 0}, TERMS, "roo-synthetic"))
        for changes in ({"code": "OTHER"}, {"event_ticket_type_id": None}, {"percent_off": 30},
                        {"id": True}, {"remaining_count": True}, {"remaining_count": 2}, {"valid_end_at": None},
                        {"currency": "aud"}, {"cents_off": 10}, {"valid_start_at": EXPIRY}):
            with self.subTest(changes=changes):
                self.assertFalse(self.service.matches_coupon({**COUPON, **changes}, TERMS, "ROO-SYNTHETIC"))

    def test_amount_coupon_matches_only_exact_minor_unit_currency(self):
        terms = {**TERMS, "discount": {"discount_type": "amount", "cents_off": 1000, "currency": "aud"}}
        coupon = {**COUPON, "percent_off": None, "cents_off": 1000, "currency": "aud"}
        self.assertTrue(self.service.matches_coupon(coupon, terms, "ROO-SYNTHETIC"))
        self.assertFalse(self.service.matches_coupon({**coupon, "currency": "usd"}, terms, "ROO-SYNTHETIC"))

    def test_coupon_expiry_tolerates_provider_millisecond_precision_without_extending(self):
        terms = {**TERMS, "valid_end_at": "2026-10-29T23:00:00.123456Z"}
        coupon = {**COUPON, "valid_end_at": "2026-10-29T23:00:00.123Z"}
        self.session.request.return_value = response(coupon)
        self.service.create_coupon(terms, "ROO-SYNTHETIC")
        self.assertEqual(self.session.request.call_args.kwargs["json"]["valid_end_at"], "2026-10-29T23:00:00.123000Z")
        self.assertTrue(self.service.matches_coupon(coupon, terms, "ROO-SYNTHETIC"))
        self.assertFalse(self.service.matches_coupon({**coupon, "valid_end_at": "2026-10-29T23:00:00.124Z"}, terms, "ROO-SYNTHETIC"))

    def test_find_is_case_insensitive_and_requires_complete_pages(self):
        self.session.request.side_effect = [
            response({"entries": [{**COUPON, "code": "OTHER"}], "has_more": True, "next_cursor": "next"}),
            response({"entries": [COUPON], "has_more": False}),
        ]
        self.assertEqual(self.service.find_coupon("evt-test", "roo-synthetic"), COUPON)

    def test_even_found_coupon_does_not_hide_incomplete_or_duplicate_scan(self):
        for pages, code in (([response({"entries": [COUPON], "has_more": True})], "incomplete_luma_pagination"),
                            ([response({"entries": [COUPON, COUPON], "has_more": False})], "duplicate_luma_coupon")):
            self.session.request.side_effect = pages
            self.assert_code(code, self.service.find_coupon, "evt-test", "ROO-SYNTHETIC")

    def test_pagination_cycle_and_page_budget_cannot_report_coupon_absent(self):
        page = response({"entries": [], "has_more": True, "next_cursor": "same"})
        self.session.request.side_effect = [page, page]
        self.assert_code("incomplete_luma_pagination", self.service.find_coupon, "evt-test", "ROO-SYNTHETIC")
        self.session.request.side_effect = [response({"entries": [], "has_more": True, "next_cursor": str(i)}) for i in range(3)]
        self.assert_code("luma_pagination_limit", self.service.find_coupon, "evt-test", "ROO-SYNTHETIC")

    def test_disable_verifies_remaining_zero_before_returning(self):
        self.session.request.side_effect = [response({}), response({"entries": [{**COUPON, "remaining_count": 0}], "has_more": False})]
        self.service.disable_coupon("evt-test", "ROO-SYNTHETIC")
        self.assertEqual(self.session.request.call_args_list[0].kwargs["json"], {"event_id": "evt-test", "code": "ROO-SYNTHETIC", "remaining_count": 0})
        self.session.request.side_effect = [response({}), response({"entries": [COUPON], "has_more": False})]
        error = self.assert_code("coupon_disable_unverified", self.service.disable_coupon, "evt-test", "ROO-SYNTHETIC")
        self.assertTrue(error.uncertain)

    def test_safe_link_cannot_forward_credentials_or_untrusted_origins(self):
        self.assertEqual(self.service.coupon_url(EVENT["url"], "roo-synthetic"), "https://luma.com/synthetic-event?coupon=ROO-SYNTHETIC")
        for url in ("https://evil.invalid/event", "https://luma.com@evil.invalid/event", "https://user:password@luma.com/event",
                    "http://luma.com/event", "https://luma.com:444/event", "https://luma.com//evil.invalid", "https://luma.com/\\evil.invalid", "https://luma.com/\nevent",
                    "https://luma.com/event/evt-test", "https://luma.com/event/", "https://luma.com/", "https://luma.com/event?utm_source=test", "https://luma.com/event#details",
                    "https://luma.com/event?", "https://luma.com/event#", "https://luma.com/encoded%2Fslug", "https://luma.com/event\x7f"):
            with self.subTest(url=url):
                self.assert_code("invalid_luma_url", self.service.coupon_url, url, "ROO-SYNTHETIC")

    def test_www_provider_origins_are_normalized_before_returning_a_link(self):
        for hostname in ("luma.com", "lu.ma"):
            with self.subTest(hostname=hostname):
                url = self.service.coupon_url("https://www." + hostname + "/synthetic-event", "ROO-SYNTHETIC")
                self.assertEqual(url, "https://" + hostname + "/synthetic-event?coupon=ROO-SYNTHETIC")


if __name__ == "__main__":
    unittest.main()
