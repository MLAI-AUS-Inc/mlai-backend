"""B11/B14/B25 staff_fixing and budget-configuration regressions; never create a database."""
from datetime import datetime, timedelta, timezone as dt_timezone
from types import SimpleNamespace
from unittest.mock import Mock, patch

from django.test import SimpleTestCase, override_settings

from . import tests_one_click_operations_unit as operation_fixtures
from . import website_prepare as prepare
from . import website_staff_fixing as staff
from .website_contract import WebsiteAuthorityError

# A Monday morning in Melbourne (Sunday 23:00 UTC).
MONDAY = datetime(2026, 11, 15, 23, 0, tzinfo=dt_timezone.utc)
FRIDAY = datetime(2026, 11, 19, 23, 0, tzinfo=dt_timezone.utc)
RECEIPT = {"exhausted": True, "stop": "repair_cap_reached", "rounds": 3, "finding_codes": ["ARTICLE_ROUTE_MISSING"]}


def _operation():
    op = operation_fixtures.PrepareJourneyTests().operation()
    op.connection.organization = SimpleNamespace(pk=7, domain="example.com")
    op.connection.capabilities = {"framework": "nextjs"}
    return op


class _Mocks:
    """Patch every side effect staff_fixing reaches; nothing leaves the process."""

    def __init__(self, case):
        self.alert = Mock()
        self.notify = Mock()
        self.refund = Mock()
        self.lead = Mock()
        for target, mock in (("content_factory.founder_journey_alerts.queue_founder_journey_alert", self.alert),
                             ("content_factory.founder_transitions.notify_staff_fixing", self.notify),
                             ("content_factory.website_first_article_refunds.reserve_refund", self.refund),
                             ("founder_tools.website_leads.record_staff_lead", self.lead)):
            patcher = patch(target, mock)
            patcher.start()
            case.addCleanup(patcher.stop)

    def escalations(self):
        return [call.kwargs["details"]["escalation"] for call in self.alert.call_args_list
                if call.kwargs["details"].get("kind") == "staff_fixing"]


@override_settings(ARTICLES_FOUNDER_JOURNEY_ALERTS_ENABLED=True)
class StaffFixingLifeTests(SimpleTestCase):
    def setUp(self):
        self.mocks = _Mocks(self)

    def entered(self, now=MONDAY):
        op = _operation()
        staff.enter(op, now=now, code="DIRECTORY_REPAIR_EXHAUSTED", step="setup", repair_receipt=RECEIPT, run_id="run-1")
        return op

    def test_entry_hands_the_fix_to_staff_without_a_founder_action(self):
        op = self.entered()
        self.assertEqual(op.state, "needs_attention")
        self.assertIsNone(op.next_attempt_at)
        self.assertEqual(op.receipt["status"], "staff_fixing")
        self.assertEqual(op.receipt["hold_owner"], "staff")
        self.assertNotIn("user_action", op.receipt)
        self.assertFalse(op.receipt["retryable"])
        record = op.receipt["staff_fixing"]
        self.assertEqual(record["deadline_at"], (MONDAY + timedelta(days=7)).isoformat())
        self.assertEqual(self.mocks.alert.call_args.kwargs["alert_reason"], "repair_loop_exhausted")
        self.assertEqual(self.mocks.escalations(), ["entered"])

    def test_re_entry_keeps_the_original_deadline_and_does_not_re_alert(self):
        op = self.entered()
        staff.enter(op, now=MONDAY + timedelta(days=3), code="X", step="setup", repair_receipt=RECEIPT)
        self.assertEqual(op.receipt["staff_fixing"]["since"], MONDAY.isoformat())
        self.assertEqual(self.mocks.escalations(), ["entered"])

    def test_escalates_at_each_bound_and_never_exceeds_seven_days(self):
        op = self.entered()
        self.assertEqual(staff.escalate(op, now=MONDAY + timedelta(hours=23)), [])
        self.assertEqual(staff.escalate(op, now=MONDAY + timedelta(days=1)), ["owner_due"])
        self.assertEqual(self.mocks.alert.call_args.kwargs["alert_reason"], "our_team_item")
        self.assertEqual(staff.escalate(op, now=MONDAY + timedelta(hours=71)), [])
        self.assertEqual(staff.escalate(op, now=MONDAY + timedelta(hours=72)), ["founder_update"])
        self.mocks.notify.assert_called_once_with(op.connection.organization, op, "staff_fixing_update")
        self.assertEqual(staff.escalate(op, now=MONDAY + timedelta(days=7) - timedelta(seconds=1)), [])
        self.assertEqual(staff.escalate(op, now=MONDAY + timedelta(days=7)), ["day_7_exit"])
        self.assertEqual(op.state, "stopped")
        self.assertEqual(op.receipt["status"], "cant_be_done_yet")
        self.assertEqual(op.receipt["staff_fixing"]["cant_be_done_reason"], "not_fixed_in_time")
        self.mocks.refund.assert_called_once()
        self.mocks.lead.assert_called_once()
        self.assertEqual(self.mocks.lead.call_args.kwargs["reason"], staff.LEAD_REASON)
        self.assertEqual(self.mocks.escalations(), ["entered", "owner_due", "day_7_exit"])
        self.assertEqual(self.mocks.notify.call_args.args[2], "staff_fixing_cant_be_done")
        # Closed means closed: later sweeps change nothing and re-entry starts a new life.
        self.assertEqual(staff.escalate(op, now=MONDAY + timedelta(days=30)), [])
        self.assertFalse(staff.active(op))
        self.mocks.refund.assert_called_once()

    def test_a_late_sweep_applies_every_passed_bound_at_once(self):
        op = self.entered()
        self.assertEqual(staff.escalate(op, now=MONDAY + timedelta(days=9)), ["owner_due", "founder_update", "day_7_exit"])
        self.assertEqual(op.state, "stopped")

    def test_named_owner_suppresses_the_owner_alert_and_weekends_are_skipped(self):
        op = self.entered(now=FRIDAY)
        self.assertEqual(staff.next_working_day(FRIDAY), FRIDAY + timedelta(days=3))
        staff_user = SimpleNamespace(pk=99, is_active=True, is_staff=True)
        staff.assign_owner(op, staff_user=staff_user, now=FRIDAY + timedelta(hours=1))
        self.assertEqual(staff.escalate(op, now=FRIDAY + timedelta(days=3)), ["founder_update"])
        with self.assertRaises(PermissionError):
            staff.assign_owner(op, staff_user=SimpleNamespace(pk=1, is_active=True, is_staff=False))

    def test_exits(self):
        op = self.entered()
        staff.close(op, "ready", now=MONDAY + timedelta(days=2))
        self.assertFalse(staff.active(op))
        self.assertNotIn("hold_owner", op.receipt)
        self.mocks.refund.assert_not_called()
        op = self.entered()
        staff.close(op, "named_ask", user_action={"id": "authorize_github", "label": "Connect GitHub"})
        self.assertEqual(op.state, "needs_user")
        with self.assertRaises(ValueError):
            staff.close(self.entered(), "named_ask", user_action={"id": "retry"})
        with self.assertRaises(ValueError):
            staff.close(self.entered(), "cant_be_done_yet", reason="free text")

    def test_refund_failure_still_stops_and_alerts(self):
        self.mocks.refund.side_effect = RuntimeError("ledger down")
        op = self.entered()
        staff.close(op, "cant_be_done_yet", reason="cms_content", now=MONDAY + timedelta(days=1))
        self.assertEqual(op.state, "stopped")
        self.assertIn("refund_failed", self.mocks.escalations())
        self.mocks.lead.assert_called_once()

    def test_repair_receipt_is_read_from_the_result_not_the_code(self):
        self.assertEqual(staff.repair_exhausted({"repair_receipt": RECEIPT}), RECEIPT)
        self.assertEqual(staff.repair_exhausted({"article_system_setup": {"repair_receipt": RECEIPT}}), RECEIPT)
        self.assertIsNone(staff.repair_exhausted({"repair_receipt": {**RECEIPT, "exhausted": "yes"}}))
        self.assertIsNone(staff.repair_exhausted({"repair_receipt": {**RECEIPT, "stop": "other"}}))
        self.assertIsNone(staff.repair_exhausted({"error_code": "DIRECTORY_REPAIR_EXHAUSTED"}))


@override_settings(ARTICLES_FOUNDER_JOURNEY_ALERTS_ENABLED=True)
class PrepareFailureRoutingTests(SimpleTestCase):
    def setUp(self):
        self.mocks = _Mocks(self)

    def test_exhausted_repair_enters_staff_fixing_without_try_again(self):
        op = _operation()
        error = WebsiteAuthorityError("TEMPLATE_VALIDATION_FAILED", "Gate failed", retryable=True)
        error.result = {"repair_receipt": RECEIPT, "setup_run_id": "run-9"}
        prepare.prepare_failure(op, "setup", error)
        self.assertEqual(op.state, "needs_attention")
        self.assertEqual(op.receipt["status"], "staff_fixing")
        self.assertNotIn("user_action", op.receipt)
        self.assertEqual(op.receipt["staff_fixing"]["run_id"], "run-9")
        self.assertEqual(self.mocks.alert.call_args.kwargs["alert_reason"], "repair_loop_exhausted")

    def test_same_code_without_a_receipt_keeps_the_existing_path(self):
        op = _operation()
        prepare.prepare_failure(op, "setup", WebsiteAuthorityError("TEMPLATE_VALIDATION_FAILED", "Gate failed", retryable=True))
        self.assertNotEqual(op.receipt.get("status"), "staff_fixing")
        self.mocks.alert.assert_not_called()

    @override_settings(ARTICLES_FAILURE_TAXONOMY_POLICY_ENABLED=True, ARTICLES_FAILURE_TAXONOMY_ENROLLED_ORGANIZATIONS=("7",))
    def test_budget_not_configured_is_transient_alerted_and_refundable(self):
        from .incident_guards import NO_DELIVERY_REFUND_CODES
        from .website_failure_contract import classify_failure
        from .website_failure_scope import failure_scope
        self.assertIn("RUN_BUDGET_NOT_CONFIGURED", NO_DELIVERY_REFUND_CODES)
        with failure_scope(7):
            failure = classify_failure("RUN_BUDGET_NOT_CONFIGURED", "website_authority")
            self.assertEqual(failure["failure_class"], "transient")
            op = _operation()
            prepare.prepare_failure(op, "setup", WebsiteAuthorityError("RUN_BUDGET_NOT_CONFIGURED", "No ceiling", retryable=True))
        self.assertEqual(op.state, "pending")
        alert = self.mocks.alert.call_args.kwargs
        self.assertEqual(alert["alert_reason"], "our_team_item")
        self.assertEqual(alert["details"]["code"], "RUN_BUDGET_NOT_CONFIGURED")


class ControlBudgetTests(SimpleTestCase):
    @override_settings(ARTICLES_RUN_SDK_CONTROL_ENABLED=True, ARTICLES_RUN_CONTROL_MODEL_USD="0", ARTICLES_RUN_CONTROL_MODEL_TOKENS=0)
    def test_setup_without_a_configured_allowance_is_logged_not_silent(self):
        from . import run_execution_control as control
        with self.assertLogs("content_factory.run_execution_control", level="ERROR") as logs:
            self.assertIsNone(control.initial_control_budget("article_system_setup"))
        self.assertIn("ARTICLES_RUN_CONTROL_MODEL_USD,ARTICLES_RUN_CONTROL_MODEL_TOKENS", "\n".join(logs.output))

    @override_settings(ARTICLES_RUN_SDK_CONTROL_ENABLED=True, ARTICLES_RUN_CONTROL_MODEL_USD="2.50", ARTICLES_RUN_CONTROL_MODEL_TOKENS=400000)
    def test_allowance_is_fixed_at_issue(self):
        from . import run_execution_control as control
        value = control.initial_control_budget("article_system_setup")
        self.assertEqual((value["max_model_usd"], value["max_model_tokens"]), ("2.50", 400000))
