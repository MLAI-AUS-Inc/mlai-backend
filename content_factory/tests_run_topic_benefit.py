"""Actual topic grant/debit constructors and verified benefit transaction proofs."""
from django.db import connections
from copy import deepcopy
import json
import os
from pathlib import Path
from unittest.mock import patch
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier, Event

from django.test import TransactionTestCase, override_settings
from django.db import close_old_connections, connection

from roo.services import PointsService
from .first_article_entitlement import entitled_authorization, grant_topic_batch
from .founder_journey_testing import ALL_P1_FLAGS_ON
from .run_backend_facts import create_job_facts
from .run_ownership import bind_admitted_run, current_execution_ticket, owner_receipt, reserve_admitted_run, user_principal, wire_identity
from .self_heal_testing import make_p2_org
from .tests_run_event_authority import FLAGS


@override_settings(**FLAGS, **ALL_P1_FLAGS_ON, ARTICLES_FREE_FIRST_ARTICLE_ENABLED=True,
                   ARTICLES_RUN_CONTROL_MODEL_USD="5", ARTICLES_RUN_CONTROL_MODEL_TOKENS=100000)
class TopicBenefitTests(TransactionTestCase):
    def admit(self, kind, admission=None, *, workflow="auto_discovery", notification_context=None):
        from integrations.services.article_generation import _ledger_reference_id
        from roo.models import Ledger
        if admission is not None:
            from django.contrib.auth import get_user_model
            from organizations.models import Organization
            from .tests_run_terminal_settlement import ActualPairedTerminalSettlementTests
            receipt = admission["run_request"]["backend_run_admission_v1"]
            for model, value in ((Organization, int(receipt["organization_id"])),
                                 (get_user_model(), int(receipt["principal"]["user_id"]))):
                ActualPairedTerminalSettlementTests.sequence(self, model, value)
        fixture = make_p2_org("topic-" + kind + ".fixture.test")
        self.fixture, self.org, self.payer = fixture, fixture.organization, fixture.owner
        key, physical = "r3-topic-" + kind + "-original", "r3-topic-" + kind + "-worker"
        raw = {"domain": self.org.domain, "client_request_id": key, "requested_topic_count": 3,
               "request_source": "roo_slackbot", "requested_by_slack_user_id": "mlai_user:" + str(self.payer.pk)}
        if workflow == "island_refresh":
            raw["island_research_brief"] = {"subject": "Balcony gardens", "description": "Measured practical garden ideas",
                "audience": "Local gardeners", "focus": "", "intent": "any"}
        if notification_context:
            raw["notification_context"] = notification_context(self)
        if admission is not None:
            ActualPairedTerminalSettlementTests.sequence(self, Ledger,
                admission["original_ledger"]["id"] - (1 if kind == "paid" else 0))
        if kind == "free":
            original = grant_topic_batch(organization=self.org, user=self.payer,
                client_request_id=key, requested_topic_count=3)
            self.assertIsNotNone(original)
            action = admission["run_request"]["roo_points_action"] if admission else "topic_discovery"
            raw.update(entitled_authorization(domain=self.org.domain, action=action, ledger=original))
        else:
            PointsService.credit_purchased_topup(user=self.payer, delta=100,
                description="Synthetic topic fixture funding", idempotency_key=key + ":funding")
            original, _ = PointsService.spend(user=self.payer, delta=3, source="CONTENT_FACTORY",
                description="Synthetic original topic debit", created_by_slack_id="mlai_user:" + str(self.payer.pk),
                idempotency_key="content_factory:topic_generation:charge:" + key,
                reference_type="CONTENT_FACTORY", reference_id=_ledger_reference_id(key))
            raw.update(roo_points_action="content_island_topic_generation", roo_points_authorized=True,
                roo_points_ledger_id=str(original.pk), roo_points_billing_user_id=self.payer.pk,
                roo_points_billing_status="charged", roo_points_cost=3, roo_points_required=3)
        run = bind_admitted_run(reserve_admitted_run(organization=self.org, workflow=workflow,
            principal=user_principal(self.payer.pk, "mlai_user:" + str(self.payer.pk)), payload=raw), physical)
        create_job_facts(job_id=run.run_id, domain=run.domain, slack_user_id=run.slack_user_id,
            status="queued", client_request_id=key, request_meta=deepcopy(run.run_request),
            billing_status="charged" if kind == "paid" else "entitled", billing_ledger_id=original.pk,
            billing_amount=3 if kind == "paid" else 0)
        if admission is not None:
            self.assertEqual(run.run_request, admission["run_request"])
            self.assertEqual(run.run_id, admission["run_id"])
        return run, original

    def payload(self):
        from .run_artifact_observations import _digest
        selection = {"selected_keyword": "Urban balcony gardens", "selection_reason": "Measured local demand",
            "options": [{"keyword": "Urban balcony gardens", "volume": 100, "difficulty": 12},
                        {"keyword": "Container vegetables", "volume": 200, "difficulty": 21},
                        {"keyword": "Small garden design", "volume": 300, "difficulty": 19}]}
        return {"schema_version": 1, "selection": selection, "selection_sha256": _digest(selection)}

    def observation(self, run, payload, *, kind="topic_selection_v1"):
        from django.utils import timezone
        from .run_artifact_observations import _digest
        from .run_ownership import WIRE_REQUEST_KEY, WIRE_EXECUTION_KEY
        fields = {WIRE_REQUEST_KEY: wire_identity(owner_receipt(run)), WIRE_EXECUTION_KEY: current_execution_ticket(run)}
        record = {"schema_version": 1, "run_id": run.run_id, "kind": kind, **deepcopy(fields), "payload": payload,
            "payload_sha256": _digest(payload), "source_sha256": _digest(payload), "observed_at": timezone.now().isoformat()}
        record["evidence_sha256"] = _digest(record)
        return ({"schema_version": 1, "run_id": run.run_id, **fields, "evidence": [record]},
                {"schema_version": 1, "run_id": run.run_id, **fields, kind: deepcopy(payload)})

    def remote(self, value, status=200):
        import requests
        response = requests.Response()
        response.status_code = status
        response._content = json.dumps(value).encode()
        response._content_consumed = True
        return response

    @override_settings(CONTENT_FACTORY_URL="https://topic.fixture.invalid", CONTENT_FACTORY_API_KEY="synthetic-topic-key")
    def test_verified_topics_project_choices_once_without_lifecycle_or_original_clock_changes(self):
        from workflow_runs.models import ContentFactoryRun
        from .run_topic_delivery import project_verified_topics, has_verified_topic_delivery
        from .run_ownership import TOPIC_DELIVERY_RESULT_KEY, public_run_value
        from .models import ContentFactoryJob
        run, original = self.admit("paid")
        before = ContentFactoryRun.objects.values().get(pk=run.pk)
        evidence, canonical = self.observation(run, self.payload())
        with patch("requests.get", side_effect=[self.remote(evidence), self.remote(canonical)]) as get:
            self.assertEqual(project_verified_topics(run.run_id)["verified"], ["topic_selection_v1"])
            self.assertEqual(project_verified_topics(run.run_id)["verified"], ["topic_selection_v1"])
        self.assertEqual(get.call_count, 2)
        job = ContentFactoryJob.objects.get(job_id=run.run_id)
        self.assertEqual(job.selection_data, self.payload()["selection"])
        self.assertEqual(job.status, "awaiting_confirmation")
        run.refresh_from_db()
        self.assertTrue(has_verified_topic_delivery(run))
        for field in ("run_request", "status", "approval_state", "current_step", "created_at", "updated_at",
                      "last_event_emitted_at", "reconciled_at"):
            self.assertEqual(getattr(run, field), before[field], field)
        self.assertNotIn(TOPIC_DELIVERY_RESULT_KEY, public_run_value(run.result))
        self.assertFalse(original.service_refunds.exists())

    @override_settings(CONTENT_FACTORY_URL="https://topic.fixture.invalid", CONTENT_FACTORY_API_KEY="synthetic-topic-key")
    def test_typed_candidate_without_matching_canonical_choices_grants_no_benefit(self):
        from .run_topic_delivery import project_verified_topics, has_verified_topic_delivery
        from .models import ContentFactoryJob
        from .run_artifact_observations import ArtifactObservationInvalid
        run, original = self.admit("paid")
        evidence, canonical = self.observation(run, self.payload())
        canonical["topic_selection_v1"]["selection"]["options"][0]["keyword"] = "Changed bytes"
        with patch("requests.get", side_effect=[self.remote(evidence), self.remote(canonical)]), self.assertRaises(ArtifactObservationInvalid):
            project_verified_topics(run.run_id)
        run.refresh_from_db()
        self.assertFalse(has_verified_topic_delivery(run))
        self.assertEqual(ContentFactoryJob.objects.get(job_id=run.run_id).selection_data, {})
        self.assertFalse(original.service_refunds.exists())

    def terminal(self, run, *, status="failed"):
        from django.utils import timezone
        from .tests_run_event_authority import batch
        from .run_event_authority import receive_authoritative_run_events
        from .run_ownership import WIRE_REQUEST_KEY, WIRE_EXECUTION_KEY
        from .run_terminal_settlement import receipt_digest
        first = batch(run, [{"status": "running"}, {"status": status}])
        self.assertEqual(receive_authoritative_run_events(first).status_code, 200)
        run.refresh_from_db()
        final = batch(run, [{"seq": 3, "state_version": 3, "status": "cancelled"}])
        closed = {"schema_version": 1, "run_id": run.run_id, "status": "closed_no_effects", "kind": "topic_selection_v1",
            WIRE_REQUEST_KEY: wire_identity(owner_receipt(run)), WIRE_EXECUTION_KEY: current_execution_ticket(run),
            "closed_at": timezone.now().isoformat(), "event_cursor": 3}
        closed["receipt_sha256"] = receipt_digest(closed)
        page = {"schema_version": 1, "run_id": run.run_id, "batches": [first, final], "next_cursor": 3, "has_more": False}
        return closed, page

    @override_settings(CONTENT_FACTORY_URL="https://topic.fixture.invalid", CONTENT_FACTORY_API_KEY="synthetic-topic-key")
    def test_paid_topic_failure_refunds_actual_original_three_once_after_full_closed_history(self):
        from workflow_runs.models import ContentFactoryRun
        from .run_terminal_settlement import settle_authoritative_terminal, terminal_execution_closed
        run, original = self.admit("paid")
        closed, page = self.terminal(run)
        before = ContentFactoryRun.objects.values().get(pk=run.pk)
        with patch("requests.post", return_value=self.remote({"terminal_topic_settlement_v1": closed})) as post, \
                patch("requests.get", return_value=self.remote(page)):
            result = settle_authoritative_terminal(run.run_id)
            self.assertEqual(result["state"], "refunded", result)
            self.assertEqual(settle_authoritative_terminal(run.run_id), result)
        self.assertNotIn("expected_cancellation_v1", post.call_args.kwargs["json"])
        self.assertEqual(post.call_count, 1)
        effect = original.service_refunds.get()
        self.assertEqual(effect.delta_microroo, 3_000_000)
        self.assertEqual(effect.user_id, original.user_id)
        run.refresh_from_db()
        self.assertTrue(terminal_execution_closed(run))
        for field in ("run_request", "created_at", "updated_at", "last_event_emitted_at", "reconciled_at"):
            self.assertEqual(getattr(run, field), before[field], field)

    @override_settings(CONTENT_FACTORY_URL="https://topic.fixture.invalid", CONTENT_FACTORY_API_KEY="synthetic-topic-key")
    def test_free_topic_failure_releases_original_grant_once_without_credit_or_new_reservation(self):
        from .run_dispatch_settlement import _free_obligation_state
        from .run_terminal_settlement import settle_authoritative_terminal
        from roo.models import Ledger
        run, original = self.admit("free")
        closed, page = self.terminal(run, status="denied")
        before = list(Ledger.objects.values_list("pk", "kind", "delta_microroo"))
        with patch("requests.post", return_value=self.remote({"terminal_topic_settlement_v1": closed})) as post, \
                patch("requests.get", return_value=self.remote(page)):
            result = settle_authoritative_terminal(run.run_id)
            self.assertEqual(result["state"], "released", result)
            self.assertEqual(settle_authoritative_terminal(run.run_id), result)
        self.assertEqual(post.call_count, 1)
        run.refresh_from_db()
        self.assertEqual(_free_obligation_state(run), "released")
        self.assertFalse(Ledger.objects.filter(kind__in=["SPEND", "REFUND"]).exists())
        self.assertEqual(list(Ledger.objects.filter(pk=original.pk).values_list("pk", "kind", "delta_microroo")), before)

    @override_settings(CONTENT_FACTORY_URL="https://topic.fixture.invalid", CONTENT_FACTORY_API_KEY="synthetic-topic-key")
    def test_article_closure_cannot_be_reused_as_topic_no_benefit_proof(self):
        from .run_terminal_settlement import settle_authoritative_terminal, receipt_digest
        run, original = self.admit("paid")
        closed, page = self.terminal(run)
        closed.pop("kind")
        closed["receipt_sha256"] = receipt_digest(closed)
        with patch("requests.post", return_value=self.remote({"terminal_topic_settlement_v1": closed})), \
                patch("requests.get") as get:
            result = settle_authoritative_terminal(run.run_id)
        self.assertEqual(result["state"], "pending")
        get.assert_not_called()
        self.assertFalse(original.service_refunds.exists())

    def paired(self, kind, outcome):
        value = json.loads((Path(__file__).parent / "testdata/r3" / ("topic_actual_" + kind + "_" + outcome + ".json")).read_text())
        run, original = self.admit(kind, admission=value["original_backend_admission"])
        from .run_event_authority import receive_authoritative_run_events
        for body in value["history_before_cancel"]["batches"]:
            result = receive_authoritative_run_events(body)
            self.assertEqual(result.status_code, 200, result.data)
        run.refresh_from_db()
        return value, run, original

    @override_settings(CONTENT_FACTORY_URL="https://topic.fixture.invalid", CONTENT_FACTORY_API_KEY="synthetic-topic-key")
    def test_actual_paired_paid_topic_failure_refunds_original_once_with_unchanged_worker_echo(self):
        from .run_terminal_settlement import settle_authoritative_terminal
        value, run, original = self.paired("paid", "settlement")
        with patch("requests.post", return_value=self.remote(value["cancel_response"])) as post, \
                patch("requests.get", return_value=self.remote(value["history"])):
            result = settle_authoritative_terminal(run.run_id)
            self.assertEqual(result["state"], "refunded", result)
            self.assertEqual(settle_authoritative_terminal(run.run_id), result)
        self.assertEqual(post.call_args.kwargs["json"], value["cancel_request"])
        self.assertEqual(post.call_count, 1)
        self.assertEqual(original.service_refunds.get().delta_microroo, 3_000_000)
        self.assertEqual(value["cancel_replay"]["terminal_topic_settlement_v1"], value["cancel_response"]["terminal_topic_settlement_v1"])

    @override_settings(CONTENT_FACTORY_URL="https://topic.fixture.invalid", CONTENT_FACTORY_API_KEY="synthetic-topic-key")
    def test_actual_paired_free_topic_failure_releases_only_its_real_original_grant(self):
        from .run_terminal_settlement import settle_authoritative_terminal
        from .run_dispatch_settlement import _free_obligation_state
        from roo.models import Ledger
        value, run, original = self.paired("free", "settlement")
        with patch("requests.post", return_value=self.remote(value["cancel_response"])) as post, \
                patch("requests.get", return_value=self.remote(value["history"])):
            result = settle_authoritative_terminal(run.run_id)
            self.assertEqual(result["state"], "released", result)
            self.assertEqual(settle_authoritative_terminal(run.run_id), result)
        self.assertEqual(post.call_args.kwargs["json"], value["cancel_request"])
        self.assertEqual(post.call_count, 1)
        run.refresh_from_db()
        self.assertEqual(_free_obligation_state(run), "released")
        self.assertFalse(Ledger.objects.filter(kind__in=["SPEND", "REFUND"]).exists())

    def paired_delivery(self, kind):
        from .run_topic_delivery import project_verified_topics, has_verified_topic_delivery
        from .run_terminal_settlement import settle_authoritative_terminal
        from .run_event_authority import receive_authoritative_run_events
        from .models import ContentFactoryJob
        value, run, original = self.paired(kind, "delivery")
        with patch("requests.get", side_effect=[self.remote(value["delivery_evidence"]),
                self.remote(value["canonical_topic_response"])]) as get:
            self.assertEqual(project_verified_topics(run.run_id)["verified"], ["topic_selection_v1"])
            self.assertEqual(project_verified_topics(run.run_id)["verified"], ["topic_selection_v1"])
        self.assertEqual(get.call_count, 2)
        run.refresh_from_db()
        self.assertTrue(has_verified_topic_delivery(run))
        self.assertEqual(ContentFactoryJob.objects.get(job_id=run.run_id).selection_data,
                         value["canonical_topic_response"]["topic_selection_v1"]["selection"])
        for body in value["history"]["batches"]:
            self.assertEqual(receive_authoritative_run_events(body).status_code, 200)
        with patch("requests.post") as close:
            self.assertEqual(settle_authoritative_terminal(run.run_id)["state"], "kept")
        close.assert_not_called()
        self.assertFalse(original.service_refunds.exists())
        if kind == "free":
            from .run_dispatch_settlement import _free_obligation_state
            self.assertEqual(_free_obligation_state(run), "reserved")

    @override_settings(CONTENT_FACTORY_URL="https://topic.fixture.invalid", CONTENT_FACTORY_API_KEY="synthetic-topic-key")
    def test_actual_paired_paid_choices_stay_earned_after_cancel_and_replay(self):
        self.paired_delivery("paid")

    @override_settings(CONTENT_FACTORY_URL="https://topic.fixture.invalid", CONTENT_FACTORY_API_KEY="synthetic-topic-key")
    def test_actual_paired_free_choices_never_release_the_used_original_batch(self):
        self.paired_delivery("free")

    @override_settings(CONTENT_FACTORY_URL="https://topic.fixture.invalid", CONTENT_FACTORY_API_KEY="synthetic-topic-key",
                       MY_STARTUP_TRANSITION_NOTIFICATIONS_ENABLED=True, COMMUNITY_CHAT_FRONTEND_URL="https://chat.fixture.invalid")
    def test_verified_manual_research_queues_one_existing_outbox_notice_and_rechecks_current_recipient(self):
        from .run_topic_delivery import project_verified_topics
        from .founder_transitions import notify_verified_research_ready, _current
        from .models import ContentFactoryCallbackEvent, OrganizationContentConfig
        from django.utils import timezone
        run, original = self.admit("paid")
        evidence, canonical = self.observation(run, self.payload())
        with patch("requests.get", side_effect=[self.remote(evidence), self.remote(canonical)]), patch("requests.post") as post:
            project_verified_topics(run.run_id)
            project_verified_topics(run.run_id)
        post.assert_not_called()
        run.refresh_from_db()
        from .founder_transitions import owns_notifications
        self.assertTrue(owns_notifications(run.organization), run.organization.content_config.pillar_strategy)
        self.assertEqual(notify_verified_research_ready(run, evidence_id="f" * 64), 0)
        config = OrganizationContentConfig.objects.get(organization=self.org)
        notices = config.pillar_strategy["founder_notices"]["outbox"]
        self.assertEqual(len(notices), 2)
        self.assertEqual(ContentFactoryCallbackEvent.objects.filter(event_type="founder_transition").count(), 2)
        self.assertTrue(all("/my-startup/articles?company_id=" in notice["link"] for notice in notices.values()))
        self.assertTrue(all(notice["user_id"] == self.payer.pk for notice in notices.values()))
        for notice in notices.values():
            self.assertEqual(_current(config, notice, now=timezone.now()).pk, self.payer.pk)
        self.payer.is_active = False
        self.payer.save(update_fields=["is_active"])
        for notice in notices.values():
            self.assertIsNone(_current(config, notice, now=timezone.now()))

    @override_settings(CONTENT_FACTORY_URL="https://topic.fixture.invalid", CONTENT_FACTORY_API_KEY="synthetic-topic-key")
    def test_island_proposals_remain_actual_measured_islands_and_do_not_become_topic_options(self):
        from .run_artifact_observations import _digest
        from .run_topic_delivery import project_verified_topics
        from .island_research import proposal_for_adoption
        from .run_event_authority import receive_authoritative_run_events
        from .tests_run_event_authority import batch
        run, original = self.admit("paid", workflow="island_refresh")
        self.assertEqual(receive_authoritative_run_events(batch(run, [{"status": "completed"}])).status_code, 200)
        run.refresh_from_db()
        proposals = [{"id": "measured-balcony-gardens", "pillar_keyword": "Balcony gardens", "name": "Balcony gardens",
            "centroid_embedding": [0.1, 0.2], "keywords": [{"keyword": "Balcony gardens", "volume": 100, "difficulty": 12}]}]
        payload = {"schema_version": 1, "suggested_islands": proposals, "proposals_sha256": _digest(proposals)}
        evidence, canonical = self.observation(run, payload, kind="island_proposals_v1")
        with patch("requests.get", side_effect=[self.remote(evidence), self.remote(canonical)]):
            self.assertEqual(project_verified_topics(run.run_id)["verified"], ["island_proposals_v1"])
        run.refresh_from_db()
        self.assertEqual(proposal_for_adoption(run, proposals[0]["id"]), proposals[0])
        self.assertNotIn("options", run.selection_data)
        self.assertEqual(run.result["suggested_islands"], proposals)

    @override_settings(CONTENT_FACTORY_URL="https://topic.fixture.invalid", CONTENT_FACTORY_API_KEY="synthetic-topic-key")
    def test_verified_choices_reach_real_quote_confirm_and_original_retry_without_reusing_topic_payment(self):
        from integrations.services.article_generation import quote_content_factory_article, confirm_topic
        from .run_topic_delivery import project_verified_topics
        from .models import ContentFactoryJob
        from roo.models import Ledger
        run, original = self.admit("paid")
        evidence, canonical = self.observation(run, self.payload())
        with patch("requests.get", side_effect=[self.remote(evidence), self.remote(canonical)]):
            project_verified_topics(run.run_id)
        job = ContentFactoryJob.objects.get(job_id=run.run_id)
        keyword = job.selection_data["options"][0]["keyword"]
        actor = "mlai_user:" + str(self.payer.pk)
        quoted = quote_content_factory_article(actor_id=actor, domain=run.domain, source_run_id=run.run_id)
        before = deepcopy(run.run_request)
        with patch("integrations.services.article_generation.http_requests.post",
                return_value=self.remote({"run_id": "typed-topic-confirmed-worker", "status": "queued"}, status=202)) as post:
            first = confirm_topic(domain=run.domain, confirmed_keyword=keyword, slack_user_id=actor, billing_user=self.payer,
                source_run_id=run.run_id, client_request_id="typed-topic-new-article", expected_cost_points=quoted,
                delivery_mode="content_only", delivery_mode_confirmed=True)
            replay = confirm_topic(domain=run.domain, confirmed_keyword=keyword, slack_user_id=actor, billing_user=self.payer,
                source_run_id=run.run_id, client_request_id="typed-topic-new-article", delivery_mode="content_only", delivery_mode_confirmed=True)
        self.assertEqual(first["job_id"], replay["job_id"])
        self.assertEqual(post.call_count, 1)
        child = ContentFactoryJob.objects.get(job_id="typed-topic-confirmed-worker")
        self.assertNotEqual(child.billing_ledger_id, original.pk)
        self.assertEqual(child.billing_amount, quoted)
        self.assertEqual(Ledger.objects.filter(kind="SPEND").count(), 2)
        run.refresh_from_db()
        self.assertEqual(run.run_request, before)

    @override_settings(CONTENT_FACTORY_URL="https://topic.fixture.invalid", CONTENT_FACTORY_API_KEY="synthetic-topic-key",
                       ROO_API_KEY="synthetic-topic-confirm")
    def test_actual_authenticated_confirm_http_retains_mode_and_one_article_charge_with_replay(self):
        from rest_framework.test import APIRequestFactory
        from workflow_runs.models import ContentFactoryRun
        from roo.models import Ledger
        from .content_views import ContentConfirmView, ContentJobConfirmView
        from .models import ContentFactoryJob
        from .run_topic_delivery import project_verified_topics
        from .run_article_charge import article_charge_receipt
        run, original = self.admit("paid")
        evidence, canonical = self.observation(run, self.payload())
        with patch("requests.get", side_effect=[self.remote(evidence), self.remote(canonical)]):
            project_verified_topics(run.run_id)
        before = ContentFactoryRun.objects.values().get(pk=run.pk)
        body = {"domain": run.domain, "confirmed_keyword": self.payload()["selection"]["options"][0]["keyword"],
            "slack_user_id": "mlai_user:" + str(self.payer.pk), "source_run_id": run.run_id,
            "client_request_id": "actual-http-confirmed-article", "request_source": "roo_slackbot",
            "expected_cost_points": 12, "delivery_mode": "content_only", "delivery_mode_confirmed": True}
        request = lambda value: APIRequestFactory().post("/api/v1/content/confirm", value, format="json", HTTP_X_API_KEY="synthetic-topic-confirm")
        for mode in (None, "unsupported"):
            invalid = {**body, "delivery_mode": mode}
            with patch("integrations.services.article_generation.http_requests.post") as post:
                rejected = ContentConfirmView.as_view()(request(invalid))
            self.assertNotEqual(rejected.status_code, 202, rejected.data)
            post.assert_not_called()
            self.assertEqual(Ledger.objects.filter(kind="SPEND").count(), 1)
        with patch("integrations.services.article_generation.http_requests.post",
                return_value=self.remote({"run_id": "actual-http-confirmed-worker", "status": "queued"}, 202)) as post:
            first = ContentConfirmView.as_view()(request(body))
            replay = ContentConfirmView.as_view()(request(body))
            job_replay = ContentJobConfirmView.as_view()(request({**body, "option_index": 0}), job_id=run.run_id)
        self.assertEqual(first.status_code, 202, first.data)
        self.assertEqual(replay.status_code, 202, replay.data)
        self.assertEqual(job_replay.status_code, 200, job_replay.data)
        self.assertEqual(first.data["job_id"], replay.data["job_id"])
        self.assertEqual(post.call_count, 1)
        self.assertEqual(post.call_args.kwargs["json"]["delivery_mode"], "content_only")
        self.assertIs(post.call_args.kwargs["json"]["delivery_mode_confirmed"], True)
        self.assertEqual(Ledger.objects.filter(kind="SPEND").count(), 2)
        source_job = ContentFactoryJob.objects.get(job_id=run.run_id)
        child_job = ContentFactoryJob.objects.get(job_id=first.data["job_id"])
        self.assertEqual(source_job.client_request_id, owner_receipt(run)["identity"]["dispatch_key"])
        self.assertEqual(child_job.client_request_id, body["client_request_id"])
        self.assertNotEqual(child_job.client_request_id, source_job.client_request_id)
        run.refresh_from_db()
        self.assertEqual(article_charge_receipt(run)["request"]["delivery_mode"], "content_only")
        for field in ("run_request", "status", "approval_state", "current_step", "created_at", "updated_at",
                "last_event_emitted_at", "reconciled_at"):
            self.assertEqual(getattr(run, field), before[field], field)

    @override_settings(CONTENT_FACTORY_URL="https://topic.fixture.invalid", CONTENT_FACTORY_API_KEY="synthetic-topic-key")
    def test_missing_or_unconfirmed_portable_consent_never_debits_an_article_or_dispatches(self):
        from integrations.services.article_generation import confirm_topic, ArticleGenerationError
        from roo.models import Ledger
        run, original = self.admit("paid")
        before = list(Ledger.objects.values())
        for mode, confirmed in ((None, None), ("content_only", False), ("unknown", True)):
            with self.subTest(mode=mode), patch("integrations.services.article_generation.http_requests.post") as post:
                with self.assertRaises(ArticleGenerationError):
                    confirm_topic(domain=run.domain, confirmed_keyword="Urban balcony gardens", slack_user_id="mlai_user:" + str(self.payer.pk),
                        billing_user=self.payer, source_run_id=run.run_id, client_request_id="forbidden-new-article",
                        expected_cost_points=12, delivery_mode=mode, delivery_mode_confirmed=confirmed)
            post.assert_not_called()
        self.assertEqual(list(Ledger.objects.values()), before)

    @override_settings(CONTENT_FACTORY_URL="https://topic.fixture.invalid", CONTENT_FACTORY_API_KEY="synthetic-topic-key")
    def test_revoked_or_inactive_principal_prevents_new_verification_before_any_worker_read(self):
        from .run_topic_delivery import project_verified_topics
        from .run_artifact_observations import ArtifactObservationInvalid
        run, original = self.admit("paid")
        self.payer.is_active = False
        self.payer.save(update_fields=["is_active"])
        with patch("requests.get") as get, self.assertRaises(ValueError):
            project_verified_topics(run.run_id)
        get.assert_not_called()
        self.assertFalse(original.service_refunds.exists())

    @override_settings(CONTENT_FACTORY_URL="https://topic.fixture.invalid", CONTENT_FACTORY_API_KEY="synthetic-topic-key")
    def test_stop_intent_arriving_during_canonical_read_prevents_new_choice_projection(self):
        from .run_topic_delivery import project_verified_topics
        from .run_user_stop import record_user_denial
        from .models import ContentFactoryJob
        run, original = self.admit("paid")
        evidence, canonical = self.observation(run, self.payload())
        def get(url, **kwargs):
            if url.endswith("/evidence"):
                return self.remote(evidence)
            record_user_denial(run, user=self.payer, actor_id="mlai_user:" + str(self.payer.pk))
            return self.remote(canonical)
        with patch("requests.get", side_effect=get), self.assertRaises(ValueError):
            project_verified_topics(run.run_id)
        self.assertEqual(ContentFactoryJob.objects.get(job_id=run.run_id).selection_data, {})
        self.assertFalse(original.service_refunds.exists())

    @override_settings(CONTENT_FACTORY_URL="https://topic.fixture.invalid", CONTENT_FACTORY_API_KEY="synthetic-topic-key")
    def test_verified_choices_are_recovered_by_real_history_pull_without_any_callback(self):
        from .run_reconciliation import reconcile_authoritative_run
        from .models import ContentFactoryJob
        from .run_ownership import RECONCILIATION_RESULT_KEY
        value = json.loads((Path(__file__).parent / "testdata/r3/topic_actual_paid_delivery.json").read_text())
        run, original = self.admit("paid", admission=value["original_backend_admission"])
        def get(url, **kwargs):
            if url.endswith("/events"):
                return self.remote(value["history_before_cancel"])
            return self.remote(value["delivery_evidence"] if url.endswith("/evidence") else value["canonical_topic_response"])
        with patch("requests.get", side_effect=get) as get:
            self.assertEqual(reconcile_authoritative_run(run.run_id, refresh=True)["outcome"], "history_received")
        self.assertEqual(get.call_count, 3)
        self.assertEqual(ContentFactoryJob.objects.get(job_id=run.run_id).selection_data,
            value["canonical_topic_response"]["topic_selection_v1"]["selection"])
        run.refresh_from_db()
        self.assertEqual(run.status, "awaiting_confirmation")
        self.assertEqual(run.result[RECONCILIATION_RESULT_KEY]["cursor"], value["history_before_cancel"]["next_cursor"])

    @override_settings(CONTENT_FACTORY_URL="https://topic.fixture.invalid", CONTENT_FACTORY_API_KEY="synthetic-topic-key")
    def test_free_topic_benefit_does_not_keep_separately_paid_undelivered_article_charge(self):
        from integrations.services.article_generation import confirm_topic, quote_content_factory_article
        from .run_topic_delivery import project_verified_topics, has_verified_topic_delivery
        from .run_dispatch_settlement import _free_obligation_state
        from .run_terminal_settlement import settle_authoritative_terminal
        from .run_article_charge import article_charge_receipt
        from roo.models import Ledger
        from workflow_runs.models import ContentFactoryRun
        run, grant = self.admit("free")
        PointsService.credit_purchased_topup(user=self.payer, delta=100,
            description="Synthetic separate article funding", idempotency_key="separate-article-funding")
        evidence, canonical = self.observation(run, self.payload())
        with patch("requests.get", side_effect=[self.remote(evidence), self.remote(canonical)]):
            project_verified_topics(run.run_id)
        actor = "mlai_user:" + str(self.payer.pk)
        cost = quote_content_factory_article(actor_id=actor, domain=run.domain, source_run_id=run.run_id)
        with patch("integrations.services.article_generation.http_requests.post",
                return_value=self.remote({"run_id": "paid-child-from-free-topics", "status": "queued"}, status=202)):
            confirm_topic(domain=run.domain, confirmed_keyword=self.payload()["selection"]["options"][0]["keyword"],
                slack_user_id=actor, billing_user=self.payer, source_run_id=run.run_id,
                client_request_id="separately-paid-article", expected_cost_points=cost,
                delivery_mode="content_only", delivery_mode_confirmed=True)
        child = ContentFactoryRun.objects.get(run_id="paid-child-from-free-topics")
        source_request = deepcopy(run.run_request)
        closed, page = self.terminal(child)
        closed.pop("kind")
        from .run_terminal_settlement import receipt_digest
        closed["receipt_sha256"] = receipt_digest(closed)
        with patch("requests.post", return_value=self.remote({"terminal_settlement_v1": closed})), \
                patch("requests.get", return_value=self.remote(page)):
            result = settle_authoritative_terminal(child.run_id)
        self.assertEqual(result["state"], "refunded", result)
        run.refresh_from_db()
        article_ledger = Ledger.objects.get(pk=article_charge_receipt(run)["ledger_id"])
        self.assertEqual(article_ledger.service_refunds.get().delta_microroo, cost * 1_000_000)
        self.assertTrue(has_verified_topic_delivery(run))
        self.assertEqual(_free_obligation_state(run), "reserved")
        self.assertEqual(run.run_request, source_request)
        self.assertEqual(Ledger.objects.filter(pk=grant.pk).get().kind, "ADJUST")

    @override_settings(CONTENT_FACTORY_URL="https://topic.fixture.invalid", CONTENT_FACTORY_API_KEY="synthetic-topic-key",
                       MY_STARTUP_TRANSITION_NOTIFICATIONS_ENABLED=True, COMMUNITY_CHAT_FRONTEND_URL="https://chat.fixture.invalid")
    def test_concurrent_positive_receipts_preserve_one_proof_and_one_outbox_per_recipient(self):
        from .run_topic_delivery import project_verified_topics
        from .run_ownership import TOPIC_DELIVERY_RESULT_KEY
        from .models import ContentFactoryCallbackEvent
        run, original = self.admit("paid")
        evidence, canonical = self.observation(run, self.payload())
        barrier = Barrier(2)
        def get(url, **kwargs):
            self.assertFalse(connection.in_atomic_block)
            if url.endswith("/evidence"):
                barrier.wait(timeout=5)
                return self.remote(evidence)
            return self.remote(canonical)
        def invoke():
            close_old_connections()
            try:
                return project_verified_topics(run.run_id)
            finally:
                connections.close_all()
        with patch("requests.get", side_effect=get), ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda _: invoke(), range(2)))
        self.assertTrue(all(row["verified"] == ["topic_selection_v1"] for row in results))
        run.refresh_from_db()
        self.assertEqual(len(run.result[TOPIC_DELIVERY_RESULT_KEY]["proofs"]), 1)
        self.assertEqual(ContentFactoryCallbackEvent.objects.filter(event_type="founder_transition").count(), 2)

    @override_settings(CONTENT_FACTORY_URL="https://topic.fixture.invalid", CONTENT_FACTORY_API_KEY="synthetic-topic-key")
    def test_two_topic_terminal_consumers_admit_one_cancel_and_one_original_refund(self):
        from .run_terminal_settlement import settle_authoritative_terminal
        run, original = self.admit("paid")
        closed, page = self.terminal(run)
        entered, release = Event(), Event()
        def post(*args, **kwargs):
            self.assertFalse(connection.in_atomic_block)
            entered.set()
            self.assertTrue(release.wait(5))
            return self.remote({"terminal_topic_settlement_v1": closed})
        def invoke():
            close_old_connections()
            try:
                return settle_authoritative_terminal(run.run_id)
            finally:
                connections.close_all()
        with patch("requests.post", side_effect=post) as cancel, patch("requests.get", return_value=self.remote(page)), \
                ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(invoke)
            self.assertTrue(entered.wait(5))
            try:
                self.assertEqual(pool.submit(invoke).result(timeout=5)["state"], "pending")
            finally:
                release.set()
            self.assertEqual(first.result(timeout=10)["state"], "refunded")
        self.assertEqual(cancel.call_count, 1)
        self.assertEqual(original.service_refunds.count(), 1)

    def automation_context(self, *, foreign=False):
        from django.utils import timezone
        from .models import NotificationChannel, ResearchAutomation, AutomationRun
        from integrations.services.notification_adapters import notification_context_for_run
        fixture = make_p2_org("foreign-notice.fixture.test") if foreign else self.fixture
        channel = NotificationChannel.objects.create(organization=fixture.organization, user=fixture.owner,
            channel_type="slack", route_id="U_SYNTHETIC_TOPIC", consent_state="active", verified_at=timezone.now())
        automation = ResearchAutomation.objects.create(organization=fixture.organization, user=fixture.owner,
            notification_channel=channel, status="active")
        self.automation_run = AutomationRun.objects.create(automation=automation, scheduled_for_at=timezone.now(),
            local_date=timezone.now().date(), idempotency_key="synthetic-notice-run", status="queued")
        return notification_context_for_run(self.automation_run)

    @override_settings(CONTENT_FACTORY_URL="https://topic.fixture.invalid", CONTENT_FACTORY_API_KEY="synthetic-topic-key")
    def test_automation_replay_sends_one_existing_channel_delivery_with_canonical_options(self):
        from .run_topic_delivery import project_verified_topics
        from .models import NotificationDelivery
        run, original = self.admit("paid", notification_context=lambda test: test.automation_context())
        evidence, canonical = self.observation(run, self.payload())
        with patch("requests.get", side_effect=[self.remote(evidence), self.remote(canonical)]), \
                patch("integrations.services.notification_adapters._send_slack", return_value=(True, "synthetic-notice-id", {})) as send:
            project_verified_topics(run.run_id)
            project_verified_topics(run.run_id)
        self.assertEqual(send.call_count, 1)
        self.assertEqual(NotificationDelivery.objects.count(), 1)
        self.assertEqual(NotificationDelivery.objects.get().status, "sent")
        self.automation_run.refresh_from_db()
        self.assertEqual(self.automation_run.callback_payload["selection"], self.payload()["selection"])
        self.assertEqual(self.automation_run.content_factory_run_id, run.run_id)
        self.assertFalse(original.service_refunds.exists())

    @override_settings(CONTENT_FACTORY_URL="https://topic.fixture.invalid", CONTENT_FACTORY_API_KEY="synthetic-topic-key")
    def test_original_context_for_another_organization_never_receives_topic_notification(self):
        from .run_topic_delivery import project_verified_topics
        from .models import NotificationDelivery
        run, original = self.admit("paid", notification_context=lambda test: test.automation_context(foreign=True))
        evidence, canonical = self.observation(run, self.payload())
        with patch("requests.get", side_effect=[self.remote(evidence), self.remote(canonical)]), \
                patch("integrations.services.notification_adapters._send_slack") as send:
            project_verified_topics(run.run_id)
            project_verified_topics(run.run_id)
        send.assert_not_called()
        self.assertFalse(NotificationDelivery.objects.exists())

    @override_settings(CONTENT_FACTORY_URL="https://topic.fixture.invalid", CONTENT_FACTORY_API_KEY="synthetic-topic-key")
    def test_stop_after_actual_portable_activation_is_rechecked_before_article_debit_or_dispatch(self):
        from integrations.services import article_generation as billing
        from .run_user_stop import record_user_denial
        from roo.models import Ledger
        run, original = self.admit("paid")
        actor = "mlai_user:" + str(self.payer.pk)
        actual, calls = billing.require_article_activation, []
        def activation(**kwargs):
            result = actual(**kwargs)
            calls.append(kwargs)
            if len(calls) == 2:
                record_user_denial(run, user=self.payer, actor_id=actor)
            return result
        before = list(Ledger.objects.values())
        with patch.object(billing, "require_article_activation", side_effect=activation), \
                patch.object(billing.http_requests, "post") as post, self.assertRaises(billing.ArticleGenerationError):
            billing.confirm_topic(domain=run.domain, confirmed_keyword="Urban balcony gardens", slack_user_id=actor,
                billing_user=self.payer, source_run_id=run.run_id, client_request_id="revoked-new-article", expected_cost_points=12,
                delivery_mode="content_only", delivery_mode_confirmed=True)
        self.assertEqual(len(calls), 2)
        self.assertTrue(all(call["article_request"].get("delivery_mode") == "content_only" for call in calls))
        post.assert_not_called()
        self.assertEqual(list(Ledger.objects.values()), before)

    @override_settings(CONTENT_FACTORY_URL="https://topic.fixture.invalid", CONTENT_FACTORY_API_KEY="synthetic-topic-key",
                       ROO_API_KEY="synthetic-topic-callback")
    def test_authenticated_callback_uses_canonical_choices_and_refuses_foreign_ticket_before_worker_reads(self):
        from rest_framework.test import APIRequestFactory
        from .service_views import ContentFactoryCallbackView
        from .run_ownership import WIRE_REQUEST_KEY, WIRE_EXECUTION_KEY
        from .models import ContentFactoryJob
        run, original = self.admit("paid")
        before = deepcopy(run.run_request), run.status, run.updated_at
        evidence, canonical = self.observation(run, self.payload())
        body = {"event": "topic_selection", "job_id": run.run_id, "domain": run.domain,
            "generation": 0, "state_version": 1, "status": "completed", "refundable": True,
            "selection": {"options": [{"keyword": "Unverified worker callback"}]},
            WIRE_REQUEST_KEY: wire_identity(owner_receipt(run)), WIRE_EXECUTION_KEY: current_execution_ticket(run)}
        request = lambda value: APIRequestFactory().post("/callback", value, format="json", HTTP_X_API_KEY="synthetic-topic-callback")
        with patch("requests.get", side_effect=[self.remote(evidence), self.remote(canonical)]), \
                patch.object(ContentFactoryCallbackView, "_dispatch_ordered_callback") as legacy:
            response = ContentFactoryCallbackView.as_view()(request(body))
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data["verified"], ["topic_selection_v1"])
        legacy.assert_not_called()
        self.assertEqual(ContentFactoryJob.objects.get(job_id=run.run_id).selection_data, self.payload()["selection"])
        run.refresh_from_db()
        self.assertEqual((run.run_request, run.status, run.updated_at), before)
        body[WIRE_EXECUTION_KEY]["execution_id"] = "foreign-ticket"
        with patch("requests.get") as get:
            rejected = ContentFactoryCallbackView.as_view()(request(body))
        self.assertEqual(rejected.status_code, 409, rejected.data)
        get.assert_not_called()
        self.assertFalse(original.service_refunds.exists())

    @override_settings(CONTENT_FACTORY_URL="https://topic.fixture.invalid", CONTENT_FACTORY_API_KEY="synthetic-topic-key")
    def test_canonical_read_cannot_project_on_ticket_changed_during_http_and_earned_history_survives_retry(self):
        from django.db import transaction
        from workflow_runs.models import ContentFactoryRun
        from .run_artifact_observations import _digest
        from .run_execution_control import append_execution_ticket
        from .run_topic_delivery import project_verified_topics, has_verified_topic_delivery
        from .run_terminal_settlement import settle_authoritative_terminal
        from .models import ContentFactoryJob
        run, original = self.admit("paid")
        evidence, canonical = self.observation(run, self.payload())
        def advance():
            with transaction.atomic():
                current = ContentFactoryRun.objects.select_for_update().get(pk=run.pk)
                append_execution_ticket(current, control_key=_digest("topic-current-retry"), reason="user_resume")
        def get(url, **kwargs):
            if url.endswith("/evidence"):
                return self.remote(evidence)
            advance()
            return self.remote(canonical)
        with patch("requests.get", side_effect=get), self.assertRaises(ValueError):
            project_verified_topics(run.run_id)
        self.assertEqual(ContentFactoryJob.objects.get(job_id=run.run_id).selection_data, {})
        run.refresh_from_db()
        self.assertEqual(current_execution_ticket(run)["generation"], 1)
        current_evidence, current_canonical = self.observation(run, self.payload())
        with patch("requests.get", side_effect=[self.remote(current_evidence), self.remote(current_canonical)]):
            project_verified_topics(run.run_id)
        with transaction.atomic():
            current = ContentFactoryRun.objects.select_for_update().get(pk=run.pk)
            append_execution_ticket(current, control_key=_digest("topic-later-retry"), reason="user_resume")
        run.refresh_from_db()
        self.terminal(run)
        run.refresh_from_db()
        self.assertTrue(has_verified_topic_delivery(run))
        with patch("requests.post") as close:
            self.assertEqual(settle_authoritative_terminal(run.run_id)["state"], "kept")
        close.assert_not_called()
        self.assertFalse(original.service_refunds.exists())

    @override_settings(CONTENT_FACTORY_URL="https://topic.fixture.invalid", CONTENT_FACTORY_API_KEY="synthetic-topic-key")
    def test_present_null_history_boundary_remains_byte_identical_and_never_closes_or_refunds(self):
        from workflow_runs.models import ContentFactoryRun
        from .run_ownership import RECONCILIATION_RESULT_KEY, TERMINAL_TRANSPORT_RESULT_KEY
        from .run_terminal_settlement import settle_authoritative_terminal
        run, original = self.admit("paid")
        self.terminal(run)
        run.refresh_from_db()
        ContentFactoryRun.objects.filter(pk=run.pk).update(result={**run.result, RECONCILIATION_RESULT_KEY: None})
        before = ContentFactoryRun.objects.values().get(pk=run.pk)
        with patch("requests.post") as close, patch("requests.get") as get:
            self.assertEqual(settle_authoritative_terminal(run.run_id)["state"], "pending")
        close.assert_not_called(); get.assert_not_called()
        after = ContentFactoryRun.objects.values().get(pk=run.pk)
        transport = after["result"].pop(TERMINAL_TRANSPORT_RESULT_KEY)
        self.assertEqual(transport["last_outcome"], "terminal_reconciliation_metadata_invalid")
        self.assertEqual(after, before)
        self.assertFalse(original.service_refunds.exists())

    def test_original_paid_and_free_admissions_are_exact_constructor_receipts(self):
        for kind in ("free", "paid"):
            with self.subTest(kind=kind):
                run, ledger = self.admit(kind)
                self.assertEqual(run.run_request["backend_run_admission_v1"], wire_identity(owner_receipt(run)))
                self.assertEqual(run.run_request["backend_run_execution_v1"], current_execution_ticket(run))
                if os.environ.get("R3_EXPORT_TOPIC_ADMISSIONS") == "1":
                    root = Path("/Users/samdonegan/Documents/Code/founder-journey-worktrees/recovery")
                    value = {"schema_version": 1, "run_id": run.run_id, "run_request": run.run_request,
                        "original_ledger": {"id": ledger.pk, "kind": ledger.kind, "user_id": ledger.user_id,
                            "source": ledger.source, "idempotency_key": ledger.idempotency_key,
                            "delta_microroo": ledger.delta_microroo}}
                    (root / ("R3_BE_ACTUAL_" + kind.upper() + "_TOPIC_FOR_CF.json")).write_text(json.dumps(value, indent=2) + "\n")
