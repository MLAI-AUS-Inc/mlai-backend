"""Database integration tests. Run only after the repository's migration approval."""
from datetime import date
from django.contrib.auth import get_user_model
from django.test import TestCase
from rest_framework.exceptions import ValidationError
from organizations.models import Organization
from startup_updates.models import MonthlyEvidenceSnapshot, MonthlyUpdateDraft, MonthlyUpdateApproval
from startup_updates.evidence_contract import content_hash
from startup_updates.revisions import save_revision, approve_and_publish, frozen_memo, RevisionConflict


class MonthlyRevisionTests(TestCase):
    def setUp(self):
        self.organization = Organization.objects.create(name="Example", domain="example.invalid")
        self.actor = get_user_model().objects.create_user(email="founder@example.invalid", password="test-only")
        self.month = date(2026, 1, 1)
        self.draft = MonthlyUpdateDraft.objects.create(organization=self.organization, month=self.month)
        payload = {"metrics": [{"key": "revenue", "label": "Revenue", "value": "100", "display_value": "AUD 100", "unit": "AUD", "quality": "source_reported"}], "events": [], "charts": {"performance": []}}
        self.snapshot = MonthlyEvidenceSnapshot.objects.create(organization=self.organization, month=self.month, content_hash=content_hash(payload), payload=payload)

    def save(self, memo=None, **kwargs):
        self.draft.refresh_from_db()
        return save_revision(self.draft, memo or {"highlights": ["Revenue was {{metric:revenue}}."]}, snapshot=self.snapshot, **kwargs)

    def publish(self, revision, audience=None):
        return approve_and_publish(self.draft, actor=self.actor, revision_id=revision.pk, revision_hash=revision.content_hash, audience_visibility=audience or ["just_me"])

    def test_revision_and_snapshot_are_immutable(self):
        revision = self.save()
        with self.assertRaises(ValueError):
            revision.save()
        with self.assertRaises(ValueError):
            self.snapshot.save()

    def test_stale_edit_and_publish_rejected(self):
        first = self.save()
        second = self.save({"highlights": ["We shipped the release."]}, expected_revision=first.pk)
        with self.assertRaises(RevisionConflict):
            self.save(expected_revision=first.pk)
        with self.assertRaises(RevisionConflict):
            self.publish(first)
        self.publish(second)

    def test_disclosure_change_requires_new_revision(self):
        revision = self.save()
        with self.assertRaises(RevisionConflict):
            self.publish(revision, ["community"])
        self.assertFalse(MonthlyUpdateApproval.objects.exists())

    def test_published_copy_unchanged_after_edit(self):
        first = self.save()
        self.publish(first)
        self.save({"highlights": ["Unpublished replacement."]}, expected_revision=first.pk)
        self.draft.refresh_from_db()
        self.assertEqual(frozen_memo(self.draft, published=True)["highlights"], ["Revenue was AUD 100."])
        self.assertEqual(frozen_memo(self.draft)["highlights"], ["Unpublished replacement."])

    def test_publish_is_idempotent(self):
        revision = self.save()
        first = self.publish(revision).published_at
        second = self.publish(revision).published_at
        self.assertEqual(first, second)
        self.assertEqual(MonthlyUpdateApproval.objects.count(), 1)

    def test_wrong_tenant_or_month_snapshot_rejected(self):
        other = Organization.objects.create(name="Other", domain="other.invalid")
        self.snapshot.organization = other
        with self.assertRaises(ValidationError):
            self.save()

    def test_community_does_not_inherit_unselected_financial_data(self):
        revision = self.save({"highlights": ["We shipped."], "kpi_snapshot": []}, audience="community")
        self.assertEqual(revision.structured_memo["kpi_snapshot"], [])
        self.assertIsNone(revision.structured_memo["financial_snapshot"])
        self.publish(revision, ["community"])

    def test_pending_machine_review_blocks_publication(self):
        revision = self.save()
        type(revision).objects.filter(pk=revision.pk).update(validation={"groundedness_status": "pending"})
        with self.assertRaises(ValidationError):
            self.publish(revision)


from integrations.tests_startup_updates import StartupUpdateApiTestCase
from startup_updates.models import StartupMetricObservation, StartupProfile, UserStartupBinding
from startup_updates.services import create_startup_update_run
from django.urls import reverse


class MonthlyEvidencePipelineTests(StartupUpdateApiTestCase):
    def setUp(self):
        super().setUp()
        StartupProfile.objects.create(organization=self.organization, default_currency="AUD")
        binding = UserStartupBinding.objects.create(user=self.user, organization=self.organization, google_connection=self.google_connection)
        self.run = create_startup_update_run(organization=self.organization, binding=binding)
        self.run.run_request["draft_months"] = ["2026-03-01"]
        self.run.run_request["input_sources"] = ["gmail", "xero"]
        self.run.save(update_fields=["run_request"])
        self.observation = StartupMetricObservation.objects.create(
            organization=self.organization, period_month=date(2026, 3, 1),
            metric_key="revenue", metric_name="Revenue", value_number=100,
            value_text="AUD 100", unit="AUD", source_provider="xero",
            source_metadata={"source_metric": "xero_profit_and_loss_revenue"})

    def pin(self):
        with self._with_key():
            refresh = self.client.post(reverse("startup_updates_source_evidence_refresh", args=[self.run.run_id]), {}, format="json", **self.headers)
            self.assertEqual(refresh.status_code, 200, refresh.data)
            response = self.client.post(reverse("startup_updates_evidence_snapshot", args=[self.run.run_id]), {}, format="json", **self.headers)
        self.assertEqual(response.status_code, 200, response.data)
        return response.data["snapshots"]["2026-03-01"]

    def submit(self, item):
        with self._with_key():
            return self.client.post(reverse("startup_updates_draft_results", args=[self.run.run_id]), {"drafts": [item]}, format="json", **self.headers)

    def test_generation_retry_and_review_use_pinned_values(self):
        pin = self.pin()
        self.observation.value_number = 999
        self.observation.value_text = "AUD 999"
        self.observation.save()
        self.assertEqual(self.pin(), pin)
        item = {"month": "2026-03-01", "snapshot_id": pin["snapshot_id"], "expected_revision": pin["expected_revision"],
            "structured_memo": {"highlights": ["Revenue was {{metric:revenue}}."]}}
        saved = self.submit(item)
        self.assertEqual(saved.status_code, 200, saved.data)
        receipt = saved.data["drafts"][0]
        draft = MonthlyUpdateDraft.objects.get(organization=self.organization, month=date(2026, 3, 1))
        self.assertEqual(draft.current_revision.structured_memo["highlights"], ["Revenue was AUD 100."])
        self.assertEqual(draft.current_revision.snapshot.payload["charts"]["performance"][-1]["income"], 100)
        retry = self.submit(item)
        self.assertEqual(retry.status_code, 200, retry.data)
        self.assertEqual(retry.data["drafts"][0]["revisionId"], receipt["revisionId"])
        reviewed = self.submit({**item, "revision_id": receipt["revisionId"], "revision_hash": receipt["revisionHash"], "groundedness_status": "passed"})
        self.assertEqual(reviewed.status_code, 200, reviewed.data)
        draft.current_revision.refresh_from_db()
        self.assertEqual(draft.current_revision.validation["groundedness_status"], "passed")
        changed = self.submit({**item, "structured_memo": {"highlights": ["Changed retry"]}})
        self.assertEqual(changed.status_code, 409)

    def test_rejects_unbound_generated_revenue_and_foreign_snapshot(self):
        pin = self.pin()
        item = {"month": "2026-03-01", "snapshot_id": pin["snapshot_id"], "expected_revision": None,
            "structured_memo": {"highlights": ["Revenue was AUD 999."]}}
        self.assertEqual(self.submit(item).status_code, 400)
        self.assertEqual(self.submit({**item, "snapshot_id": pin["snapshot_id"] + 100}).status_code, 409)
        self.assertFalse(MonthlyUpdateDraft.objects.filter(organization=self.organization).exists())

    def test_snapshot_excludes_supplier_receipts_and_wrong_currency(self):
        self.observation.delete()
        for provider, amount, unit, metadata in [
            ("xero", 400, "USD", {"source_metric": "xero_profit_and_loss_revenue"}),
            ("financial", 900, "AUD", {"source_metric": "supplier_payment"}),
        ]:
            StartupMetricObservation.objects.create(organization=self.organization, period_month=date(2026, 3, 1),
                metric_key="revenue", metric_name="Revenue", value_number=amount, value_text=str(amount),
                unit=unit, source_provider=provider, source_metadata=metadata)
        revenue = next(item for item in self.pin()["payload"]["metrics"] if item["key"] == "revenue")
        self.assertIsNone(revenue["value"])
        self.assertEqual(revenue["quality"], "unknown")


class FounderEvidenceApiTests(TestCase):
    def setUp(self):
        from rest_framework.test import APIClient
        from founder_tools.models import VibeRaisingCompany, VibeRaisingProfile
        self.user = get_user_model().objects.create_user(email="domainless@example.invalid", password="test-only")
        self.profile = VibeRaisingProfile.objects.create(user=self.user, role="founder")
        self.company = VibeRaisingCompany.objects.create(profile=self.profile, name="Global Startup", domain="", registered=False)
        self.profile.active_company = self.company
        self.profile.save()
        self.client = APIClient()
        self.client.force_authenticate(self.user)

    def save_update(self, **extra):
        response = self.client.post("/api/v1/vibe-raising/updates/", {"companyId": self.company.pk,
            "month": "March", "year": 2026, "highlights": "Shipped our release.", "saveMode": "draft",
            "metrics": {"revenue": "100"}, **extra}, format="json")
        self.assertIn(response.status_code, (200, 201), response.data)
        return response.data["update"]

    def test_health_cards_and_charts_share_saved_snapshot_across_config_change(self):
        update = self.save_update()
        health = self.client.get("/api/v1/vibe-raising/business-health/", {"company_id": self.company.pk})
        self.assertEqual(health.status_code, 200, health.data)
        snapshot = MonthlyEvidenceSnapshot.objects.get(pk=update["snapshotId"])
        self.assertEqual(health.data["snapshot_hash"], snapshot.content_hash)
        revenue = next(item for item in health.data["metrics"] if item["key"] == "revenue")
        self.assertEqual(revenue["display_value"], update["metrics"]["revenue"])
        self.assertEqual(revenue["quality"], "founder_asserted")
        self.assertEqual(snapshot.payload["charts"]["performance"][-1]["income"], 100)
        changed = self.client.post("/api/v1/vibe-raising/business-health/", {"companyId": self.company.pk,
            "timezone": "Australia/Melbourne", "currency": "AUD", "metricLabel": "Experiments completed",
            "metricDefinition": "Experiments with results reviewed during the reporting month."}, format="json")
        self.assertEqual(changed.status_code, 200, changed.data)
        snapshot.refresh_from_db()
        self.assertEqual(snapshot.payload["period"]["timezone"], "UTC")
        self.assertEqual(snapshot.payload["config_version"], 1)
        self.company.refresh_from_db()
        self.assertEqual(self.company.domain, "")
        self.assertEqual(self.company.organization.name, "Global Startup")

    def test_receipt_required_and_published_copy_survives_stale_edit(self):
        update = self.save_update(audienceVisibility=["community"])
        url = f'/api/v1/vibe-raising/updates/{update["id"]}/publish/'
        self.assertEqual(self.client.post(url, {"companyId": self.company.pk}, format="json").status_code, 409)
        receipt = {"companyId": self.company.pk, "revisionId": update["revisionId"], "revisionHash": update["revisionHash"], "audienceVisibility": ["community"]}
        self.assertEqual(self.client.post(url, receipt, format="json").status_code, 200)
        edited = self.save_update(expectedRevision=update["revisionId"], highlights="Still under review.")
        self.assertNotEqual(edited["revisionId"], update["revisionId"])
        self.assertIsNone(edited["publishedAt"])
        self.assertEqual(self.client.post(url, receipt, format="json").status_code, 409)
        public = self.client.get("/api/v1/vibe-raising/updates/").data["updates"][0]
        self.assertEqual(public["highlights"], "Shipped our release.")
        self.assertEqual(public["revisionId"], update["revisionId"])
        self.assertIsNone(public["evidenceSnapshot"])
        self.assertIsNone(public["financialSnapshot"])

    def test_configuration_rejects_invalid_definitions_and_wrong_company(self):
        for bad in ({"wrong": "shape"}, [{"key": [], "label": "A", "definition": "B"}], [{"key": "revenue", "label": "Revenue", "definition": "Invented"}]):
            response = self.client.post("/api/v1/vibe-raising/business-health/", {"companyId": self.company.pk, "metricDefinitions": bad}, format="json")
            self.assertEqual(response.status_code, 400, response.data)
        from uuid import uuid4
        response = self.client.get("/api/v1/vibe-raising/business-health/", {"company_id": str(uuid4())})
        self.assertEqual(response.status_code, 404)
