"""Database integration checks. Run only with explicit migration approval."""
from datetime import date
from decimal import Decimal
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from rest_framework.test import APIClient
from organizations.models import Organization
from founder_tools.models import VibeRaisingCompany, VibeRaisingProfile
from startup_updates.models import StartupProfile, StartupMetricObservation, MonthlyEvidenceSnapshot, MonthlyUpdateDraft
from startup_updates.evidence_contract import content_hash
from startup_updates.revisions import save_revision, approve_and_publish
from .progress import get_progress_series
from .views import _serialize_monthly_update

BASE = "/api/v1/vibe-raising/progress/"

@override_settings(STARTUP_PROGRESS_ENABLED=True)
class ProgressApiTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(email="progress@example.invalid", password="test-only")
        self.profile = VibeRaisingProfile.objects.create(user=self.user, role="founder")
        self.org = Organization.objects.create(name="Progress",domain="progress.example.invalid")
        self.company = VibeRaisingCompany.objects.create(profile=self.profile, organization=self.org, name="Progress",domain=self.org.domain)
        self.startup = StartupProfile.objects.create(organization=self.org, reporting_timezone="Australia/Melbourne")
        self.client = APIClient(); self.client.force_authenticate(self.user)

    def post(self, suffix="", **body):
        return self.client.post(BASE+suffix,{"companyId":str(self.company.pk),**body},format="json")

    def create_metric(self):
        response=self.post("custom-metrics/",expectedVersion=0,label="Active pilots",definition="Companies in agreed pilots at month end",category="customers",unit="pilots",aggregation="stock",points=[{"date":"2026-07-01","value":2},{"date":"2026-08-01","value":5}])
        self.assertEqual(response.status_code,201,response.data)
        return response.data

    def test_company_scope_preferences_and_stale_write(self):
        other_user=get_user_model().objects.create_user(email="other@example.invalid",password="test-only")
        other_profile=VibeRaisingProfile.objects.create(user=other_user,role="founder")
        other=VibeRaisingCompany.objects.create(profile=other_profile,name="Other",domain="other.example.invalid")
        response=self.client.get(BASE,{"company_id":str(other.pk)})
        self.assertEqual(response.status_code,404)
        data=self.create_metric(); item=data["series"][0]
        chart={"id":item["id"],"seriesIds":[item["id"]],"months":6,"type":"line","caption":""}
        response=self.post(expectedVersion=data["version"],charts=[chart],range=6)
        self.assertEqual(response.status_code,200,response.data)
        self.assertEqual(self.post(expectedVersion=data["version"],charts=[],range=6).status_code,409)
        self.assertEqual(self.post(expectedVersion=response.data["version"],charts=[{**chart,"seriesIds":["foreign"]}],range=6).status_code,400)
        self.assertEqual(self.client.get(BASE,{"company_id":str(self.company.pk)}).data["charts"],[chart])

    def test_custom_metric_cannot_overwrite_imported_series_or_change_definition(self):
        data=self.create_metric(); definition=data["definitions"][0]
        payload={**definition,"expectedVersion":data["version"],"points":[{"date":"2026-08-01","value":6}]}
        self.assertEqual(self.post("custom-metrics/",**{**payload,"key":"revenue"}).status_code,400)
        self.assertEqual(self.post("custom-metrics/",**{**payload,"unit":"AUD"}).status_code,400)
        self.assertEqual(self.post("custom-metrics/",**{**payload,"definition":"Changed meaning"}).status_code,400)
        self.assertEqual(self.post("custom-metrics/",**payload).status_code,201)
        self.assertEqual(StartupMetricObservation.objects.filter(organization=self.org,metric_key=definition["key"],period_month=date(2026,8,1)).count(),1)

    def test_frozen_chart_publishing_inheritance_removal_and_disclosure(self):
        self.create_metric()
        item=get_progress_series(self.org)[0]
        spec={"id":item["id"],"seriesIds":[item["id"]],"months":6,"type":"line","caption":"Pilot progress"}
        draft=MonthlyUpdateDraft.objects.create(organization=self.org,month=date(2026,9,1),update_date=date(2026,9,18))
        payload={"metrics":[{"key":"revenue","label":"Hidden income","value":"99999","display_value":"AUD 99999","unit":"AUD","quality":"source_reported"}],"events":[],"charts":{"performance":[]}}
        snapshot=MonthlyEvidenceSnapshot.objects.create(organization=self.org,month=draft.month,payload=payload,content_hash=content_hash(payload))
        first=save_revision(draft,{"highlights":["We shipped."],"_progress_chart_specs":[spec],"progress_charts":[{"value":999}]},snapshot=snapshot)
        approved=approve_and_publish(draft,actor=self.user,revision_id=first.pk,revision_hash=first.content_hash,audience_visibility=["just_me"])
        StartupMetricObservation.objects.filter(organization=self.org,metric_key=item["metricKey"]).update(value_number=Decimal(900))
        draft.refresh_from_db()
        inherited=save_revision(draft,{"highlights":["Edited writing."]},snapshot=snapshot,expected_revision=first.pk)
        self.assertEqual(inherited.structured_memo["progress_charts"],first.structured_memo["progress_charts"])
        draft.refresh_from_db()
        removed=save_revision(draft,{"highlights":["Edited again."],"_progress_chart_specs":[]},snapshot=snapshot,expected_revision=inherited.pk)
        self.assertEqual(removed.structured_memo["progress_charts"],[])
        draft.refresh_from_db()
        public=_serialize_monthly_update(draft,published=True)
        self.assertEqual(public["metrics"],{})
        self.assertIsNone(public["financialSnapshot"])
        self.assertEqual(public["progressCharts"][0]["series"][0]["points"][-1]["value"],5)
        self.assertNotIn("scope",public["progressCharts"][0]["series"][0])
        self.assertNotIn("observationId",public["progressCharts"][0]["series"][0]["points"][0])
        with self.assertRaises(ValueError): first.save()

    def test_disabled_flag_hides_api(self):
        with override_settings(STARTUP_PROGRESS_ENABLED=False):
            self.assertEqual(self.client.get(BASE,{"company_id":str(self.company.pk)}).status_code,404)

    def test_ga_history_is_idempotent_and_failure_keeps_prior_values(self):
        from integrations.models import ExternalServiceConnection
        from startup_updates.models import GoogleAnalyticsPropertySelection
        from .progress_google_analytics import sync_progress_google_analytics
        from requests.exceptions import Timeout
        connection=ExternalServiceConnection.objects.create(user=self.user,organization=self.org,provider="google_analytics",status="connected")
        GoogleAnalyticsPropertySelection.objects.create(user=self.user,organization=self.org,connection=connection,property_id="123",selected=True)
        report={"metricHeaders":[{"name":name} for name in ["totalUsers","sessions","newUsers","engagementRate"]],"metadata":{"timeZone":"Australia/Melbourne"},"rowCount":1,"rows":[{"dimensionValues":[{"value":"202608"}],"metricValues":[{"value":"20"},{"value":"40"},{"value":"15"},{"value":"0.5"}]}]}
        events={"rowCount":1,"rows":[{"dimensionValues":[{"value":"first_project"}]}]}
        with patch('vibe_raising.progress_google_analytics._google_analytics_required_token',return_value='local-fixture'), patch('vibe_raising.progress_google_analytics._fetch_run_report',side_effect=[report,events,report,events]):
            for version in [0,1]:
                sync_progress_google_analytics(self.org,{"propertyId":"123","expectedVersion":version,"eventName":""})
        self.assertEqual(StartupMetricObservation.objects.filter(organization=self.org,source_provider="google_analytics").count(),4)
        self.assertEqual(StartupMetricObservation.objects.get(organization=self.org,metric_key="ga.engagementRate").value_number,50)
        with patch('vibe_raising.progress_google_analytics._google_analytics_required_token',return_value='local-fixture'), patch('vibe_raising.progress_google_analytics._fetch_run_report',side_effect=Timeout):
            with self.assertRaises(Timeout):
                sync_progress_google_analytics(self.org,{"propertyId":"123","expectedVersion":2})
        self.assertEqual(StartupMetricObservation.objects.filter(organization=self.org,source_provider="google_analytics").count(),4)
        self.startup.refresh_from_db()
        self.assertEqual(self.startup.progress_configuration['version'],2)
