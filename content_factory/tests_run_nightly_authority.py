"""Real nightly dispatch and exact current service authority, without providers."""
from copy import deepcopy
from datetime import date
import json
import os
from pathlib import Path
from unittest.mock import patch

from django.test import TransactionTestCase, override_settings
from roo.models import Ledger
from workflow_runs.models import ContentFactoryRun
from .founder_journey_testing import ALL_P1_FLAGS_ON
from .models import ResearchedKeyword
from .run_ownership import owner_receipt, current_execution_ticket, wire_identity
from .self_heal_testing import make_p2_org
from .tests_run_event_authority import FLAGS


@override_settings(**FLAGS, **ALL_P1_FLAGS_ON, CONTENT_FACTORY_URL="https://nightly.fixture.invalid",
    CONTENT_FACTORY_API_KEY="synthetic-nightly-key", ROO_API_KEY="synthetic-nightly-service-key",
    ARTICLES_RUN_CONTROL_MODEL_USD="5", ARTICLES_RUN_CONTROL_MODEL_TOKENS=100000)
class NightlyAuthorityTests(TransactionTestCase):
    def dispatch(self, suffix="", admission=None):
        import requests
        from integrations.services.article_generation import http_requests
        from .services.island_refresh_scheduler import dispatch_island_refresh
        if admission is not None:
            from organizations.models import Organization
            from .tests_run_terminal_settlement import ActualPairedTerminalSettlementTests
            original = admission["run_request"]["backend_run_admission_v1"]
            ActualPairedTerminalSettlementTests.sequence(self, Organization, int(original["organization_id"]))
        self.fixture = fixture = make_p2_org("actual-nightly" + suffix + ".fixture.test")
        physical_id = "r3-actual-nightly" + suffix + "-worker"
        config = fixture.config
        config.pillar_strategy = {**config.pillar_strategy, "pillars": [{"name": "Garden ideas", "keyword": "balcony gardens"}]}
        config.save(update_fields=["pillar_strategy"])
        ResearchedKeyword.objects.create(organization=fixture.organization, keyword="balcony gardens",
            keyword_normalized="balcony gardens", volume=100, difficulty=12)
        sent = []
        def post(url, *, json, **kwargs):
            self.assertEqual(url, "https://nightly.fixture.invalid/api/runs/island-refresh")
            current = ContentFactoryRun.objects.get(result__run_owner_v1__identity__dispatch_key=json["client_request_id"])
            self.assertEqual(json["backend_run_admission_v1"], wire_identity(owner_receipt(current)))
            self.assertEqual(json["island_refresh_recovery_version"], 1)
            sent.append(deepcopy(json))
            response = requests.Response(); response.status_code = 202
            response._content = __import__("json").dumps({"run_id": physical_id, "status": "queued"}).encode()
            response._content_consumed = True
            return response
        with patch.object(http_requests, "post", side_effect=post) as remote:
            result = dispatch_island_refresh(fixture.organization, local_date=date(2026, 11, 3))
        self.assertEqual(result["status"], "dispatched", result)
        self.assertEqual(remote.call_count, 1)
        run = ContentFactoryRun.objects.get(run_id=physical_id)
        self.assertEqual(owner_receipt(run)["identity"]["principal"], {"kind": "scheduler", "service": "island_refresh"})
        self.assertEqual(sent[0], run.run_request)
        self.assertFalse(Ledger.objects.exists())
        if admission is not None:
            self.assertEqual(run.run_request, admission["run_request"])
            self.assertEqual(run.run_id, admission["run_id"])
        return run

    def test_actual_nightly_dispatch_keeps_version_and_budget_in_original_admission(self):
        run = self.dispatch()
        self.assertEqual(run.run_request["backend_run_budget_v1"], {
            "schema_version": 1, "runtime": "article", "max_model_usd": "5", "max_model_tokens": 100000})
        self.assertEqual(run.run_request["backend_run_execution_v1"], current_execution_ticket(run))
        if os.environ.get("R3_EXPORT_NIGHTLY_ADMISSION") == "1":
            value = {"schema_version": 1, "producer": "actual dispatch_island_refresh -> reserve_scheduler_dispatch -> HTTP, only external POST mocked",
                "run_id": run.run_id, "run_request": run.run_request,
                "control_request_example": {"schema_version": 1, "control_key": "a" * 64, "reason": "orphan_resume",
                    "backend_run_admission_v1": wire_identity(owner_receipt(run)),
                    "backend_run_execution_v1": current_execution_ticket(run)}}
            Path("/Users/samdonegan/Documents/Code/founder-journey-worktrees/recovery/R3_BE_ACTUAL_NIGHTLY_FOR_CF.json").write_text(json.dumps(value, indent=2) + "\n")

    def control(self, run, *, key="b" * 64, mutate=None, reason="orphan_resume"):
        import requests
        from django.db import connection
        from rest_framework.test import APIClient
        from .run_event_authority import receive_authoritative_run_events
        from .tests_run_event_authority import batch
        journal = batch(run, [{"status": "running", "step": "load_context"}])
        body = {"schema_version": 1, "control_key": key, "reason": reason,
            "backend_run_admission_v1": wire_identity(owner_receipt(run)),
            "backend_run_execution_v1": current_execution_ticket(run)}
        budget = {"schema_version": 1, "run_id": run.run_id, **{name: body[name] for name in
            ("backend_run_admission_v1", "backend_run_execution_v1")},
            "backend_run_budget_v1": deepcopy(run.run_request.get("backend_run_budget_v1")), "event_cursor": 1,
            "used_model_usd_upper_bound": "0.25", "used_model_tokens": 100, "uncertain_calls": 1}
        history = {"schema_version": 1, "run_id": run.run_id, "batches": [journal], "next_cursor": 1, "has_more": False}
        client = APIClient(); client.credentials(HTTP_X_API_KEY="synthetic-nightly-service-key")
        def get(url, **kwargs):
            self.assertFalse(connection.in_atomic_block)
            self.assertFalse(kwargs["allow_redirects"])
            if mutate is not None and url.endswith("/execution-budget"):
                mutate()
            response = requests.Response(); response.status_code = 200
            response._content = json.dumps(budget if url.endswith("/execution-budget") else history).encode()
            response._content_consumed = True
            return response
        with patch("requests.get", side_effect=get) as remote:
            response = client.post("/api/content-factory/runs/" + run.run_id + "/execution-ticket", body, format="json")
        return response, remote.call_count, body

    def test_actual_nightly_control_uses_real_budget_history_and_reducer_then_replays_same_ticket_flags_off(self):
        from rest_framework.test import APIClient
        run = self.dispatch()
        original = deepcopy(run.run_request)
        with override_settings(ARTICLES_RUN_OWNERSHIP_ENABLED=False, ARTICLES_RUN_FACT_AUTHORITY_ENABLED=False,
                ARTICLES_RUN_EVENT_AUTHORITY_ENABLED=False, ARTICLES_RUN_CONTROL_MODEL_USD="0", ARTICLES_RUN_CONTROL_MODEL_TOKENS=0):
            response, calls, body = self.control(run)
            self.assertEqual(response.status_code, 200, response.data)
            self.assertEqual(calls, 2)
            self.assertEqual(response.data["backend_run_execution_v1"]["generation"], 1)
            run.refresh_from_db()
            before = ContentFactoryRun.objects.values().get(pk=run.pk)
            client = APIClient(); client.credentials(HTTP_X_API_KEY="synthetic-nightly-service-key")
            with patch("requests.get") as remote:
                replay = client.post("/api/content-factory/runs/" + run.run_id + "/execution-ticket", body, format="json")
            self.assertEqual(replay.status_code, 200, replay.data)
            self.assertEqual(replay.data, response.data)
            remote.assert_not_called()
            self.assertEqual(ContentFactoryRun.objects.values().get(pk=run.pk), before)
        run.refresh_from_db()
        self.assertEqual(run.run_request, original)
        self.assertEqual(owner_receipt(run)["identity"]["principal"], {"kind": "scheduler", "service": "island_refresh"})
        self.assertFalse(Ledger.objects.exists())

    def test_keyword_removal_freeze_pause_or_stop_hold_before_any_external_reads(self):
        from .models import OrganizationContentConfig, NotificationChannel, ResearchAutomation
        from django.utils import timezone
        mutations = ("keywords", "frozen", "paused", "stop", "strategy", "offboard", "workshop")
        for name in mutations:
            with self.subTest(name=name):
                run = self.dispatch("-" + name)
                org = self.fixture.organization
                config = OrganizationContentConfig.objects.get(organization=org)
                if name == "keywords":
                    ResearchedKeyword.objects.filter(organization=org).delete()
                elif name == "frozen":
                    config.pillar_strategy["startup_team"] = {"version": 1, "frozen": True, "owner_user_id": self.fixture.owner.pk}
                    config.save(update_fields=["pillar_strategy"])
                elif name == "paused":
                    channel = NotificationChannel.objects.create(organization=org, user=self.fixture.owner,
                        channel_type="slack", route_id="C_SYNTHETIC", display_name="Synthetic paused channel")
                    ResearchAutomation.objects.create(organization=org, user=self.fixture.owner, notification_channel=channel, status="paused")
                elif name == "stop":
                    from .run_user_stop import record_user_denial
                    record_user_denial(run, user=self.fixture.owner, actor_id="mlai_user:" + str(self.fixture.owner.pk))
                elif name == "strategy":
                    config.pillar_strategy = {key: value for key, value in config.pillar_strategy.items() if key != "pillars"}
                    config.save(update_fields=["pillar_strategy"])
                elif name == "offboard":
                    config.pillar_strategy["previous_owner"] = {"user_id": self.fixture.owner.pk}
                    config.save(update_fields=["pillar_strategy"])
                run.refresh_from_db()
                before = ContentFactoryRun.objects.values().get(pk=run.pk)
                from datetime import timedelta
                windows = [{"start": (timezone.now() - timedelta(hours=1)).isoformat(),
                    "end": (timezone.now() + timedelta(hours=1)).isoformat()}] if name == "workshop" else []
                with override_settings(ARTICLES_WORKSHOP_WINDOWS=windows):
                    response, calls, _ = self.control(run)
                self.assertEqual(response.status_code, 409, response.data)
                self.assertEqual(calls, 0)
                self.assertEqual(ContentFactoryRun.objects.values().get(pk=run.pk), before)

    def test_keyword_removal_during_budget_read_is_rechecked_before_ticket_grant(self):
        run = self.dispatch()
        response, calls, body = self.control(run, mutate=lambda: ResearchedKeyword.objects.filter(organization=run.organization).delete())
        self.assertEqual(response.status_code, 409, response.data)
        self.assertEqual(calls, 2)
        run.refresh_from_db()
        self.assertEqual(current_execution_ticket(run)["generation"], 0)
        self.assertNotIn(body["control_key"], (run.result.get("run_owner_control_v1") or {}).get("grants", {}))
        self.assertFalse(Ledger.objects.exists())

    def test_other_service_workflow_unversioned_or_bound_originals_never_gain_the_nightly_gate(self):
        from .run_ownership import reserve_admitted_run, bind_admitted_run, scheduler_principal, BUDGET_KEY, WIRE_REQUEST_KEY, WIRE_EXECUTION_KEY
        original = self.dispatch()
        variants = [
            ("other-service", "island_refresh", "daily_discovery", {}),
            ("other-workflow", "auto_discovery", "island_refresh", {}),
            ("unversioned", "island_refresh", "island_refresh", {"island_refresh_recovery_version": None}),
            ("bool-version", "island_refresh", "island_refresh", {"island_refresh_recovery_version": True}),
            ("wrong-origin", "island_refresh", "island_refresh", {"request_source": "roo_slackbot"}),
            ("paid-brief", "island_refresh", "island_refresh", {"island_research_brief": {"subject": "Paid topic idea"}}),
            ("repo", "island_refresh", "island_refresh", {"github_repo": "synthetic/site"}),
            ("source", "island_refresh", "island_refresh", {"source_run_id": "another-worker"}),
        ]
        for name, workflow, service, changes in variants:
            with self.subTest(name=name):
                payload = {key: deepcopy(value) for key, value in original.run_request.items()
                    if key not in {BUDGET_KEY, WIRE_REQUEST_KEY, WIRE_EXECUTION_KEY}}
                payload.update(changes, client_request_id="nightly-refusal-" + name)
                if name == "unversioned":
                    payload.pop("island_refresh_recovery_version")
                run = reserve_admitted_run(organization=self.fixture.organization,
                    principal=scheduler_principal(service), workflow=workflow, payload=payload)
                run = bind_admitted_run(run, "refused-nightly-" + name)
                before = ContentFactoryRun.objects.values().get(pk=run.pk)
                response, calls, body = self.control(run)
                self.assertEqual(response.status_code, 409, response.data)
                self.assertEqual(calls, 0)
                self.assertEqual(ContentFactoryRun.objects.values().get(pk=run.pk), before)
        self.assertFalse(Ledger.objects.exists())

    def test_original_budget_is_required_and_current_ticket_cannot_acquire_source_authority(self):
        from django.db import transaction
        from .run_execution_control import append_execution_ticket
        from .run_ownership import reserve_admitted_run, bind_admitted_run, scheduler_principal, BUDGET_KEY, WIRE_REQUEST_KEY, WIRE_EXECUTION_KEY
        run = self.dispatch()
        payload = {key: deepcopy(value) for key, value in run.run_request.items()
            if key not in {BUDGET_KEY, WIRE_REQUEST_KEY, WIRE_EXECUTION_KEY}}
        payload["client_request_id"] = "nightly-no-original-budget"
        with override_settings(ARTICLES_RUN_CONTROL_MODEL_USD="0", ARTICLES_RUN_CONTROL_MODEL_TOKENS=0):
            no_budget = reserve_admitted_run(organization=self.fixture.organization,
                principal=scheduler_principal("island_refresh"), workflow="island_refresh", payload=payload)
        no_budget = bind_admitted_run(no_budget, "nightly-no-cap-worker")
        response, calls, _ = self.control(no_budget)
        self.assertEqual(response.status_code, 409, response.data)
        self.assertEqual(calls, 0)
        scope = deepcopy(current_execution_ticket(run)["scope"])
        scope["expected_source_sha"] = "a" * 40
        with transaction.atomic():
            current = ContentFactoryRun.objects.select_for_update().get(pk=run.pk)
            append_execution_ticket(current, control_key="c" * 64, reason="source_repin", scope=scope)
        run.refresh_from_db()
        before = ContentFactoryRun.objects.values().get(pk=run.pk)
        response, calls, _ = self.control(run)
        self.assertEqual(response.status_code, 409, response.data)
        self.assertEqual(calls, 0)
        self.assertEqual(ContentFactoryRun.objects.values().get(pk=run.pk), before)

    def test_three_delivered_local_days_hold_without_mutating_policy_or_counting_same_day_fanout(self):
        from datetime import timedelta
        from django.utils import timezone
        from .models import AutomationRun, NotificationChannel, NotificationDelivery, ResearchAutomation
        from .run_execution_control import require_current_principal
        run = self.dispatch()
        now = timezone.now()
        channel = NotificationChannel.objects.create(organization=run.organization, user=self.fixture.owner,
            channel_type="slack", route_id="C_SYNTHETIC", display_name="Synthetic current delivery")
        automation = ResearchAutomation.objects.create(organization=run.organization, user=self.fixture.owner,
            notification_channel=channel, status="active")
        ResearchAutomation.objects.filter(pk=automation.pk).update(created_at=now - timedelta(days=5))
        for day in (1, 2, 3):
            sent = now - timedelta(days=day)
            slot = AutomationRun.objects.create(automation=automation, scheduled_for_at=sent,
                local_date=sent.date(), slot_index=0, idempotency_key="nightly-pause-day-" + str(day))
            if day < 3:
                for fanout in range(3):
                    NotificationDelivery.objects.create(automation_run=slot, channel=channel, event_type="topic_selection",
                        status="sent", delivered_at=sent, idempotency_key="nightly-day-" + str(day) + "-fanout-" + str(fanout))
            else:
                require_current_principal(run)
                NotificationDelivery.objects.create(automation_run=slot, channel=channel, event_type="topic_selection",
                    status="sent", delivered_at=sent, idempotency_key="nightly-third-distinct-day")
        before = ContentFactoryRun.objects.values().get(pk=run.pk)
        automation_before = ResearchAutomation.objects.values().get(pk=automation.pk)
        config_before = type(self.fixture.config).objects.values().get(pk=self.fixture.config.pk)
        response, calls, _ = self.control(run)
        self.assertEqual(response.status_code, 409, response.data)
        self.assertEqual(calls, 0)
        self.assertEqual(ContentFactoryRun.objects.values().get(pk=run.pk), before)
        self.assertEqual(ResearchAutomation.objects.values().get(pk=automation.pk), automation_before)
        self.assertEqual(type(self.fixture.config).objects.values().get(pk=self.fixture.config.pk), config_before)
        ResearchAutomation.objects.filter(pk=automation.pk).update(metadata={"daily_research": {
            "last_engaged_at": "2026-02-31T00:00:00+00:00"}})
        malformed = ResearchAutomation.objects.values().get(pk=automation.pk)
        response, calls, _ = self.control(run)
        self.assertEqual(response.status_code, 409, response.data)
        self.assertEqual(calls, 0)
        self.assertEqual(ResearchAutomation.objects.values().get(pk=automation.pk), malformed)

    def test_actual_cf_orphan_control_request_and_budget_receive_one_real_backend_ticket(self):
        import requests
        from django.db import connection
        from rest_framework.test import APIClient
        value = json.loads((Path(__file__).parent / "testdata/r3/nightly_actual_control_request.json").read_text())
        run = self.dispatch(admission=value["original_backend_admission"])
        self.assertEqual(value["body"]["backend_run_admission_v1"], wire_identity(owner_receipt(run)))
        self.assertEqual(value["body"]["backend_run_execution_v1"], current_execution_ticket(run))
        original = deepcopy(run.run_request)
        def get(url, **kwargs):
            self.assertFalse(connection.in_atomic_block)
            self.assertFalse(kwargs["allow_redirects"])
            self.assertEqual(kwargs["headers"]["X-API-KEY"], "synthetic-nightly-key")
            if url.endswith("/execution-budget"):
                data = value["execution_budget"]
            else:
                self.assertTrue(url.endswith("/events"), url)
                self.assertEqual(kwargs["params"], {"after_seq": 0, "limit": 100})
                data = value["history"]
            response = requests.Response(); response.status_code = 200
            response._content = json.dumps(data).encode(); response._content_consumed = True
            return response
        client = APIClient(); client.credentials(HTTP_X_API_KEY="synthetic-nightly-service-key")
        url = "/api/content-factory/runs/" + run.run_id + "/execution-ticket"
        with override_settings(ARTICLES_RUN_OWNERSHIP_ENABLED=False, ARTICLES_RUN_FACT_AUTHORITY_ENABLED=False,
                ARTICLES_RUN_EVENT_AUTHORITY_ENABLED=False, ARTICLES_RUN_CONTROL_MODEL_USD="0", ARTICLES_RUN_CONTROL_MODEL_TOKENS=0):
            with patch("requests.get", side_effect=get) as remote:
                response = client.post(url, value["body"], format="json")
            self.assertEqual(response.status_code, 200, response.data)
            self.assertEqual(remote.call_count, 2)
            granted = deepcopy(response.data)
            self.assertEqual(granted["backend_run_execution_v1"]["generation"], 1)
            self.assertEqual(granted["backend_run_admission_v1"], value["body"]["backend_run_admission_v1"])
            run.refresh_from_db()
            before_replay = ContentFactoryRun.objects.values().get(pk=run.pk)
            with patch("requests.get") as remote:
                replay = client.post(url, value["body"], format="json")
            self.assertEqual(replay.status_code, 200, replay.data)
            self.assertEqual(replay.data, granted)
            remote.assert_not_called()
            self.assertEqual(ContentFactoryRun.objects.values().get(pk=run.pk), before_replay)
        run.refresh_from_db()
        self.assertEqual(run.run_request, original)
        self.assertFalse(Ledger.objects.exists())
        if os.environ.get("R3_EXPORT_NIGHTLY_CONTROL_GRANT") == "1":
            Path("/Users/samdonegan/Documents/Code/founder-journey-worktrees/recovery/R3_BE_ACTUAL_NIGHTLY_CONTROL_GRANTED.json").write_text(
                json.dumps(granted, indent=2) + "\n")
        completed = json.loads((Path(__file__).parent / "testdata/r3/nightly_actual_completed_worker.json").read_text())
        self.assertEqual(completed["resume"]["backend_grant"], granted)
        self.assertEqual(completed["run_request"]["backend_run_admission_v1"], original["backend_run_admission_v1"])
        self.assertEqual(completed["run_request"]["backend_run_execution_v1"], granted["backend_run_execution_v1"])
        self.assertIs(completed["run_request"]["include_expansion"], True)
        self.assertEqual((completed["expansion_calls"], completed["repository_authorization_calls"]), (1, 0))
        # Pagination removes only the already committed prefix. Every retained
        # event/ticket/digest is unchanged from the actual authenticated CF GET.
        page = deepcopy(completed["history"])
        page["batches"] = [{**batch, "events": [event for event in batch["events"] if event["seq"] > 4]}
            for batch in page["batches"] if any(event["seq"] > 4 for event in batch["events"])]
        def final_history(url, **kwargs):
            self.assertTrue(url.endswith("/events"), url)
            self.assertEqual(kwargs["params"], {"after_seq": 4, "limit": 100})
            self.assertFalse(connection.in_atomic_block)
            reply = requests.Response(); reply.status_code = 200
            reply._content = json.dumps(page).encode(); reply._content_consumed = True
            return reply
        from .run_reconciliation import reconcile_authoritative_run
        from .run_ownership import EVENT_RESULT_KEY, RECONCILIATION_RESULT_KEY, TOPIC_DELIVERY_RESULT_KEY
        from .run_event_authority import receive_authoritative_run_events
        with patch("requests.get", side_effect=final_history) as remote:
            outcome = reconcile_authoritative_run(run.run_id, refresh=True)
        self.assertEqual(outcome["outcome"], "history_received", outcome)
        self.assertEqual(remote.call_count, 1)
        run.refresh_from_db()
        self.assertEqual(run.status, "completed")
        self.assertEqual(current_execution_ticket(run), granted["backend_run_execution_v1"])
        self.assertEqual(run.result[RECONCILIATION_RESULT_KEY]["cursor"], 28)
        self.assertEqual(len(run.result[EVENT_RESULT_KEY]["events"]), 28)
        self.assertNotIn(TOPIC_DELIVERY_RESULT_KEY, run.result)
        before_replay = ContentFactoryRun.objects.values().get(pk=run.pk)
        for batch in completed["history"]["batches"]:
            replay = receive_authoritative_run_events(batch)
            self.assertEqual(replay.status_code, 200, replay.data)
            self.assertEqual(replay.data["accepted"], 0)
        self.assertEqual(ContentFactoryRun.objects.values().get(pk=run.pk), before_replay)
        self.assertEqual(run.run_request, original)
        self.assertFalse(Ledger.objects.exists())
