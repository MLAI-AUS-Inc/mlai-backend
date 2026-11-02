"""Synthetic cross-tenant preflight and actual producer/consumer receipt regressions."""

import copy
from datetime import date
from itertools import combinations
from types import SimpleNamespace
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import SimpleTestCase, TestCase, TransactionTestCase, override_settings
from rest_framework.exceptions import ValidationError

from integrations.models import ExternalServiceConnection
from organizations.models import Organization
from startup_updates.evidence_contract import content_hash
from startup_updates.models import MonthlyEvidenceSnapshot, MonthlyUpdateDraft
from startup_updates.preflight import (
    PROVIDERS,
    SourcesNotReady,
    check_sources,
    require_source_preflight,
    selected_sources,
)
from startup_updates.revisions import save_revision, worker_revision_payload


class SelectionMatrixTests(SimpleTestCase):
    def test_every_nonempty_source_subset_checks_exactly_the_selected_sources(self):
        providers = sorted(PROVIDERS)
        for size in range(1, len(providers) + 1):
            for subset in combinations(providers, size):
                with self.subTest(sources=subset):
                    normalized = selected_sources(list(subset))
                    results = check_sources(normalized, lambda p: {"checked": p})
                    self.assertEqual([x["checked"] for x in results], list(subset))
                    self.assertTrue(all(x["status"] == "ready" for x in results))

    def test_all_failures_are_reported_without_stopping_at_first(self):
        def probe(provider):
            if provider == "gmail":
                raise PermissionError()
            if provider == "slack":
                raise TimeoutError()
            return {}

        self.assertEqual(
            [x["status"] for x in check_sources(["gmail", "notion", "slack"], probe)],
            ["needs_reconnect", "ready", "temporarily_unavailable"],
        )

    def test_empty_unknown_and_malformed_sources_never_fall_back_to_gmail(self):
        for value in ([], None, ["unknown"], [None], "gmail"):
            with self.subTest(value=value), self.assertRaises(ValidationError):
                selected_sources(value)
        self.assertEqual(selected_sources([], manual=True), ["manual_documents"])


class ReceiptFlowTests(TestCase):
    def setUp(self):
        self.org = Organization.objects.create(
            name="Synthetic", domain="pulse.example.invalid"
        )
        self.user = get_user_model().objects.create_user(
            email="founder@example.invalid", password="synthetic"
        )
        self.other = get_user_model().objects.create_user(
            email="other@example.invalid", password="synthetic"
        )
        self.month = date(2026, 8, 1)

    def receipt(self, audience="private"):
        evidence = {
            "period": {"month": "2026-08-01"},
            "metrics": [],
            "events": [],
            "organization_id": self.org.pk,
        }
        snapshot, _ = MonthlyEvidenceSnapshot.objects.get_or_create(
            organization=self.org,
            content_hash=content_hash(evidence),
            defaults={"month": self.month, "payload": evidence},
        )
        draft = MonthlyUpdateDraft.objects.create(
            organization=self.org, month=self.month
        )
        revision = save_revision(
            draft,
            {
                "summary": "The synthetic release shipped.",
                "_financial_history_selection": {
                    "enabled": False,
                    "shareAmounts": False,
                },
            },
            snapshot=snapshot,
            audience=audience,
            expected_revision=None,
        )
        return (
            draft,
            revision,
            {
                "snapshot_id": snapshot.pk,
                "expected_revision": None,
                "payload": copy.deepcopy(evidence),
            },
        )

    def test_real_backend_receipt_passes_real_worker_for_both_versions_and_all_audiences(
        self,
    ):
        from valley.workflows.runner import WorkflowRunner

        runner = object.__new__(WorkflowRunner)
        for audience in ("private", "community", "public"):
            draft, revision, pin = self.receipt(audience)
            for version in (1, 2):
                receipt = {
                    **worker_revision_payload(revision, version=version),
                    "month": str(self.month),
                    "structured_memo": revision.structured_memo,
                }
                original = copy.deepcopy(receipt)
                got = runner._validated_saved_revisions(
                    [receipt],
                    [str(self.month)],
                    {str(self.month): pin},
                    audience=audience,
                )[0]
                self.assertEqual(got["evidenceSnapshot"], pin["payload"])
                self.assertEqual(receipt, original)
                corrupted = copy.deepcopy(receipt)
                corrupted["evidenceSnapshot"]["events"] = ["invented"]
                with self.assertRaises(ValueError):
                    runner._validated_saved_revisions(
                        [corrupted],
                        [str(self.month)],
                        {str(self.month): pin},
                        audience=audience,
                    )
            draft.delete()

    def test_connection_lookup_never_uses_another_user_or_startup(self):
        from startup_updates.preflight import probe_source

        other_org = Organization.objects.create(
            name="Other startup", domain="other.example.invalid"
        )
        ExternalServiceConnection.objects.create(
            user=self.other,
            organization=self.org,
            provider="slack",
            access_token="synthetic",
        )
        ExternalServiceConnection.objects.create(
            user=self.user,
            organization=other_org,
            provider="slack",
            access_token="synthetic",
        )
        with patch(
            "integrations.services.external_connectors._slack_api_request"
        ) as api:
            with self.assertRaises(PermissionError):
                probe_source("slack", user=self.user, organization=self.org)
            api.assert_not_called()

    def test_manual_only_preflight_does_not_require_any_connection(self):
        self.assertEqual(
            require_source_preflight(
                user=self.user,
                organization=self.org,
                input_sources=[],
                manual_summary="We shipped the release.",
            )[0]["status"],
            "ready",
        )

    def test_company_owner_can_use_delegated_upload_but_another_startup_cannot(self):
        from founder_tools.models import VibeRaisingCompany, VibeRaisingProfile
        from startup_updates.models import StartupManualDocument

        profile = VibeRaisingProfile.objects.create(user=self.user, role="founder")
        company = VibeRaisingCompany.objects.create(
            profile=profile, organization=self.org, name="Owned startup"
        )
        document = StartupManualDocument.objects.create(
            organization=self.org,
            company=company,
            created_by=self.other,
            original_filename="synthetic-notes.txt",
            storage_path="synthetic/delegated",
            extraction_status="processed",
            extracted_text="The synthetic release shipped.",
        )
        # Keep TestCase's uncommitted fixtures on its connection. Production
        # probes execute the same function in independent short-lived threads.
        with (
            patch("startup_updates.preflight.ThreadPoolExecutor") as executor,
            patch("startup_updates.preflight.close_old_connections"),
            patch("startup_updates.preflight.connections.close_all"),
        ):
            executor.return_value.__enter__.return_value.map.side_effect = (
                lambda fn, items: map(fn, items)
            )
            checks = require_source_preflight(
                user=self.user,
                organization=self.org,
                input_sources=["manual_documents"],
                manual_document_ids=[str(document.pk)],
            )
            self.assertEqual(checks[0]["status"], "ready")
            other_org = Organization.objects.create(
                name="Another startup", domain="elsewhere.example.invalid"
            )
            with self.assertRaises(SourcesNotReady):
                require_source_preflight(
                    user=self.user,
                    organization=other_org,
                    input_sources=["manual_documents"],
                    manual_document_ids=[str(document.pk)],
                )


@override_settings(MLAI_API_KEY="synthetic")
class PreflightBarrierTests(TestCase):
    def setUp(self):
        from startup_updates.models import StartupProfile, UserStartupBinding
        from workflow_runs.models import ContentFactoryRun

        self.org = Organization.objects.create(
            name="Flow startup", domain="flow.example.invalid"
        )
        self.user = get_user_model().objects.create_user(email="flow@example.invalid")
        StartupProfile.objects.create(organization=self.org)
        binding = UserStartupBinding.objects.create(
            user=self.user, organization=self.org
        )
        self.run = ContentFactoryRun.objects.create(
            run_id="synthetic-flow",
            workflow="startup_monthly_update",
            organization=self.org,
            domain=self.org.domain,
            status="queued",
            run_request={
                "organization_id": self.org.pk,
                "binding_id": binding.pk,
                "source_preflight_version": 1,
                "current_month": "2026-08-01",
                "target_month": "2026-08-01",
                "draft_months": ["2026-08-01"],
                "input_sources": ["gmail", "slack", "notion"],
            },
        )

    def test_real_endpoint_checks_all_sources_and_stops_before_any_full_collection(
        self,
    ):
        from rest_framework.test import APIRequestFactory

        from startup_updates.api_views import (
            StartupUpdateSourcePreflightView,
            _locked_pipeline_run_context,
        )

        def probe(provider, **kwargs):
            if provider == "gmail":
                raise PermissionError()
            if provider == "slack":
                raise TimeoutError()
            return {"connectionId": 7}

        request = APIRequestFactory().post(
            "/", {}, format="json", HTTP_X_API_KEY="synthetic"
        )
        with patch(
            "startup_updates.preflight.probe_source", side_effect=probe
        ) as probes:
            response = StartupUpdateSourcePreflightView.as_view()(
                request, run_id=self.run.run_id
            )
        self.assertEqual(response.status_code, 200, response.data)
        self.assertFalse(response.data["ready"])
        self.assertEqual(
            {x.args[0] for x in probes.call_args_list}, {"gmail", "slack", "notion"}
        )
        self.assertEqual(
            [x["status"] for x in response.data["checks"]],
            ["needs_reconnect", "temporarily_unavailable", "ready"],
        )
        from startup_updates.revisions import RevisionConflict

        with self.assertRaises(RevisionConflict):
            _locked_pipeline_run_context(self.run.run_id)
        self.assertEqual(MonthlyUpdateDraft.objects.count(), 0)

    def test_fences_reject_old_worker_and_resume_never_resurrects_cancellation(self):
        from django.db import transaction

        from startup_updates.execution import claim_execution, validate_execution
        from startup_updates.revisions import RevisionConflict

        with transaction.atomic():
            one = claim_execution(self.run, owner="worker-one")["token"]
            two = claim_execution(self.run, owner="worker-two")["token"]
        self.assertGreater(two, one)
        with self.assertRaises(RevisionConflict):
            validate_execution(self.run, one)
        validate_execution(self.run, two)
        self.run.status = "cancelled"
        self.run.save()
        with self.assertRaises(RevisionConflict):
            claim_execution(self.run, owner="worker-three", resume=True)

    def test_slack_batch_checks_preflight_and_fence_before_touching_connector(self):
        from django.db import transaction
        from rest_framework.test import APIRequestFactory

        from startup_updates.api_views import StartupUpdateSlackClassificationBatchView
        from startup_updates.execution import claim_execution

        request = APIRequestFactory().get("/", HTTP_X_API_KEY="synthetic")
        with patch("startup_updates.api_views._resolve_run_slack_connection") as lookup:
            response = StartupUpdateSlackClassificationBatchView.as_view()(
                request, run_id=self.run.run_id
            )
            self.assertEqual(response.status_code, 409, response.data)
            lookup.assert_not_called()
        self.run.run_request["source_preflight_complete"] = True
        self.run.save()
        with transaction.atomic():
            token = claim_execution(self.run, owner="current-worker")["token"]
        request = APIRequestFactory().get(
            "/", HTTP_X_API_KEY="synthetic", HTTP_X_PULSE_EXECUTION_TOKEN=str(token - 1)
        )
        with patch("startup_updates.api_views._resolve_run_slack_connection") as lookup:
            response = StartupUpdateSlackClassificationBatchView.as_view()(
                request, run_id=self.run.run_id
            )
            self.assertEqual(response.status_code, 409, response.data)
            lookup.assert_not_called()

    def test_preparation_failures_remain_visible_after_reloading_run(self):
        from rest_framework.test import APIRequestFactory

        from startup_updates.api_views import StartupUpdateSourcePreflightView

        request = APIRequestFactory().post(
            "/", {}, format="json", HTTP_X_API_KEY="synthetic"
        )
        with (
            patch(
                "startup_updates.preflight.probe_source",
                return_value={"connectionId": 7},
            ),
            patch(
                "vibe_raising.views._sync_selected_connector_sources_for_draft",
                return_value={"notion": ["unavailable"]},
            ),
            patch(
                "startup_updates.services.refresh_startup_update_run_source_context"
            ) as collect,
        ):
            response = StartupUpdateSourcePreflightView.as_view()(
                request, run_id=self.run.run_id
            )
        self.assertFalse(response.data["ready"])
        collect.assert_not_called()
        self.run.refresh_from_db()
        self.assertEqual(
            self.run.result["source_preflight"]["checks"], response.data["checks"]
        )
        self.assertEqual(
            [c["status"] for c in response.data["checks"]],
            ["ready", "ready", "temporarily_unavailable"],
        )
        self.assertFalse(self.run.run_request["source_preflight_complete"])

    def test_notes_only_runs_real_capture_save_validation_and_review_without_publication(
        self,
    ):
        from django.urls import reverse
        from rest_framework.test import APIClient
        from valley.workflows.runner import WorkflowRunner

        from startup_updates.models import (
            MonthlyUpdateApproval,
            MonthlyUpdateRevision,
            UserStartupBinding,
        )
        from startup_updates.services import create_startup_update_run

        self.run.status = "cancelled"
        self.run.save()
        binding = UserStartupBinding.objects.get(user=self.user, organization=self.org)
        run = create_startup_update_run(
            organization=self.org,
            binding=binding,
            target_month=date(2026, 8, 1),
            input_sources=["manual_documents"],
            manual_summary="We shipped the synthetic offline release on 12 August 2026.",
        )
        api = APIClient()
        from django.db import transaction

        from startup_updates.execution import claim_execution

        with transaction.atomic():
            token = claim_execution(run, owner="synthetic-real-worker")["token"]
        api.credentials(
            HTTP_X_API_KEY="synthetic", HTTP_X_PULSE_EXECUTION_TOKEN=str(token)
        )
        preflight = api.post(
            reverse("startup_updates_source_preflight", args=[run.run_id]),
            {},
            format="json",
        )
        self.assertEqual(preflight.status_code, 200, preflight.data)
        self.assertTrue(preflight.data["ready"], preflight.data)
        run.refresh_from_db()
        refreshed = api.post(
            reverse("startup_updates_source_evidence_refresh", args=[run.run_id]),
            {},
            format="json",
        )
        self.assertEqual(refreshed.status_code, 200, refreshed.data)
        snapshots = api.post(
            reverse("startup_updates_evidence_snapshot", args=[run.run_id]),
            {},
            format="json",
        )
        self.assertEqual(snapshots.status_code, 200, snapshots.data)

        class Bridge:
            def capture_evidence_snapshot(_self, run_id):
                return snapshots.data["snapshots"]

            def submit_draft_results(_self, run_id, drafts):
                response = api.post(
                    reverse("startup_updates_draft_results", args=[run_id]),
                    {"drafts": drafts},
                    format="json",
                    HTTP_X_PULSE_RECEIPT_VERSION="2",
                )
                self.assertEqual(response.status_code, 200, response.data)
                return response.data

        runner = object.__new__(WorkflowRunner)
        runner.client = Bridge()
        runner.settings = SimpleNamespace(
            openai_reasoning_model="synthetic",
            llm_max_concurrency=1,
            openai_review_model="synthetic-review",
        )
        runner._sync_remote_run = lambda *args: None
        runner.llm = SimpleNamespace(
            generate_drafts=lambda **kwargs: [
                {
                    "month": "2026-08-01",
                    "structured_memo": {
                        "title": "August update",
                        "topline": "We shipped the synthetic offline release.",
                        "highlights": [
                            "We shipped the synthetic offline release on 12 August 2026."
                        ],
                    },
                }
            ],
            review_drafts=lambda **kwargs: [
                {
                    "month": "2026-08-01",
                    "audience": "private",
                    "status": "ready",
                    "groundedness_status": "passed",
                }
            ],
        )
        local = SimpleNamespace(run_id=run.run_id, state={})
        backend = SimpleNamespace(domain=self.org.domain, run_request=run.run_request)
        outcome = runner._step_draft_generation(backend, local)
        local.state.update(outcome.state_patch)
        first = MonthlyUpdateRevision.objects.count()
        # Lost save acknowledgement: same output acknowledges one immutable revision.
        runner._step_draft_generation(backend, local)
        self.assertEqual(MonthlyUpdateRevision.objects.count(), first)
        review = runner._step_groundedness_review(backend, local)
        self.assertFalse(review.requeue)
        draft = MonthlyUpdateDraft.objects.get(run=run)
        self.assertEqual(
            draft.current_revision.validation["groundedness_status"], "passed"
        )
        self.assertIsNone(draft.published_revision_id)
        self.assertEqual(MonthlyUpdateApproval.objects.count(), 0)

    def test_every_singleton_pair_and_all_sources_pass_real_preflight_before_collection(
        self,
    ):
        from itertools import combinations

        from rest_framework.test import APIRequestFactory

        from startup_updates.api_views import StartupUpdateSourcePreflightView

        providers = sorted(PROVIDERS)
        selections = (
            [(p,) for p in providers]
            + list(combinations(providers, 2))
            + [tuple(providers)]
        )
        for sources in selections:
            with self.subTest(sources=sources):
                self.run.refresh_from_db()
                self.run.run_request["source_connection_pins"] = {}
                self.run.run_request["source_preflight_complete"] = False
                self.run.run_request["input_sources"] = list(sources)
                self.run.run_request["manual_summary"] = (
                    "We shipped a synthetic release."
                )
                self.run.result = {}
                self.run.save()
                request = APIRequestFactory().post(
                    "/", {}, format="json", HTTP_X_API_KEY="synthetic"
                )
                with (
                    patch(
                        "startup_updates.preflight.probe_source",
                        return_value={"connectionId": 7},
                    ) as probes,
                    patch(
                        "startup_updates.services.refresh_startup_update_run_source_context"
                    ) as collect,
                    patch(
                        "vibe_raising.views._sync_selected_connector_sources_for_draft",
                        return_value={},
                    ),
                ):
                    response = StartupUpdateSourcePreflightView.as_view()(
                        request, run_id=self.run.run_id
                    )
                    self.assertEqual(response.status_code, 200, response.data)
                    self.assertTrue(response.data["ready"], response.data)
                    self.assertEqual(
                        {c.args[0] for c in probes.call_args_list},
                        set(sources) - {"manual_documents"},
                    )
                    collect.assert_called_once()
                self.run.refresh_from_db()
                self.assertTrue(self.run.run_request["source_preflight_complete"])

    def test_automatic_recovery_is_bounded_and_clears_old_dead_letters(self):
        from django.db import transaction

        from startup_updates.execution import claim_execution
        from startup_updates.revisions import RevisionConflict

        for attempt in range(5):
            self.run.status = "failed"
            self.run.resume_available = True
            self.run.result = {
                **self.run.result,
                "_valley_meta": {
                    **(self.run.result.get("_valley_meta") or {}),
                    "dead_letters": [{"retryable": True, "resolved": False}],
                },
            }
            self.run.save()
            with transaction.atomic():
                claim_execution(self.run, owner="dead-letter-recovery", resume=True)
            self.assertEqual(
                self.run.result["_valley_meta"]["automatic_recovery_count"], attempt + 1
            )
            self.assertTrue(
                self.run.result["_valley_meta"]["dead_letters"][0]["resolved"]
            )
        self.run.status = "failed"
        self.run.resume_available = True
        self.run.save()
        with transaction.atomic(), self.assertRaises(RevisionConflict):
            claim_execution(self.run, owner="dead-letter-recovery", resume=True)


class CachedAccountScopeTests(SimpleTestCase):
    def test_cached_metrics_cannot_fill_from_another_account_or_missing_provenance(
        self,
    ):
        from startup_updates.connection_scope import metric_in_scope

        for provider, metadata, expected in [
            ("xero", {"connection_id": 7}, True),
            ("xero", {"connection_id": 8}, False),
            ("financial", {"connection_ids": [7]}, True),
            ("financial", {"connection_ids": [7, 8]}, False),
            ("xero", {}, False),
        ]:
            with self.subTest(provider=provider, metadata=metadata):
                metric = SimpleNamespace(
                    source_provider=provider, source_metadata=metadata, run_id=1
                )
                self.assertEqual(
                    metric_in_scope(metric, {"xero": 7, "stripe": 7}), expected
                )
        metric = SimpleNamespace(source_provider="gmail", source_metadata={}, run_id=3)
        self.assertTrue(metric_in_scope(metric, {"gmail": 7}, run_id=3))
        self.assertFalse(metric_in_scope(metric, {"gmail": 7}, run_id=4))


@override_settings(MLAI_API_KEY="synthetic")
class CatalogAndBankEvidenceTests(TestCase):
    def setUp(self):
        from startup_updates.models import UserStartupBinding
        from workflow_runs.models import ContentFactoryRun

        self.org = Organization.objects.create(
            name="Cash evidence", domain="cash.example.invalid"
        )
        self.user = get_user_model().objects.create_user(email="cash@example.invalid")
        self.binding = UserStartupBinding.objects.create(
            user=self.user, organization=self.org
        )
        self.connection = ExternalServiceConnection.objects.create(
            user=self.user,
            organization=self.org,
            provider="bank_feed",
            external_account_id="selected-user",
        )
        self.run = ContentFactoryRun.objects.create(
            run_id="cash-flow",
            workflow="startup_monthly_update",
            organization=self.org,
            domain=self.org.domain,
            run_request={
                "organization_id": self.org.pk,
                "binding_id": self.binding.pk,
                "input_sources": ["bank_feed"],
                "source_preflight_complete": True,
                "source_connection_pins": {"bank_feed": self.connection.pk},
                "draft_months": ["2026-08-01"],
                "current_month": "2026-08-01",
                "target_month": "2026-08-01",
                "source_period_contract": "calendar_month_v1",
                "reporting_timezone": "Australia/Melbourne",
                "narrative_period": {
                    "start": "2026-08-01T00:00:00+10:00",
                    "end": "2026-09-01T00:00:00+10:00",
                },
            },
        )

    def test_bank_pagination_checks_origin_account_and_repeated_cursors_before_io(self):
        from django.test import override_settings

        from startup_updates.bank_sources import fetch_bank_page

        with override_settings(BASIQ_API_BASE_URL="https://bank.example.invalid"):
            for cursor in (
                "https://attacker.invalid/users/selected-user/transactions",
                "/users/another-user/transactions",
            ):
                with patch(
                    "integrations.services.external_connectors._basiq_get_json"
                ) as get:
                    with self.assertRaises(ValueError):
                        fetch_bank_page(self.run, self.binding, {"cursor": cursor})
                    get.assert_not_called()
            with (
                patch(
                    "integrations.services.external_connectors._basiq_token",
                    return_value={"access_token": "synthetic"},
                ),
                patch(
                    "integrations.services.external_connectors._basiq_get_json",
                    return_value={
                        "data": [],
                        "links": {"next": "/users/selected-user/transactions"},
                    },
                ),
                self.assertRaises(ValueError),
            ):
                fetch_bank_page(self.run, self.binding, {})

    def test_bank_only_refresh_replays_one_page_and_captures_completed_cash_evidence(
        self,
    ):
        from django.urls import reverse
        from rest_framework.test import APIClient

        from startup_updates.models import StartupEvent
        from startup_updates.revisions import capture_snapshot

        api = APIClient()
        api.credentials(HTTP_X_API_KEY="synthetic")
        rows = [
            {
                "id": "cash-1",
                "transactionDate": "2026-08-12",
                "amount": "12.50",
                "currency": "AUD",
                "direction": "credit",
                "description": "Do not retain merchant PII",
            },
            {
                "id": "outside",
                "transactionDate": "2026-09-01",
                "amount": "99",
                "currency": "AUD",
            },
        ]
        url = reverse("startup_updates_source_evidence_refresh", args=[self.run.run_id])
        with patch(
            "startup_updates.bank_sources.fetch_bank_page",
            return_value=(rows, {"complete": True}),
        ) as fetch:
            response = api.post(url, {}, format="json")
            self.assertEqual(response.status_code, 200, response.data)
            self.assertTrue(response.data["pending"])
            response = api.post(url, {}, format="json")
            self.assertEqual(response.status_code, 200, response.data)
            self.assertNotIn("pending", response.data)
            fetch.assert_called_once()
        self.run.refresh_from_db()
        from startup_updates.bank_sources import commit_bank_page

        commit_bank_page(self.run, rows, {"complete": True})
        self.run.save()
        self.assertEqual(StartupEvent.objects.filter(run=self.run).count(), 1)
        snapshot = capture_snapshot(self.org, date(2026, 8, 1), run=self.run)
        receipt = snapshot.payload["source_evidence"]["bank_feed:cash-1"]
        self.assertEqual(receipt["output"]["facts"][0]["amount"], "12.50")
        self.assertEqual(len(snapshot.payload["events"]), 1)
        self.assertNotIn("Do not retain merchant PII", str(snapshot.payload))
        self.assertIn(
            "not accounting revenue", snapshot.payload["events"][0]["summary"]
        )

    def test_multiple_catalog_events_have_distinct_keys_completed_receipts_and_local_month(
        self,
    ):
        from startup_updates.event_sources import commit_event_page
        from startup_updates.models import StartupEvent
        from startup_updates.revisions import capture_snapshot

        rows = [
            {
                "event_id": str(i),
                "name": f"Event {i}",
                "start_at": "2026-07-31T14:30:00Z",
            }
            for i in (1, 2)
        ]
        commit_event_page(self.run, "humanitix", rows, {"complete": True})
        commit_event_page(self.run, "humanitix", rows, {"complete": True})
        self.run.save()
        self.assertEqual(
            StartupEvent.objects.filter(
                run=self.run, month_bucket="2026-08-01"
            ).count(),
            2,
        )
        snapshot = capture_snapshot(self.org, date(2026, 8, 1), run=self.run)
        self.assertEqual(len(snapshot.payload["events"]), 2)
        self.assertTrue(
            all(r.get("output") for r in snapshot.payload["source_evidence"].values())
        )

    def test_reviewed_repairs_are_immutable_idempotent_and_bounded(self):
        from django.urls import reverse
        from rest_framework.test import APIClient

        from startup_updates.models import MonthlyUpdateRevision
        from startup_updates.revisions import capture_snapshot

        snapshot = capture_snapshot(self.org, date(2026, 8, 1), run=self.run)
        self.run.run_request["evidence_snapshots"] = {
            "2026-08-01": {"snapshot_id": snapshot.pk, "expected_revision": None}
        }
        self.run.save()
        api = APIClient()
        api.credentials(HTTP_X_API_KEY="synthetic", HTTP_X_PULSE_RECEIPT_VERSION="2")
        url = reverse("startup_updates_draft_results", args=[self.run.run_id])

        def submit(payload):
            return api.post(url, {"drafts": [payload]}, format="json")

        payload = {
            "month": "2026-08-01",
            "audience": "private",
            "snapshot_id": snapshot.pk,
            "expected_revision": None,
            "structured_memo": {"title": "Original synthetic draft"},
        }
        response = submit(payload)
        self.assertEqual(response.status_code, 200, response.data)
        receipt = response.data["drafts"][0]
        original_id = receipt["revisionId"]
        for attempt in range(3):
            response = submit(
                {
                    **payload,
                    "revision_id": receipt["revisionId"],
                    "revision_hash": receipt["revisionHash"],
                    "status": "needs_review",
                    "groundedness_status": "failed",
                    "groundedness_notes": "Remove unsupported wording.",
                }
            )
            self.assertEqual(response.status_code, 200, response.data)
            repaired = {
                **payload,
                "repair_revision_id": receipt["revisionId"],
                "structured_memo": {"title": f"Correction {attempt + 1}"},
            }
            response = submit(repaired)
            if attempt == 2:
                self.assertEqual(response.status_code, 409, response.data)
                break
            self.assertEqual(response.status_code, 200, response.data)
            receipt = response.data["drafts"][0]
            count = MonthlyUpdateRevision.objects.count()
            replay = submit(repaired)
            self.assertEqual(replay.status_code, 200, replay.data)
            self.assertEqual(
                replay.data["drafts"][0]["revisionId"], receipt["revisionId"]
            )
            self.assertEqual(MonthlyUpdateRevision.objects.count(), count)
        self.assertEqual(MonthlyUpdateRevision.objects.count(), 3)
        self.assertEqual(
            MonthlyUpdateRevision.objects.get(pk=original_id).structured_memo["title"],
            "Original synthetic draft",
        )
        self.assertFalse(
            MonthlyUpdateDraft.objects.exclude(published_revision_id=None).exists()
        )


class ConcurrentExecutionTests(TransactionTestCase):
    def test_postgres_claims_are_monotonic_under_competing_workers(self):
        from concurrent.futures import ThreadPoolExecutor

        from django.db import (
            close_old_connections,
            connection,
            connections,
            transaction,
        )

        from startup_updates.execution import claim_execution
        from workflow_runs.models import ContentFactoryRun

        if connection.vendor != "postgresql":
            self.skipTest("Row-lock contention requires PostgreSQL")
        org = Organization.objects.create(
            name="Concurrency", domain="concurrency.example.invalid"
        )
        run = ContentFactoryRun.objects.create(
            run_id="contended",
            organization=org,
            domain=org.domain,
            workflow="startup_monthly_update",
        )

        def claim(index):
            close_old_connections()
            try:
                with transaction.atomic():
                    locked = ContentFactoryRun.objects.select_for_update().get(
                        pk=run.pk
                    )
                    return claim_execution(locked, owner=f"synthetic-worker-{index}")[
                        "token"
                    ]
            finally:
                connections.close_all()

        with ThreadPoolExecutor(max_workers=8) as pool:
            tokens = list(pool.map(claim, range(8)))
        self.assertEqual(sorted(tokens), list(range(1, 9)))
        run.refresh_from_db()
        self.assertEqual(run.result["_pulse_execution"]["token"], 8)
