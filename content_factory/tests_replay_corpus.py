"""Plan F5, level 1 (backend): every seeded founder failure passes or ends in its expected ask.

The records in ``content_factory/fixtures/replay`` are synthetic, shaped from
the catalogue, and fed to the backend function that decided the founder's
outcome: the system-stopped run follow-up (B24), the alert's lead code (A3)
and the empty research night streak (B23). No email, Slack or network call is
made; the email sender is a test double.
"""
from datetime import datetime, timedelta, timezone as dt_timezone
from unittest.mock import patch

from django.test import SimpleTestCase, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from workflow_runs.models import ContentFactoryRun

from . import replay_records
from .replay_records import RecordError, load_records, validate

RECORDS = {record["id"]: record for record in load_records()}
SEEDED_RUN_PKS = {4978, 5396}
SEEDED_ORGS = {30, 87, 173}


def _outcome_of_followup(run, decision):
    from . import system_stopped_runs as stopped
    if decision == "restarted":
        return "pass"
    if decision == "notice":
        run.refresh_from_db()
        return f"ask:{stopped.notice_for(run)['action']['id']}"
    return f"code:{replay_records.run_failure_code(run)}"


class ReplayCorpusTests(TestCase):
    """Each record through its handler; ``replay`` returns the outcome reached."""

    def replay(self, record):
        """Run one record in its own savepoint, rolled back, so records never share rows."""
        from django.db import transaction
        with transaction.atomic():
            outcome = getattr(self, "handle_" + record["replay"]["handler"].replace(".", "_"))(record)
            transaction.set_rollback(True)
        return outcome

    # --- handlers -------------------------------------------------------------------------------

    def handle_system_stopped_follow_up(self, record):
        """``system_stopped_runs.follow_up`` on the recorded run of a connected, verified site (B24)."""
        from . import system_stopped_runs as stopped
        from .founder_journey_testing import make_history_free_org
        from .tests_live_url_verification import MERGED, _connect, _next_js_target
        from .website_connections import contract_for
        inputs = record["inputs"]
        built = make_history_free_org(domain=inputs["domain"])
        connection = _connect(built, verified_sha=MERGED)
        connection.authorized_by = built.owner
        connection.capabilities = {**connection.capabilities, "publishingReady": True}
        connection.save(update_fields=["authorized_by", "capabilities"])
        _next_js_target(connection)
        saved = inputs["run"]
        run = ContentFactoryRun.objects.create(
            run_id=f"replay-{record['id']}", workflow=record["workflow"], domain=built.organization.domain,
            organization=built.organization, github_repo=connection.github_repo, run_request=contract_for(connection),
            status=saved["status"], current_step=saved["current_step"], error=saved["error"], result=saved["result"])
        stopped_at = timezone.now() - timedelta(hours=inputs["stopped_hours_ago"])
        ContentFactoryRun.objects.filter(pk=run.pk).update(updated_at=stopped_at)
        run.refresh_from_db()
        with patch.object(stopped, "_restart", side_effect=AssertionError("a replay never dispatches a restart")):
            decision = stopped.follow_up(run)
        return _outcome_of_followup(run, decision)

    def handle_alerts_run_blocking_code(self, record):
        """``founder_journey_alerts.run_blocking_code``: what the alert leads with (A3)."""
        from . import founder_journey_alerts as alerts
        saved = record["inputs"]["run"]
        code = alerts.run_blocking_code(ContentFactoryRun(status=saved["status"], result=saved["result"]))
        return f"code:{code}" if code else "pass"

    def handle_setup_resume_response(self, record):
        """Classify the worker's response to a reserved website setup retry."""
        from . import failure_policy_engine as policy
        from .self_heal_testing import ALL_FLAGS_ON, make_p2_org, make_run
        with override_settings(**ALL_FLAGS_ON):
            org = make_p2_org().organization
            run = make_run(org, stamped=True, workflow=record["workflow"],
                           result={"error_code": record["code"]}, status=record["inputs"]["status"])
            decision = policy.Decision("resume", None, "repair", record["inputs"]["attempt"], None, "policy")
            response = {"content_factory_status_code": record["http"][0]["status"]}
            with patch("content_factory.vibe_marketing_views._call_content_factory_run_action", return_value=response):
                outcome = policy._execute_run(run, decision, now=timezone.now())
        return "code:" + str(outcome.get("reason") or outcome.get("status"))

    def handle_research_empty_nights(self, record):
        """The ``research_empty`` callback for each recorded night of a daily automation (B23)."""
        from content_factory.models import (AutomationRun, AutomationRunStatus, NotificationChannelType,
                                            NotificationConsentState, NotificationDelivery)
        from core.models import User
        from integrations.services.notification_adapters import notification_context_for_run
        from integrations.services.research_automations import create_or_update_research_automation
        from organizations.models import Organization
        from tests.website_fixtures import CallbackFixtureClient
        inputs = record["inputs"]
        client = CallbackFixtureClient()
        client.credentials(HTTP_X_API_KEY="test-roo-key")
        org = Organization.objects.create(name="Replay founder", domain=inputs["domain"],
                                          competitors=inputs["competitors"], seed_keywords=inputs["seed_keywords"])
        user = User.objects.create_user(email=f"replay-founder@{inputs['domain']}")
        automation = create_or_update_research_automation(
            domain=org.domain, channel_type=NotificationChannelType.EMAIL, route_id=user.email, user=user,
            timezone_name="Australia/Melbourne", frequency_per_day=1, local_send_times=["08:00"],
            consent_state=NotificationConsentState.ACTIVE)
        first = datetime(2026, 11, 9, 21, 0, tzinfo=dt_timezone.utc)
        nights = []
        for day in range(inputs["nights"]):
            scheduled = first + timedelta(days=day)
            nights.append(AutomationRun.objects.create(
                automation=automation, scheduled_for_at=scheduled, local_date=(scheduled + timedelta(hours=11)).date(),
                slot_index=0, status=AutomationRunStatus.QUEUED, content_factory_run_id=f"replay-night-{day}",
                idempotency_key=f"research-automation:replay:{day}:0"))
        with override_settings(ROO_API_KEY="test-roo-key", INTERNAL_API_KEY="test-roo-key",
                               COMMUNITY_CHAT_FRONTEND_URL="https://chat.example.test"), \
                patch("integrations.services.notification_adapters._send_email",
                      return_value=(True, "replay-email", {})) as sent:
            for night in nights:
                response = client.post(reverse("content_factory_callback"), {
                    "event_type": "research_empty", "job_id": night.content_factory_run_id,
                    "run_id": night.content_factory_run_id, "domain": org.domain, "workflow": record["workflow"],
                    "outcome": "empty", "empty_reason": inputs["empty_reason"],
                    "empty_detail_code": inputs["empty_detail_code"],
                    "notification_context": notification_context_for_run(night)}, format="json")
                self.assertEqual(response.status_code, 200, response.content)
        if AutomationRun.objects.filter(automation=automation, status=AutomationRunStatus.FAILED).exists():
            return "code:automation_run_failed"
        if NotificationDelivery.objects.filter(event_type="error").exists():
            return "code:failure_notice_sent"
        notices = NotificationDelivery.objects.filter(event_type="research_empty").count()
        if notices == 0:
            return "pass"
        self.assertIn("Add seed topics or competitors", str(sent.call_args))
        return "ask:add_seed_topics" if notices == 1 else "code:repeated_empty_notice"

    # --- the corpus ----------------------------------------------------------------------------

    def test_every_seeded_record_passes_or_ends_in_its_expected_ask(self):
        for record_id, record in sorted(RECORDS.items()):
            with self.subTest(record=record_id):
                self.assertEqual(self.replay(record), record["expected"])

    def test_a_founder_cancel_of_the_same_run_gets_nothing(self):
        record = {**RECORDS["be-30-system-cancelled-article-run"]}
        record["inputs"] = {**record["inputs"], "run": {**record["inputs"]["run"], "error": "Cancelled by user."}}
        self.assertEqual(self.replay(record), "code:none")

    def test_two_empty_nights_are_quiet(self):
        record = {**RECORDS["be-vyavos-three-empty-nights"]}
        record["inputs"] = {**record["inputs"], "nights": 2}
        self.assertEqual(self.replay(record), "pass")


class ReplayRecordFormatTests(SimpleTestCase):
    def test_every_catalogue_case_on_the_backend_side_is_seeded(self):
        pks = {record["source"]["run_pk"] for record in RECORDS.values()}
        orgs = {record["source"]["org_id"] for record in RECORDS.values()}
        self.assertLessEqual(SEEDED_RUN_PKS, pks)
        self.assertLessEqual(SEEDED_ORGS, orgs)

    def test_every_record_names_a_handler_this_module_has(self):
        for record in RECORDS.values():
            with self.subTest(record=record["id"]):
                self.assertTrue(hasattr(ReplayCorpusTests, "handle_" + record["replay"]["handler"].replace(".", "_")))
                self.assertIs(record["source"]["synthetic"], True)

    def test_the_validator_refuses_secrets_personal_data_and_unreviewed_exports(self):
        base = RECORDS["be-5396-alert-leads-with-connection-code"]
        cases = {
            "unknown fields": lambda r: r.update(extra=1),
            "redaction_reviewed_by": lambda r: r["source"].update(synthetic=False),
            "expected must be": lambda r: r.update(expected="ok"),
            "secret_field": lambda r: r["inputs"].update(installation_token="x"),
            "github_token": lambda r: r["inputs"].update(note="ghp_" + "B" * 36),
            "email": lambda r: r["inputs"].update(owner="someone@founder-site.com.au"),
            "reserved example host": lambda r: r.update(http=[{"method": "GET", "url": "https://founder.com.au/",
                                                              "status": 200, "headers": {}, "body": ""}]),
        }
        import copy
        for message, mutate in cases.items():
            record = copy.deepcopy(base)
            mutate(record)
            with self.subTest(message=message), self.assertRaisesRegex(RecordError, message):
                validate(record)
