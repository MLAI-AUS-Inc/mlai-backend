"""The P2 alert store (be-registry): IF-P2-23 reasons and IF-P2-19 acknowledgements, due times and details."""
from __future__ import annotations

from datetime import datetime, timezone as dt_timezone
import json
import re
from unittest.mock import patch

from django.test import SimpleTestCase, TestCase, override_settings

from . import founder_journey_alerts as alerts
from .models import OrganizationContentConfig
from .self_heal_testing import (
    ALL_FLAGS_ON, FRIDAY_4PM_MELBOURNE, fake_provider, make_named_legacy_org, make_p2_org, make_stamped_run,
    missing_provider,
)
from workflow_runs.models import ContentFactoryRun

TODAY_REASONS = {
    "run_terminal": "A founder run stopped",
    "run_blocked": "A founder run is blocked",
    "startup_autofill_blocked": "Startup autofill is blocked",
    "no_draft_24h": "A startup has no delivered draft 24 hours after it was created",
    "your_turn_72h": "A founder has been waiting on their turn for over 72 hours",
    "domain_claim_disputed": "A founder tried to claim a domain another startup holds",
    "first_article_attempts_exhausted": "A founder used every free first-article attempt",
    "free_cap_spike": "Free first-article reservations spiked",
}
P2_REASONS = {
    "hosting_retired": "Move retired blog URLs to the startup website; see the site retirement receipt",
    "our_team_item": "A startup has an item no automatic path can finish",
    "unregistered_failure_code": "A run reported a failure code the registry does not know",
    "codeless_terminal": "A run stopped without a failure code",
    "setup_attempts_spike": "A startup had more than 6 setup attempts today",
    "queue_age": "A queued run has waited more than 10 minutes",
    "provider_error_rate": "Provider errors passed 20% of active runs",
    "throttle_spike": "Founders hit more than 5 rate limits in an hour",
    "refresh_failure_spike": "Sign-in refresh failures passed 3 times the weekly baseline",
    "github_callback_failures": "GitHub callback failures spiked",
    "free_budget_exhausted": "A free article ran out of model budget",
    "claim_findings_delivered": "A draft was delivered with claims to check",
    "repair_loop_exhausted": "A preview repair loop ran out of attempts",
    "pull_request_cap": "A startup reached 3 MLAI pull requests today",
    "ladder_exhausted": "Every blog route failed for a startup's website",
    "self_heal_capped": "Self-healing stopped retrying an item",
    "source_change_repeated": "A run's website source changed a third time",
    "deploy_not_detected": "A merged article was not deployed within 30 minutes",
    "merge_rejected_twice": "GitHub rejected an MLAI merge twice",
    "join_request_unanswered": "A team join request has waited 72 hours",
}
PRIVATE_DETAILS = {
    "owner_email": "founder@example.test", "founder_name": "Ada", "article_title": "Secret", "contact": "a@b.test",
    "link": "https://example.test/admin/1", "path_hint": "/my-startup/runs/1", "note": "Free text with spaces",
}


class AlertReasonTests(SimpleTestCase):
    """IF-P2-23: the 19 reasons and labels exist and the 8 existing ones are unchanged."""

    def test_reasons_and_labels(self):
        self.assertEqual(len(P2_REASONS), 20)
        for reason, label in TODAY_REASONS.items():
            self.assertIn(reason, alerts.ALERT_REASONS)
            self.assertEqual(alerts.REASON_LABELS[reason], label)
        for reason, label in P2_REASONS.items():
            self.assertIn(reason, alerts.ALERT_REASONS)
            self.assertEqual(alerts.REASON_LABELS[reason], label)
        self.assertEqual(alerts.ALERT_REASONS, frozenset(TODAY_REASONS) | frozenset(P2_REASONS))
        self.assertEqual(set(alerts.REASON_LABELS), set(alerts.ALERT_REASONS))
        self.assertEqual(alerts.DETECTED_REASONS,
                         ("run_terminal", "run_blocked", "startup_autofill_blocked", "your_turn_72h", "no_draft_24h"))

    def test_labels_carry_no_dashes(self):
        for label in P2_REASONS.values():
            self.assertNotRegex(label, "[\u2013\u2014]")
            self.assertNotIn(" - ", label)


class AckDueAtTests(SimpleTestCase):
    """IF-P2-19: one business day after queueing, Monday to Friday, Australia/Melbourne."""

    def test_friday_afternoon_is_due_monday_afternoon(self):
        due = alerts.ack_due_at(FRIDAY_4PM_MELBOURNE)
        local = due.astimezone(alerts.ACK_TIMEZONE)
        self.assertEqual((local.strftime("%A"), local.hour, local.minute), ("Monday", 16, 0))
        self.assertEqual(local.date().isoformat(), "2026-11-02")

    def test_weekdays_and_weekends(self):
        cases = (
            (datetime(2026, 10, 26, 23, 0, tzinfo=dt_timezone.utc), "Wednesday 2026-10-28 10:00"),  # Tue 10:00 AEDT
            (datetime(2026, 10, 29, 13, 30, tzinfo=dt_timezone.utc), "Monday 2026-11-02 00:30"),  # Fri 00:30 AEDT
            (datetime(2026, 10, 31, 1, 0, tzinfo=dt_timezone.utc), "Monday 2026-11-02 12:00"),  # Sat 12:00 AEDT
            (datetime(2026, 11, 1, 1, 0, tzinfo=dt_timezone.utc), "Monday 2026-11-02 12:00"),  # Sun 12:00 AEDT
        )
        for queued, expected in cases:
            with self.subTest(queued=queued):
                local = alerts.ack_due_at(queued).astimezone(alerts.ACK_TIMEZONE)
                self.assertEqual(local.strftime("%A %Y-%m-%d %H:%M"), expected)
        self.assertEqual(alerts.ack_due_at(FRIDAY_4PM_MELBOURNE).tzinfo, dt_timezone.utc)

    def test_dst_change_keeps_the_wall_clock(self):
        # Melbourne moves to AEDT on Sunday 4 Oct 2026; Friday 2 Oct 16:00 AEST is due Monday 5 Oct 16:00 AEDT.
        queued = datetime(2026, 10, 2, 6, 0, tzinfo=dt_timezone.utc)
        local = alerts.ack_due_at(queued).astimezone(alerts.ACK_TIMEZONE)
        self.assertEqual(local.strftime("%Y-%m-%d %H:%M %Z"), "2026-10-05 16:00 AEDT")


class AlertStoreTests(TestCase):
    """IF-P2-19: acknowledgements, due times and ids-only details."""

    def setUp(self):
        self.built = make_p2_org()
        self.org = self.built.organization

    def _receipts(self):
        config = OrganizationContentConfig.objects.get(organization=self.org)
        return config.pillar_strategy[alerts.RECEIPTS_KEY]

    def test_acknowledge_once(self):
        run = make_stamped_run(self.org)
        alert_id = alerts.queue_founder_journey_alert(organization=self.org, alert_reason="our_team_item", run=run)
        config = OrganizationContentConfig.objects.get(organization=self.org)
        updated_at = config.updated_at
        first = alerts.acknowledge_founder_journey_alert(self.org, alert_id, by_user=self.built.owner)
        self.assertEqual(first["acknowledged_by_user_id"], self.built.owner.pk)
        self.assertTrue(first["acknowledged_at"])
        other = make_named_legacy_org("mlai").owner
        second = alerts.acknowledge_founder_journey_alert(self.org, alert_id, by_user=other)
        self.assertEqual(second, first)
        self.assertEqual(self._receipts()[alert_id], first)
        self.assertEqual(OrganizationContentConfig.objects.get(pk=config.pk).updated_at, updated_at)
        mirrored = ContentFactoryRun.objects.get(pk=run.pk).result[alerts.RECEIPTS_KEY]["our_team_item"]
        self.assertEqual((mirrored["acknowledged_at"], mirrored["acknowledged_by_user_id"]),
                         (first["acknowledged_at"], self.built.owner.pk))

    def test_daily_slots_dedupe_within_one_day_and_recur_next_day(self):
        first = alerts.queue_founder_journey_alert(organization=self.org, alert_reason="pull_request_cap",
                                                   slot="day-2026-11-02")
        replay = alerts.queue_founder_journey_alert(organization=self.org, alert_reason="pull_request_cap",
                                                    slot="day-2026-11-02")
        tomorrow = alerts.queue_founder_journey_alert(organization=self.org, alert_reason="pull_request_cap",
                                                      slot="day-2026-11-03")
        self.assertEqual(first, replay)
        self.assertNotEqual(first, tomorrow)
        self.assertEqual(set(self._receipts()), {first, tomorrow})

    def test_invalid_or_non_p2_slots_are_rejected(self):
        for reason, slot in (("run_terminal", "day-2026-11-02"), ("pull_request_cap", "private detail@email.test")):
            with self.subTest(reason=reason, slot=slot), self.assertRaises(ValueError):
                alerts.queue_founder_journey_alert(organization=self.org, alert_reason=reason, slot=slot)

    def test_acknowledge_unknown_alert(self):
        self.assertIsNone(alerts.acknowledge_founder_journey_alert(self.org, "nope", by_user=self.built.owner))
        self.assertIsNone(alerts.acknowledge_founder_journey_alert(None, "nope", by_user=self.built.owner))

    def test_new_reasons_carry_their_due_time(self):
        with patch.object(alerts.timezone, "now", return_value=FRIDAY_4PM_MELBOURNE):
            alert_id = alerts.queue_founder_journey_alert(organization=self.org, alert_reason="queue_age",
                                                          run=make_stamped_run(self.org))
        due = self._receipts()[alert_id]["details"]["ack_due_at"]
        local = datetime.fromisoformat(due).astimezone(alerts.ACK_TIMEZONE)
        self.assertEqual(local.strftime("%A %H:%M"), "Monday 16:00")

    def test_existing_reasons_keep_todays_receipt_until_staff_can_act(self):
        alert_id = alerts.queue_founder_journey_alert(organization=self.org, alert_reason="run_terminal",
                                                      run=make_stamped_run(self.org))
        self.assertEqual(self._receipts()[alert_id]["details"], {})
        for flag in ("MY_STARTUP_STAFF_CONSOLE_ENABLED", "ARTICLES_STAFF_ALERT_RULES_ENABLED"):
            with self.subTest(flag=flag), override_settings(**{flag: True}):
                alert_id = alerts.queue_founder_journey_alert(organization=self.org, alert_reason="run_blocked",
                                                              run=make_stamped_run(self.org))
                self.assertIn("ack_due_at", self._receipts()[alert_id]["details"])

    def test_details_hold_ids_only(self):
        details = {"founder_user_id": self.built.owner.pk, "item": "first_article", "code": "NOT_A_CODE",
                   "run_id": "run-abc", "attempts": 3, "at_risk": True,
                   "last_transitions": [{"at": "2026-10-30T01:00:00+00:00", "from": "working", "to": "your_turn"},
                                        {"at": "2026-10-30T02:00:00+00:00", "from": "your_turn", "to": "stopped"},
                                        {"at": "nonsense", "from": "a@b.test", "to": "working slow"},
                                        {"at": "2026-10-30T04:00:00+10:00", "from": "stopped", "to": "working"}],
                   **PRIVATE_DETAILS}
        alert_id = alerts.queue_founder_journey_alert(organization=self.org, alert_reason="our_team_item",
                                                      run=make_stamped_run(self.org), details=details)
        stored = self._receipts()[alert_id]["details"]
        for key in PRIVATE_DETAILS:
            self.assertNotIn(key, stored)
        self.assertEqual(stored["founder_user_id"], self.built.owner.pk)
        self.assertEqual(stored["item"], "first_article")
        self.assertEqual(stored["code"], "NOT_A_CODE")
        self.assertEqual(len(stored["last_transitions"]), 3)
        self.assertEqual(stored["last_transitions"][1], {"at": None, "from": None, "to": None})
        self.assertEqual(stored["last_transitions"][2]["at"], "2026-10-29T18:00:00+00:00")
        text = json.dumps(stored)
        self.assertNotIn("@", text)
        self.assertNotRegex(text, r"https?://|/my-startup|Ada|Secret|\s{2,}")

    def test_slack_text_is_unchanged_by_the_p2_details(self):
        run = make_stamped_run(self.org)
        receipt = {"alert_reason": "run_terminal", "attempts": 1,
                   "details": {"website_operation": "prepare", "ack_due_at": "2026-11-02T05:00:00+00:00",
                               "last_transitions": []}}
        context = {"org_id": self.org.pk, "domain": "", "company_id": "", "run_id": run.run_id, "workflow": "x",
                   "run_status": "failed", "stop_code": "", "step_attempts": None, "staff_link": ""}
        text = alerts.alert_text(receipt, context)
        self.assertIn("Details website_operation=prepare.", text)
        self.assertNotIn("ack_due_at", text)


@override_settings(ARTICLES_STAFF_ALERT_RULES_ENABLED=True)
class EnrichmentSeamTests(TestCase):
    """IF-P2-24: ``staff_alert_rules.enrich_alert_details``, missing and fake."""

    def setUp(self):
        self.built = make_p2_org()
        self.org = self.built.organization

    def _details(self, alert_id):
        return OrganizationContentConfig.objects.get(organization=self.org).pillar_strategy[alerts.RECEIPTS_KEY][alert_id]["details"]

    def test_provider_missing_keeps_todays_details(self):
        with missing_provider(alerts.ENRICH_MODULE):
            alert_id = alerts.queue_founder_journey_alert(organization=self.org, alert_reason="run_blocked",
                                                          run=make_stamped_run(self.org), details={"step": "plan"})
        details = self._details(alert_id)
        self.assertEqual(set(details), {"step", "ack_due_at"})

    def test_fake_provider_adds_ids_and_names_are_dropped(self):
        seen = {}

        def enrich(receipt, *, organization, run):
            seen.update(receipt=receipt, organization=organization, run=run)
            return {"founder_user_id": 42, "item": "first_article", "attempts": 2, "owner_email": "x@y.test",
                    "founder_name": "Ada", "link": "https://example.test/x"}

        run = make_stamped_run(self.org)
        with fake_provider(alerts.ENRICH_MODULE, enrich_alert_details=enrich):
            alert_id = alerts.queue_founder_journey_alert(organization=self.org, alert_reason="our_team_item", run=run,
                                                          details={"step": "plan"})
        details = self._details(alert_id)
        self.assertEqual({key: details[key] for key in ("founder_user_id", "item", "attempts", "step")},
                         {"founder_user_id": 42, "item": "first_article", "attempts": 2, "step": "plan"})
        self.assertFalse({"owner_email", "founder_name", "link"} & set(details))
        self.assertEqual(seen["organization"], self.org)
        self.assertEqual(seen["run"], run)
        self.assertEqual(seen["receipt"]["alert_reason"], "our_team_item")

    def test_a_broken_provider_never_stops_the_alert(self):
        def boom(receipt, *, organization, run):
            raise RuntimeError("enrich failed")

        with fake_provider(alerts.ENRICH_MODULE, enrich_alert_details=boom):
            with self.assertLogs("content_factory.founder_journey_alerts", "WARNING") as logs:
                alert_id = alerts.queue_founder_journey_alert(organization=self.org, alert_reason="queue_age",
                                                              run=make_stamped_run(self.org))
        self.assertIn("founder_journey_alert_enrich_failed", logs.output[0])
        self.assertIn("ack_due_at", self._details(alert_id))

    def test_rules_flag_off_never_calls_the_provider(self):
        calls = []
        with override_settings(ARTICLES_STAFF_ALERT_RULES_ENABLED=False), \
                fake_provider(alerts.ENRICH_MODULE, enrich_alert_details=lambda *a, **k: calls.append(1) or {}):
            alerts.queue_founder_journey_alert(organization=self.org, alert_reason="run_terminal",
                                               run=make_stamped_run(self.org))
        self.assertEqual(calls, [])


class LegacyAlertTests(TestCase):
    """With every P2 flag on, a legacy org's alert keeps its reason, id and receipt shape.

    The only addition is the staff due time in the details, which the staff
    console (for every startup, IF-P2-19) reads; the Slack text leaves it out.
    """

    @override_settings(**ALL_FLAGS_ON)
    def test_legacy_receipt_shape(self):
        built = make_named_legacy_org("talathrive")
        with missing_provider(alerts.ENRICH_MODULE):
            alert_id = alerts.queue_founder_journey_alert(organization=built.organization, alert_reason="run_terminal",
                                                          run=built.runs[0])
        receipt = OrganizationContentConfig.objects.get(organization=built.organization).pillar_strategy[alerts.RECEIPTS_KEY][alert_id]
        self.assertEqual(alert_id, alerts.founder_journey_alert_id(organization_id=built.organization.pk,
                                                                   alert_reason="run_terminal",
                                                                   run_id=built.runs[0].run_id))
        self.assertEqual(set(receipt), {"alert_id", "alert_reason", "state", "attempts", "sent_at", "queued_at",
                                        "company_id", "run_id", "run_pk", "details"})
        self.assertEqual(set(receipt["details"]), {"ack_due_at"})
        self.assertTrue(re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(\.\d+)?\+00:00", receipt["details"]["ack_due_at"]))
