"""B10: no generic stop reaches a founder, with the switch on or off.

A terminal run whose code is missing, generic or unregistered shows "We're
fixing this" with its points line on both founder paths (P2 copy ids and the P1
journey, which runs when the failure policy V2 flag is off), and staff are
alerted when it arrives. Refunds are unchanged: the replacement code is a
no-delivery refund code.
"""

from types import SimpleNamespace
from unittest.mock import patch
from uuid import uuid4

from django.test import SimpleTestCase, TestCase, override_settings

from roo.models import Ledger
from . import founder_journey, founder_journey_p2, no_generic_stop, p2_foundation, registry_policy
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

    def test_a_paid_article_still_checks_its_points(self):
        body = founder_journey_p2.line("fixing", outcome={"state": "pending"})
        self.assertEqual(body["body"], "We're checking the points for this article.")
        self.assertEqual(body["stage"], "writing")

    def test_a_setup_stop_has_the_setup_copy_and_no_points_line(self):
        for copy_id in ("fixing", "stopped"):
            for outcome in ({"state": "pending"}, None, {"state": "refunded", "points": 12}):
                with self.subTest(copy_id=copy_id, outcome=outcome):
                    body = founder_journey_p2.line(copy_id, article=False, outcome=outcome)
                    self.assertEqual((body["copyId"], body["headline"], body["body"]),
                                     ("fixing", "We're fixing this", founder_journey_p2.SETUP_FIXING_BODY))
                    self.assertNotIn("points", body["body"])
                    self.assertEqual((body["stage"], body["state"]), ("setup", "working"))
                    self.assertIsNone(body["action"])
                    self.assertIsNone(body["next"])


def _run(status="cancelled", *, result=None, error="", workflow="article_system_setup", request=None, run_id="run-1"):
    return SimpleNamespace(status=status, result=result if result is not None else {}, error=error, workflow=workflow,
                           run_request=request if request is not None else {}, run_id=run_id)


class OwnerInitiatedStopTests(SimpleTestCase):
    """Who stopped a run, read from its own record; no database."""

    def test_owner_cancels_and_website_actions_are_not_ours_to_fix(self):
        self.assertTrue(founder_journey.is_owner_initiated_stop(_run(result={"cancellation_reason": "owner_cancelled"})))
        self.assertTrue(founder_journey.is_owner_initiated_stop(_run(result={"cancelled_by": "owner"})))
        for transition in ("reset", "disconnect", "reconnect", "revoke", "purge"):
            with self.subTest(transition=transition):
                run = _run(error="Website connection changed.", result={
                    "cancellation_reason": "system_stop", "cancelled_by": "system",
                    "cancellation_transition": transition})
                self.assertTrue(founder_journey.is_owner_initiated_stop(run))
        self.assertTrue(founder_journey.is_owner_initiated_stop(_run(error="Website operation cancelled.")))

    def test_staff_failures_and_other_system_stops_stay_ours(self):
        staff = _run(result={"cancellation_reason": "system_stop", "cancelled_by": "staff",
                             "cancellation_transition": "reset"})
        self.assertFalse(founder_journey.is_owner_initiated_stop(staff))
        self.assertFalse(founder_journey.is_owner_initiated_stop(_run("failed", error="Website operation cancelled.")))
        lost = _run(error="Worker stopped.", result={"cancellation_reason": "worker_heartbeat_lost",
                                                     "cancelled_by": "system"})
        self.assertFalse(founder_journey.is_owner_initiated_stop(lost))
        # Pause and cleanup never stop a run.
        paused = _run(error="Website connection changed.", result={
            "cancellation_reason": "system_stop", "cancelled_by": "system", "cancellation_transition": "pause"})
        self.assertFalse(founder_journey.is_owner_initiated_stop(paused))

    def test_the_recorded_facts_answer_before_any_lookup(self):
        with patch("content_factory.system_stopped_runs.transition_for", side_effect=AssertionError("lookup")):
            self.assertTrue(founder_journey.is_owner_initiated_stop(_run(result={"cancellation_transition": "reset"})))
            self.assertTrue(founder_journey.is_owner_initiated_stop(_run(error="Website operation cancelled.")))
        with patch("content_factory.system_stopped_runs.transition_for", return_value="reset") as lookup:
            self.assertTrue(founder_journey.is_owner_initiated_stop(_run(error="Website connection changed.")))
        lookup.assert_called_once()

    def test_a_stopped_setup_from_an_older_generation_is_stale(self):
        connection = SimpleNamespace(pk=7, generation=4)
        old = {"website_connection_id": "7", "connection_generation": 3}
        self.assertTrue(founder_journey.is_stale_setup_run(_run("failed", request=old), connection))
        self.assertTrue(founder_journey.is_stale_setup_run(_run("cancelled", request=old), connection))
        self.assertTrue(founder_journey.is_stale_setup_run(_run("blocked", request=old), connection))
        current = {"website_connection_id": "7", "connection_generation": 4}
        self.assertFalse(founder_journey.is_stale_setup_run(_run("failed", request=current), connection))
        self.assertFalse(founder_journey.is_stale_setup_run(_run("running", request=old), connection))
        self.assertFalse(founder_journey.is_stale_setup_run(_run("failed", request=old, workflow="article_generation"),
                                                            connection))
        other = {"website_connection_id": "8", "connection_generation": 1}
        self.assertFalse(founder_journey.is_stale_setup_run(_run("failed", request=other), connection))
        self.assertFalse(founder_journey.is_stale_setup_run(_run("failed"), connection))  # legacy: no generation
        self.assertFalse(founder_journey.is_stale_setup_run(_run("failed", request=old), None))
        self.assertFalse(founder_journey.is_stale_setup_run(None, connection))

    def test_a_setup_run_the_shallow_reset_tombstoned_is_stale(self):
        config = SimpleNamespace(article_system={"article_setup_reset": {"excludedRunIds": ["setup-old"]}})
        self.assertTrue(founder_journey.is_stale_setup_run(_run("failed", run_id="setup-old"), None, config))
        self.assertFalse(founder_journey.is_stale_setup_run(_run("failed", run_id="setup-new"), None, config))
        self.assertFalse(founder_journey.is_stale_setup_run(_run("running", run_id="setup-old"), None, config))


class ArticleOutcomeTests(SimpleTestCase):
    """One points outcome for the journey card and the article_stopped email; no database."""

    def outcome(self, run, receipt):
        # optional() resolves the seam through a cached lookup, so clear it to
        # see this test's patch rather than an earlier one.
        p2_foundation._optional.cache_clear()
        self.addCleanup(p2_foundation._optional.cache_clear)
        with patch("content_factory.no_delivery_refunds.points_outcome", return_value=receipt):
            return founder_journey_p2.article_outcome(run)

    def test_an_unpaid_article_with_a_pending_receipt_reads_no_charge(self):
        unpaid = _run("failed", workflow="article_generation", request={"delivery_mode": "content_only"})
        for receipt in ({"state": "pending", "points": None}, None):
            with self.subTest(receipt=receipt):
                self.assertEqual(self.outcome(unpaid, receipt), {"state": "no_charge", "points": None})

    def test_a_paid_article_or_a_settled_receipt_is_unchanged(self):
        paid = _run("failed", workflow="article_generation",
                    request={"delivery_mode": "content_only", "roo_points_billing_status": "charged"})
        pending = {"state": "pending", "points": None}
        self.assertEqual(self.outcome(paid, pending), pending)
        unpaid = _run("failed", workflow="article_generation")
        refunded = {"state": "refunded", "points": 12}
        self.assertEqual(self.outcome(unpaid, refunded), refunded)
        self.assertEqual(self.outcome(_run("failed"), pending), pending)  # a setup run is not an article

    def test_the_article_stopped_email_reads_the_same_points_line_as_the_card(self):
        from . import founder_transitions
        unpaid = _run("failed", workflow="article_generation", request={"delivery_mode": "content_only"})
        organization = SimpleNamespace(domain="example.com", name="Example")
        with patch("content_factory.no_delivery_refunds.points_outcome",
                   return_value={"state": "pending", "points": None}):
            _headline, body, _label = founder_transitions._copy("article_stopped", {"run": unpaid}, organization)
            card = founder_journey_p2.line("stopped", run=unpaid, outcome=founder_journey_p2.article_outcome(unpaid))
        self.assertEqual(body, "No charge.")
        self.assertEqual(body, card["body"])


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

    def test_an_article_stopped_by_clear_saved_setup_is_not_a_failure_card(self):
        make_run(self.org, status="cancelled", step="write_article", result={
            "cancellation_reason": "system_stop", "cancelled_by": "system", "cancellation_transition": "reset"})
        body = self.journey()
        self.assertNotEqual(body["headline"], "We're fixing this")
        self.assertNotIn(body.get("copyId"), {"fixing", "failed"})
        self.assertNotIn("We couldn't write this one", body.get("body") or "")

    def test_a_setup_that_failed_before_clear_saved_setup_is_not_a_failure_card_by_run_id(self):
        connection = self.connection()
        run = make_run(self.org, workflow="article_system_setup", status="failed", step="setup",
                       request={"website_connection_id": str(connection.pk),
                                "connection_generation": connection.generation})
        type(connection).objects.filter(pk=connection.pk).update(generation=connection.generation + 1)
        body = self.journey(run_id=run.run_id)
        self.assertNotEqual(body["headline"], "We're fixing this")
        self.assertFalse(str(body.get("copyId") or "").startswith(("fixing", "failed")))
        self.assertNotIn("We couldn't write this one", body.get("body") or "")


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

    def decide(self, run):
        from django.utils import timezone
        # optional() resolves seams through a cached lookup; clear it so a
        # patched seam (publication child, points outcome) is the one read.
        p2_foundation._optional.cache_clear()
        self.addCleanup(p2_foundation._optional.cache_clear)
        with patch.object(registry_policy, "_policy_v2_for", return_value=True):
            return founder_journey_p2.run_decision(self.org, run, config=self.built.config, user=self.owner,
                                                   now=timezone.now())

    def test_a_setup_stopped_by_the_owner_or_a_website_action_has_no_card(self):
        reset = make_run(self.org, workflow="article_system_setup", status="cancelled", step="setup",
                         result={"cancellation_reason": "system_stop", "cancelled_by": "system",
                                 "cancellation_transition": "reset"})
        self.assertIsNone(self.decide(reset))
        stop = make_run(self.org, workflow="article_system_setup", status="cancelled", step="setup",
                        result={})
        stop.error = "Website operation cancelled."
        stop.save(update_fields=["error"])
        self.assertIsNone(self.decide(stop))

    def test_a_delivered_draft_keeps_its_card_after_a_website_action_stops_its_run(self):
        reset = {"cancellation_reason": "system_stop", "cancelled_by": "system", "cancellation_transition": "reset"}
        ready = make_run(self.org, status="cancelled", step="write_article", result={**reset, "channel_v1": {
            "channel": "copy_ready", "waiting_to_publish": True, "dispatch_state": "accepted"}})
        with patch("content_factory.founder_draft_views.is_delivered_content_only", return_value=True):
            self.assertEqual(self.decide(ready)["copyId"], "done_copy_ready_strategy")
        live = make_run(self.org, status="cancelled", step="write_article",
                        result={**reset, "deploy_v1": {"url": "https://example.com/blog/a"}})
        self.assertEqual(self.decide(live)["headline"], "Your article is live!")
        claims = make_run(self.org, status="cancelled", step="write_article", result=reset)
        with patch.object(founder_journey_p2, "claims_count", return_value=2):
            self.assertEqual(self.decide(claims)["copyId"], "ask_check_highlighted")
        bare = make_run(self.org, status="cancelled", step="write_article", result=reset)
        self.assertIsNone(self.decide(bare))

    def test_a_setup_that_failed_before_a_reset_has_no_card(self):
        connection = self.connection()
        run = make_run(self.org, workflow="article_system_setup", status="failed", step="setup",
                       request={"website_connection_id": str(connection.pk),
                                "connection_generation": connection.generation})
        self.assertEqual(self.decide(run)["copyId"], "fixing")
        type(connection).objects.filter(pk=connection.pk).update(generation=connection.generation + 1)
        self.assertIsNone(self.decide(run))

    def test_a_setup_stop_ours_to_fix_has_the_setup_copy_and_no_points_line(self):
        staff = make_run(self.org, workflow="article_system_setup", status="cancelled", step="setup",
                         result={"cancellation_reason": "system_stop", "cancelled_by": "staff",
                                 "cancellation_transition": "reset"})
        failed = make_run(self.org, workflow="article_system_setup", status="failed", step="setup")
        for run in (staff, failed):
            with self.subTest(status=run.status):
                decision = self.decide(run)
                self.assertEqual((decision["copyId"], decision["headline"], decision["stage"]),
                                 ("fixing", "We're fixing this", "setup"))
                self.assertEqual(decision["body"], founder_journey_p2.SETUP_FIXING_BODY)
                self.assertNotIn("points", decision["body"])

    def test_an_article_that_charged_nothing_reads_no_charge(self):
        unpaid = make_run(self.org, status="failed", step="write_article",
                          request={"delivery_mode": "content_only"})
        with patch("content_factory.no_delivery_refunds.points_outcome", return_value={"state": "pending", "points": None}):
            self.assertEqual(self.decide(unpaid)["body"], "No charge.")
            paid = make_run(self.org, status="failed", step="write_article",
                            request={"delivery_mode": "content_only", "roo_points_billing_status": "charged"})
            self.assertEqual(self.decide(paid)["body"], "We're checking the points for this article.")

    def test_a_failed_publish_of_a_paid_article_does_not_read_no_charge(self):
        source = make_run(self.org, status="completed", step="write_article",
                          request={"delivery_mode": "content_only", "roo_points_billing_status": "charged"})
        child = make_run(self.org, status="failed", step="publish", request={"delivery_mode": "content_only"})
        with patch("content_factory.publish_reviewed_draft.published_child_for_source", return_value=child), \
                patch("content_factory.no_delivery_refunds.points_outcome", return_value={"state": "pending", "points": None}):
            decision = self.decide(source)
        self.assertEqual(decision["runId"], source.run_id)
        self.assertNotEqual(decision["body"], "No charge.")


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
