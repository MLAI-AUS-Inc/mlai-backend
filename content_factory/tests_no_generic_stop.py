"""B10: no generic stop reaches a founder, with the switch on or off.

A terminal run whose code is missing, generic or unregistered shows "We're
fixing this" with its points line on both founder paths (P2 copy ids and the P1
journey, which runs when the failure policy V2 flag is off), and staff are
alerted when it arrives. Refunds are unchanged: the replacement code is a
no-delivery refund code.
"""

from unittest.mock import patch
from uuid import uuid4

from django.test import SimpleTestCase, TestCase, override_settings

from roo.models import Ledger
from . import founder_journey_p2, no_generic_stop, registry_policy
from .incident_guards import NO_DELIVERY_REFUND_CODES
from .tests_founder_journey_unit import JourneyFixture, make_run


class GenericStopRuleTests(SimpleTestCase):
    def test_switch_decides_the_code_a_codeless_terminal_is_stored_under(self):
        with override_settings(ARTICLES_NO_GENERIC_STOP_ENABLED=True):
            self.assertEqual(no_generic_stop.uncoded_failure_code(), "TERMINAL_CODE_MISSING")
        with override_settings(ARTICLES_NO_GENERIC_STOP_ENABLED=False):
            self.assertEqual(no_generic_stop.uncoded_failure_code(), "UNCLASSIFIED")

    def test_generic_codes_are_ours_to_fix(self):
        for code in (None, "", "UNCLASSIFIED", "INTERNAL_ERROR", "TERMINAL_CODE_MISSING", "unclassified"):
            self.assertEqual(no_generic_stop.generic_stop_reason(code), "codeless_terminal", code)
        self.assertEqual(no_generic_stop.generic_stop_reason("NOT_A_REGISTERED_CODE_XYZ"), "unregistered_failure_code")

    def test_registered_codes_with_an_owner_or_founder_action_are_not_generic(self):
        for code in ("github_access_required", "website_not_connected", "EDITORIAL_REJECTED", "sdk_controller_conflict"):
            self.assertIsNone(no_generic_stop.generic_stop_reason(code), code)

    def test_every_needs_user_code_names_an_action(self):
        registry = registry_policy.load_registry()["by_code_v2"]
        silent = [code for code, entry in registry.items()
                  if entry["failure_class"] == "needs_user" and no_generic_stop.generic_stop_reason(code)]
        self.assertEqual(silent, [])

    def test_refunds_are_unchanged_by_the_new_codes(self):
        # Each used to arrive as UNCLASSIFIED, INTERNAL_ERROR or SANDBOX_UNAVAILABLE.
        self.assertIn("UNCLASSIFIED", NO_DELIVERY_REFUND_CODES)
        for code in ("TERMINAL_CODE_MISSING", "SDK_CONTROLLER_CONFLICT", "SDK_CALL_RECEIPT_CONFLICT",
                     "SDK_CALL_INDETERMINATE", "SDK_TOOL_RECEIPT_CONFLICT", "SDK_TOOL_INDETERMINATE",
                     "SOURCE_DOWNLOAD_INTERRUPTED", "SOURCE_TOO_LARGE", "SOURCE_BRANCH_MISSING",
                     "SOURCE_ACCESS_REFUSED", "SOURCE_REVISION_UNAVAILABLE"):
            self.assertIn(code, NO_DELIVERY_REFUND_CODES)

    def test_fixing_copy_keeps_the_points_lines(self):
        refunded = founder_journey_p2.line("fixing", outcome={"state": "refunded", "points": 12})
        self.assertEqual((refunded["headline"], refunded["body"], refunded["state"]),
                         ("We're fixing this", "Refunded 12 points.", "working"))
        self.assertEqual(founder_journey_p2.line("fixing", outcome={"state": "no_charge"})["body"], "No charge.")
        self.assertIsNone(founder_journey_p2.line("fixing")["action"])


class P1JourneyGenericStopTests(JourneyFixture, TestCase):
    """The flag-off path of the failure policy: the P1 journey cases."""

    def refunded_run(self, result):
        key = f"paid-{uuid4().hex[:8]}"
        make_run(self.org, status="failed", step="write_article",
                 request={"delivery_mode": "content_only", "client_request_id": key,
                          "roo_points_billing_status": "charged"}, result=result)
        Ledger.objects.create(user=self.owner, delta=-12, kind="SPEND", source="CONTENT_FACTORY",
                              idempotency_key=f"content_factory:charge:{key}")
        Ledger.objects.create(user=self.owner, delta=12, kind="REFUND", source="CONTENT_FACTORY",
                              idempotency_key=f"content_factory:refund:{key}")
        self.balance = 40

    def test_codeless_failure_is_ours_to_fix_and_keeps_the_refund_line(self):
        self.refunded_run({"failure_class": "permanent"})
        body = self.journey()
        self.assertStep(body, "first_article", "working", next_kind="write_article")
        self.assertEqual((body["headline"], body["body"]), ("We're fixing this", "Your points are back."))

    def test_unclassified_and_unregistered_codes_are_ours_to_fix(self):
        for code in ("UNCLASSIFIED", "NOT_A_REGISTERED_CODE_XYZ"):
            with self.subTest(code=code):
                self.refunded_run({"error_code": code})
                self.assertEqual(self.journey()["headline"], "We're fixing this")

    def test_a_registered_stop_keeps_the_stop_copy(self):
        self.refunded_run({"error_code": "EDITORIAL_REJECTED"})
        body = self.journey()
        self.assertEqual(body["body"], "We couldn't write this one. Your points are back.")

    @override_settings(ARTICLES_NO_GENERIC_STOP_ENABLED=False)
    def test_switch_off_restores_the_old_copy(self):
        self.refunded_run({"failure_class": "permanent"})
        self.assertEqual(self.journey()["body"], "We couldn't write this one. Your points are back.")


class P2CopyGenericStopTests(JourneyFixture, TestCase):
    def test_copy_id_for_answers_fixing_for_a_generic_terminal_in_the_cohort(self):
        with patch.object(registry_policy, "_policy_v2_for", return_value=True):
            self.assertEqual(registry_policy.copy_id_for(None, organization=self.org, terminal=True, self_heal=None),
                             "fixing")
            self.assertEqual(registry_policy.copy_id_for("UNCLASSIFIED", organization=self.org, terminal=True,
                                                         self_heal=None), "fixing")
            self.assertEqual(registry_policy.copy_id_for("EDITORIAL_REJECTED", organization=self.org, terminal=True,
                                                         self_heal=None), "stopped")
            with override_settings(ARTICLES_NO_GENERIC_STOP_ENABLED=False):
                self.assertEqual(registry_policy.copy_id_for(None, organization=self.org, terminal=True,
                                                             self_heal=None), "stopped")


class GenericStopAlertTests(JourneyFixture, TestCase):
    def test_a_generic_stop_alerts_staff_once_and_an_owner_cancel_does_not(self):
        run = make_run(self.org, status="failed", result={"error_code": "TERMINAL_CODE_MISSING"})
        with patch("content_factory.founder_journey_alerts.queue_founder_journey_alert", return_value="alert") as queue:
            self.assertEqual(no_generic_stop.alert_generic_stop(run), "alert")
            queue.assert_called_once_with(organization=self.org, alert_reason="codeless_terminal", run=run)
            owner = make_run(self.org, status="cancelled", result={"cancellation_reason": "owner_cancelled"})
            self.assertIsNone(no_generic_stop.alert_generic_stop(owner))
            registered = make_run(self.org, status="failed", result={"error_code": "EDITORIAL_REJECTED"})
            self.assertIsNone(no_generic_stop.alert_generic_stop(registered))
            self.assertEqual(queue.call_count, 1)

    def test_an_unregistered_code_alerts_with_its_own_reason(self):
        run = make_run(self.org, status="failed", result={"error_code": "NOT_A_REGISTERED_CODE_XYZ"})
        with patch("content_factory.founder_journey_alerts.queue_founder_journey_alert", return_value="alert") as queue:
            no_generic_stop.alert_generic_stop(run)
        self.assertEqual(queue.call_args.kwargs["alert_reason"], "unregistered_failure_code")

    @override_settings(ARTICLES_NO_GENERIC_STOP_ENABLED=False)
    def test_switch_off_sends_no_immediate_alert(self):
        run = make_run(self.org, status="failed", result={})
        with patch("content_factory.founder_journey_alerts.queue_founder_journey_alert") as queue:
            self.assertIsNone(no_generic_stop.alert_generic_stop(run))
        queue.assert_not_called()
