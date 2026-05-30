from datetime import datetime, timedelta, timezone as datetime_timezone
from io import BytesIO
from types import SimpleNamespace
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import OperationalError, connection
from django.test import TestCase, TransactionTestCase, override_settings
from django.test.utils import CaptureQueriesContext
from django.utils import timezone
from rest_framework.test import APIClient

from content_factory.google_baseline import (
    GA4_SCOPE,
    GSC_SCOPE,
    collect_verified_google_metrics,
    google_baseline_connection_status,
)
from content_factory.models import (
    AISaturation,
    ClusterMembership,
    KeywordVelocity,
    KeywordStatus,
    OrganizationContentConfig,
    PAQuestion,
    ResearchedKeyword,
    SemanticCluster,
    TopicFeedback,
    WebsiteBaselineSnapshot,
    WrittenArticle,
)
from founder_tools.models import VibeRaisingCompany, VibeRaisingProfile
from founder_tools.serializers import FounderCompanySerializer
from integrations import http_client
from integrations.models import GoogleConnection, UserIntegration
from organizations.models import Organization
from roo.models import PointsAccount
from startup_updates.models import StartupProfile
from workflow_runs.models import ContentFactoryRun, ContentFactoryRunStep, ContentFactoryRunStatus


User = get_user_model()


def _uploaded_png(name="avatar.png", size=(32, 32)):
    from PIL import Image

    output = BytesIO()
    Image.new("RGB", size, color=(128, 64, 255)).save(output, format="PNG")
    return SimpleUploadedFile(name, output.getvalue(), content_type="image/png")


class _Response(SimpleNamespace):
    text = ""

    @property
    def content(self):
        return b"{}"

    def json(self):
        return self.payload


class _SearchConsoleExecute:
    def __init__(self, payload):
        self.payload = payload

    def execute(self):
        return self.payload


class _SearchConsoleSites:
    def __init__(self, entries):
        self.entries = entries

    def list(self):
        return _SearchConsoleExecute({"siteEntry": self.entries})


class _SearchConsoleAnalytics:
    def __init__(self):
        self.queries = []

    def query(self, siteUrl=None, body=None):
        self.queries.append({"siteUrl": siteUrl, "body": body or {}})
        dimensions = list((body or {}).get("dimensions") or [])
        if dimensions == ["date"]:
            return _SearchConsoleExecute(
                {
                    "rows": [
                        {"keys": ["2026-03-31"], "clicks": 4, "impressions": 80, "ctr": 0.05, "position": 8.5},
                        {"keys": ["2026-04-01"], "clicks": 6, "impressions": 100, "ctr": 0.06, "position": 7.4},
                    ]
                }
            )
        if dimensions == ["query"]:
            return _SearchConsoleExecute(
                {"rows": [{"keys": ["startup automation"], "clicks": 7, "impressions": 120, "ctr": 0.058, "position": 6.3}]}
            )
        if dimensions == ["page"]:
            return _SearchConsoleExecute(
                {"rows": [{"keys": ["https://acme.com/blog"], "clicks": 5, "impressions": 90, "ctr": 0.056, "position": 5.8}]}
            )
        return _SearchConsoleExecute({"rows": [{"clicks": 25, "impressions": 400, "ctr": 0.0625, "position": 7.1}]})


class _SearchConsoleService:
    def __init__(self, entries):
        self.analytics = _SearchConsoleAnalytics()
        self.entries = entries

    def sites(self):
        return _SearchConsoleSites(self.entries)

    def searchanalytics(self):
        return self.analytics


class VibeMarketingAutofillTransactionTests(TransactionTestCase):
    def setUp(self):
        self.client = APIClient()
        self.user = User.objects.create_user(
            email="founder-transaction@example.com",
            password="password",
            first_name="Founder",
            last_name="Transaction",
            role="participant",
        )
        self.profile = VibeRaisingProfile.objects.create(user=self.user, role=VibeRaisingProfile.ROLE_FOUNDER)
        self.company = VibeRaisingCompany.objects.create(
            profile=self.profile,
            name="Acme",
            domain="acme.com",
            registered=True,
        )
        self.profile.active_company = self.company
        self.profile.save(update_fields=["active_company", "updated_at"])
        PointsAccount.objects.update_or_create(
            user=self.user,
            defaults={"balance": 20, "earned_balance": 20},
        )
        self.client.force_authenticate(user=self.user)

    @override_settings(CONTENT_FACTORY_URL="https://content-factory.test", CONTENT_FACTORY_API_KEY="secret-key", IS_LOCAL_ENV=False)
    def test_autofill_dispatch_happens_after_local_transaction_commits(self):
        observed = {}

        def fake_post(url, json=None, headers=None, timeout=None):
            observed["in_atomic_block"] = connection.in_atomic_block
            return _Response(status_code=202, payload={"run_id": "autofill-run-not-atomic", "status": "queued"})

        with patch("content_factory.vibe_marketing_views.http_client.post", side_effect=fake_post):
            response = self.client.post(
                "/api/v1/vibe-marketing/autofill/",
                {
                    "companyName": "Acme",
                    "domain": "https://www.acme.com",
                    "companyLinkedInUrl": "https://linkedin.com/company/acme/",
                    "brandName": "Acme",
                    "companyContext": "",
                    "competitors": "",
                    "seedKeywords": "",
                },
                format="json",
            )

        self.assertEqual(response.status_code, 202)
        self.assertEqual(response.data["run_id"], "autofill-run-not-atomic")
        self.assertEqual(observed, {"in_atomic_block": False})


class VibeMarketingAutofillTests(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.user = User.objects.create_user(
            email="founder@example.com",
            password="password",
            first_name="Founder",
            last_name="User",
            role="participant",
        )
        self.profile = VibeRaisingProfile.objects.create(user=self.user, role=VibeRaisingProfile.ROLE_FOUNDER)
        self.company = VibeRaisingCompany.objects.create(
            profile=self.profile,
            name="Acme",
            domain="acme.com",
            registered=True,
        )
        self.profile.active_company = self.company
        self.profile.save(update_fields=["active_company", "updated_at"])
        PointsAccount.objects.update_or_create(
            user=self.user,
            defaults={"balance": 20, "earned_balance": 20},
        )
        self.client.force_authenticate(user=self.user)

    def test_bootstrap_exposes_company_avatar_url(self):
        self.company.avatar_url = "https://cdn.example.com/company-avatar.jpg"
        self.company.save(update_fields=["avatar_url", "updated_at"])

        response = self.client.get("/api/v1/vibe-marketing/bootstrap/?view=summary")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["company"]["avatarUrl"], "https://cdn.example.com/company-avatar.jpg")
        self.assertEqual(response.data["company"]["avatar_url"], "https://cdn.example.com/company-avatar.jpg")

    def test_founder_company_serializer_exposes_avatar_url(self):
        self.company.avatar_url = "https://cdn.example.com/company-avatar.jpg"
        self.company.save(update_fields=["avatar_url", "updated_at"])

        data = FounderCompanySerializer(self.company).data

        self.assertEqual(data["avatarUrl"], "https://cdn.example.com/company-avatar.jpg")
        self.assertEqual(data["avatar_url"], "https://cdn.example.com/company-avatar.jpg")

    def test_company_avatar_upload_requires_authenticated_user(self):
        client = APIClient()

        response = client.post("/api/v1/vibe-marketing/company/avatar/", {"avatar": _uploaded_png()}, format="multipart")

        self.assertEqual(response.status_code, 401)

    def test_company_avatar_upload_requires_file(self):
        response = self.client.post("/api/v1/vibe-marketing/company/avatar/", {}, format="multipart")

        self.assertEqual(response.status_code, 400)

    def test_company_avatar_upload_rejects_non_image(self):
        upload = SimpleUploadedFile("avatar.txt", b"not an image", content_type="text/plain")

        response = self.client.post("/api/v1/vibe-marketing/company/avatar/", {"avatar": upload}, format="multipart")

        self.assertEqual(response.status_code, 400)

    def test_company_avatar_upload_rejects_oversized_file(self):
        upload = SimpleUploadedFile("avatar.png", b"x" * (10 * 1024 * 1024 + 1), content_type="image/png")

        response = self.client.post("/api/v1/vibe-marketing/company/avatar/", {"avatar": upload}, format="multipart")

        self.assertEqual(response.status_code, 400)

    def test_company_avatar_upload_saves_avatar_and_returns_bootstrap(self):
        with patch("core.firebase_utils.upload_file_to_storage") as upload_file_to_storage:
            upload_file_to_storage.return_value = "https://cdn.example.com/company-avatar.jpg"
            response = self.client.post(
                "/api/v1/vibe-marketing/company/avatar/",
                {"avatar": _uploaded_png()},
                format="multipart",
            )

        self.assertEqual(response.status_code, 200)
        self.company.refresh_from_db()
        self.assertEqual(self.company.avatar_url, "https://cdn.example.com/company-avatar.jpg")
        self.assertEqual(response.data["company"]["avatarUrl"], "https://cdn.example.com/company-avatar.jpg")

    @override_settings(CONTENT_FACTORY_URL="https://content-factory.test", CONTENT_FACTORY_API_KEY="secret-key", IS_LOCAL_ENV=False)
    def test_autofill_starts_durable_run_without_persisting_generated_fields(self):
        def fake_post(url, json=None, headers=None, timeout=None):
            self.assertEqual(url, "https://content-factory.test/api/runs/autofill")
            self.assertEqual(headers["X-API-KEY"], "secret-key")
            self.assertEqual(json["domain"], "acme.com")
            self.assertEqual(json["company_linkedin_url"], "https://www.linkedin.com/company/acme")
            self.assertFalse(json["persist"])
            self.assertEqual(json["research_depth"], "deep")
            self.assertTrue(json["strict_deep_research"])
            self.assertEqual(json["min_direct_competitors"], 3)
            self.assertEqual(json["min_seed_keywords"], 8)
            self.assertEqual(json["target_seed_keywords"], 12)
            return _Response(status_code=202, payload={"run_id": "autofill-run-1", "status": "queued"})

        with patch("content_factory.vibe_marketing_views.http_client.post", side_effect=fake_post):
            response = self.client.post(
                "/api/v1/vibe-marketing/autofill/",
                {
                    "companyName": "Acme",
                    "domain": "https://www.acme.com",
                    "companyLinkedInUrl": "https://linkedin.com/company/acme/",
                    "brandName": "Acme",
                    "companyContext": "",
                    "competitors": "",
                    "seedKeywords": "",
                },
                format="json",
            )

        self.assertEqual(response.status_code, 202)
        self.assertEqual(response.data["run_id"], "autofill-run-1")
        self.assertNotIn("secret-key", str(response.data))

        organization = Organization.objects.get(domain="acme.com")
        self.assertEqual(organization.company_linkedin_url, "https://www.linkedin.com/company/acme")
        self.assertEqual(organization.competitors, [])
        self.assertEqual(organization.seed_keywords, [])

        config = OrganizationContentConfig.objects.get(organization=organization)
        self.assertEqual(config.brand_name, "Acme")
        self.assertFalse(config.company_context)

        run = ContentFactoryRun.objects.get(run_id="autofill-run-1")
        self.assertEqual(run.workflow, "startup_autofill")
        self.assertEqual(run.run_request["company_id"], str(self.company.id))
        self.assertEqual(run.run_request["organization_id"], str(organization.id))
        self.assertEqual(run.run_request["persist"], False)
        self.assertEqual(run.run_request["company_linkedin_url"], "https://www.linkedin.com/company/acme")
        self.assertEqual(run.run_request["existing_fields"]["companyContext"], "")
        self.assertEqual(run.run_request["existing_fields"]["companyLinkedInUrl"], "https://www.linkedin.com/company/acme")

    @override_settings(CONTENT_FACTORY_URL="https://content-factory.test", CONTENT_FACTORY_API_KEY="secret-key", IS_LOCAL_ENV=False)
    def test_autofill_reuses_recent_active_run_for_same_domain(self):
        organization, _created = Organization.objects.get_or_create(domain="acme.com", defaults={"name": "Acme"})
        self.company.organization = organization
        self.company.save(update_fields=["organization", "updated_at"])
        active_run = ContentFactoryRun.objects.create(
            run_id="autofill-active-studynash",
            workflow="startup_autofill",
            domain="acme.com",
            status=ContentFactoryRunStatus.RUNNING,
            current_step="research_public_web",
            run_request={"domain": "acme.com", "company_id": str(self.company.id), "organization_id": str(organization.id)},
        )

        with patch("content_factory.vibe_marketing_views.http_client.post") as post_mock:
            response = self.client.post(
                "/api/v1/vibe-marketing/autofill/",
                {
                    "companyName": "Acme",
                    "domain": "https://www.acme.com",
                    "companyLinkedInUrl": "https://linkedin.com/company/acme/",
                    "brandName": "Acme",
                    "companyContext": "",
                    "competitors": "",
                    "seedKeywords": "",
                },
                format="json",
            )

        self.assertEqual(response.status_code, 202)
        self.assertEqual(response.data["run_id"], active_run.run_id)
        self.assertEqual(response.data["runId"], active_run.run_id)
        self.assertEqual(response.data["status"], ContentFactoryRunStatus.RUNNING)
        self.assertEqual(response.data["error"], "")
        self.assertEqual(response.data["errors"], [])
        post_mock.assert_not_called()
        self.assertEqual(ContentFactoryRun.objects.filter(workflow="startup_autofill", domain="acme.com").count(), 1)

    @override_settings(CONTENT_FACTORY_URL="https://content-factory.test", CONTENT_FACTORY_API_KEY="secret-key", IS_LOCAL_ENV=False)
    def test_autofill_persists_submitted_startup_details_before_queueing(self):
        def fake_post(url, json=None, headers=None, timeout=None):
            self.assertEqual(url, "https://content-factory.test/api/runs/autofill")
            self.assertFalse(json["persist"])
            self.assertEqual(json["company_name"], "MLAI")
            self.assertEqual(json["domain"], "mlai.au")
            self.assertEqual(json["brand_name"], "MLAI")
            self.assertEqual(json["company_linkedin_url"], "https://www.linkedin.com/company/mlai-aus-inc")
            self.assertEqual(json["location"], "Melbourne, Australia")
            self.assertEqual(json["abn"], "94 807 394 137")
            self.assertEqual(json["existing_fields"]["companyContext"], "AI workflow automation for founders.")
            self.assertEqual(json["existing_fields"]["competitors"], ["buildclub.ai", "aussiefoundersclub.com"])
            self.assertEqual(json["existing_fields"]["seedKeywords"], ["ai events melbourne", "founder automation"])
            self.assertEqual(json["startup_profile"]["short_description"], "Manual founder-authored description.")
            self.assertEqual(json["startup_profile"]["problem_solved"], "Manual problem statement.")
            self.assertEqual(json["startup_profile"]["target_audience"], "Founder tools for marketing and monthly updates.")
            self.assertEqual(json["startup_profile"]["founder_names"], ["Sam Donegan"])
            self.assertEqual(json["startup_profile"]["stage"], "Seed")
            self.assertEqual(json["startup_profile"]["organization_kind"], "For-profit")
            self.assertEqual(json["existing_fields"]["profileFields"]["shortDescription"], "Manual founder-authored description.")
            self.assertEqual(json["existing_fields"]["profileFields"]["problemSolved"], "Manual problem statement.")
            self.assertEqual(json["existing_fields"]["profileFields"]["targetAudience"], "Founder tools for marketing and monthly updates.")
            self.assertEqual(json["research_depth"], "deep")
            self.assertTrue(json["strict_deep_research"])
            self.assertEqual(json["min_direct_competitors"], 3)
            self.assertEqual(json["min_seed_keywords"], 8)
            self.assertEqual(json["target_seed_keywords"], 12)
            self.assertEqual(json["min_public_sources"], 3)
            return _Response(status_code=202, payload={"run_id": "autofill-run-persist", "status": "queued"})

        with patch("content_factory.vibe_marketing_views.http_client.post", side_effect=fake_post):
            response = self.client.post(
                "/api/v1/vibe-marketing/autofill/",
                {
                    "companyName": "MLAI",
                    "domain": "https://mlai.au",
                    "companyLinkedInUrl": "https://www.linkedin.com/company/mlai-aus-inc/",
                    "location": "Melbourne, Australia",
                    "abn": "94 807 394 137",
                    "brandName": "MLAI",
                    "companyContext": "AI workflow automation for founders.",
                    "shortDescription": "Manual founder-authored description.",
                    "problemSolved": "Manual problem statement.",
                    "targetAudience": "Founder tools for marketing and monthly updates.",
                    "competitors": ["buildclub.ai", "aussiefoundersclub.com"],
                    "seedKeywords": ["ai events melbourne", "founder automation"],
                    "founderNames": ["Sam Donegan"],
                    "stage": "Seed",
                    "organizationKind": "For-profit",
                    "notes": "Founder tools for marketing and monthly updates.",
                },
                format="json",
            )

        self.assertEqual(response.status_code, 202)
        self.assertEqual(response.data["run_id"], "autofill-run-persist")

        self.company.refresh_from_db()
        self.assertEqual(self.company.name, "MLAI")
        self.assertEqual(self.company.domain, "mlai.au")
        self.assertEqual(self.company.location, "Melbourne, Australia")
        self.assertEqual(self.company.abn, "94 807 394 137")

        organization = Organization.objects.get(domain="mlai.au")
        self.assertEqual(organization.name, "MLAI")
        self.assertEqual(organization.company_linkedin_url, "https://www.linkedin.com/company/mlai-aus-inc")
        self.assertEqual(organization.competitors, ["buildclub.ai", "aussiefoundersclub.com"])
        self.assertEqual(organization.seed_keywords, ["ai events melbourne", "founder automation"])

        config = OrganizationContentConfig.objects.get(organization=organization)
        self.assertEqual(config.brand_name, "MLAI")
        self.assertEqual(config.company_context, "AI workflow automation for founders.")
        self.assertEqual(config.connected_slack_user_id, f"mlai_user:{self.user.id}")

        startup_profile = organization.startup_profile
        self.assertEqual(startup_profile.founder_names, ["Sam Donegan"])
        self.assertEqual(startup_profile.competitor_domains, ["buildclub.ai", "aussiefoundersclub.com"])
        self.assertEqual(startup_profile.positive_keywords, ["ai events melbourne", "founder automation"])
        self.assertEqual(startup_profile.stage, "Seed")
        self.assertEqual(startup_profile.organization_kind, "For-profit")
        self.assertEqual(startup_profile.short_description, "Manual founder-authored description.")
        self.assertEqual(startup_profile.problem_solved, "Manual problem statement.")
        self.assertEqual(startup_profile.target_audience, "Founder tools for marketing and monthly updates.")
        self.assertEqual(startup_profile.notes, "Founder tools for marketing and monthly updates.")

        bootstrap = self.client.get("/api/v1/vibe-marketing/bootstrap/")
        self.assertEqual(bootstrap.status_code, 200)
        self.assertEqual(bootstrap.data["company"]["name"], "MLAI")
        self.assertEqual(bootstrap.data["company"]["domain"], "mlai.au")
        self.assertEqual(bootstrap.data["company"]["companyLinkedInUrl"], "https://www.linkedin.com/company/mlai-aus-inc")
        self.assertEqual(bootstrap.data["settings"]["brandName"], "MLAI")
        self.assertEqual(bootstrap.data["settings"]["companyContext"], "AI workflow automation for founders.")
        self.assertEqual(bootstrap.data["organization"]["competitors"], ["buildclub.ai", "aussiefoundersclub.com"])
        self.assertEqual(bootstrap.data["organization"]["seedKeywords"], ["ai events melbourne", "founder automation"])
        self.assertEqual(bootstrap.data["startupProfile"]["founderNames"], ["Sam Donegan"])
        self.assertEqual(bootstrap.data["startupProfile"]["stage"], "Seed")
        self.assertEqual(bootstrap.data["startupProfile"]["organizationKind"], "For-profit")
        self.assertEqual(bootstrap.data["startupProfile"]["shortDescription"], "Manual founder-authored description.")
        self.assertEqual(bootstrap.data["startupProfile"]["problemSolved"], "Manual problem statement.")
        self.assertEqual(bootstrap.data["startupProfile"]["targetAudience"], "Founder tools for marketing and monthly updates.")
        self.assertEqual(bootstrap.data["startupProfile"]["notes"], "Founder tools for marketing and monthly updates.")

    def test_settings_save_accepts_organization_kind_without_clearing_brand_or_notes(self):
        organization = Organization.objects.create(domain="mlai.au", name="MLAI")
        self.company.organization = organization
        self.company.domain = "mlai.au"
        self.company.save(update_fields=["organization", "domain", "updated_at"])
        config = OrganizationContentConfig.objects.create(
            organization=organization,
            brand_name="Existing Brand",
            company_context="Existing context",
        )
        startup_profile = StartupProfile.objects.create(organization=organization, notes="Existing private notes")

        response = self.client.put(
            "/api/v1/vibe-marketing/settings/",
            {
                "companyName": "MLAI",
                "domain": "mlai.au",
                "location": "Melbourne, Australia",
                "abn": "94 807 394 137",
                "companyContext": "Updated context",
                "stage": "Pre-seed",
                "organizationKind": "Not-for-profit",
                "competitors": [],
                "seedKeywords": [],
            },
            format="json",
        )

        self.assertEqual(response.status_code, 200)
        config.refresh_from_db()
        startup_profile.refresh_from_db()
        self.assertEqual(config.brand_name, "Existing Brand")
        self.assertEqual(startup_profile.notes, "Existing private notes")
        self.assertEqual(startup_profile.stage, "Pre-seed")
        self.assertEqual(startup_profile.organization_kind, "Not-for-profit")
        self.assertEqual(response.data["startupProfile"]["organizationKind"], "Not-for-profit")

    @override_settings(GOOGLE_PLACES_API_KEY="")
    def test_location_lookup_missing_key_returns_empty_configured_false(self):
        response = self.client.get("/api/v1/vibe-marketing/lookups/locations/?q=Mel")

        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.data["configured"])
        self.assertEqual(response.data["suggestions"], [])

    @override_settings(GOOGLE_PLACES_API_KEY="places-key")
    def test_location_lookup_normalizes_google_city_suggestions(self):
        observed = {}

        def fake_post(url, json=None, headers=None, timeout=None):
            observed["url"] = url
            observed["headers"] = headers
            observed["json"] = json
            return _Response(
                status_code=200,
                payload={
                    "suggestions": [
                        {
                            "placePrediction": {
                                "placeId": "melbourne-place",
                                "text": {"text": "Melbourne VIC, Australia"},
                                "structuredFormat": {
                                    "mainText": {"text": "Melbourne"},
                                    "secondaryText": {"text": "VIC, Australia"},
                                },
                            }
                        }
                    ]
                },
            )

        with patch("content_factory.vibe_marketing_views.http_client.post", side_effect=fake_post):
            response = self.client.get("/api/v1/vibe-marketing/lookups/locations/?q=Mel&sessionToken=session-1")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(observed["url"], "https://places.googleapis.com/v1/places:autocomplete")
        self.assertEqual(observed["headers"]["X-Goog-Api-Key"], "places-key")
        self.assertEqual(observed["json"]["includedPrimaryTypes"], ["(cities)"])
        self.assertEqual(observed["json"]["sessionToken"], "session-1")
        self.assertTrue(response.data["configured"])
        self.assertEqual(response.data["suggestions"][0]["label"], "Melbourne VIC, Australia")
        self.assertEqual(response.data["suggestions"][0]["city"], "Melbourne")
        self.assertEqual(response.data["suggestions"][0]["placeId"], "melbourne-place")

    @override_settings(ABR_LOOKUP_AUTHENTICATION_GUID="")
    def test_abn_lookup_missing_key_returns_empty_configured_false(self):
        response = self.client.get("/api/v1/vibe-marketing/lookups/abns/?q=MLAI")

        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.data["configured"])
        self.assertEqual(response.data["suggestions"], [])

    @override_settings(ABR_LOOKUP_AUTHENTICATION_GUID="abr-guid")
    def test_abn_lookup_searches_by_name_and_normalizes_xml(self):
        observed = {}
        xml = """<?xml version="1.0" encoding="utf-8"?>
        <ABRPayloadSearchResults xmlns="http://abr.business.gov.au/ABRXMLSearch/">
          <response>
            <searchResultsList>
              <searchResultsRecord>
                <ABN><identifierValue>94807394137</identifierValue></ABN>
                <mainName><organisationName>MLAI AUS INC</organisationName></mainName>
                <mainBusinessPhysicalAddress><stateCode>VIC</stateCode><postcode>3000</postcode></mainBusinessPhysicalAddress>
                <entityStatus><entityStatusCode>Active</entityStatusCode></entityStatus>
              </searchResultsRecord>
            </searchResultsList>
          </response>
        </ABRPayloadSearchResults>"""

        def fake_get(url, params=None, timeout=None):
            observed["url"] = url
            observed["params"] = params
            return _Response(status_code=200, text=xml, payload={})

        with patch("content_factory.vibe_marketing_views.http_client.get", side_effect=fake_get):
            response = self.client.get("/api/v1/vibe-marketing/lookups/abns/?q=MLAI")

        self.assertEqual(response.status_code, 200)
        self.assertIn("ABRSearchByNameAdvancedSimpleProtocol2017", observed["url"])
        self.assertEqual(observed["params"]["name"], "MLAI")
        self.assertEqual(observed["params"]["authenticationGuid"], "abr-guid")
        self.assertEqual(response.data["suggestions"][0]["abn"], "94 807 394 137")
        self.assertEqual(response.data["suggestions"][0]["entityName"], "MLAI AUS INC")
        self.assertEqual(response.data["suggestions"][0]["state"], "VIC")

    @override_settings(ABR_LOOKUP_AUTHENTICATION_GUID="abr-guid")
    def test_abn_lookup_searches_numeric_query_by_abn(self):
        observed = {}
        xml = """<?xml version="1.0" encoding="utf-8"?>
        <ABRPayloadSearchResults xmlns="http://abr.business.gov.au/ABRXMLSearch/">
          <response>
            <businessEntity202001>
              <ABN><identifierValue>94807394137</identifierValue></ABN>
              <mainName><organisationName>MLAI AUS INC</organisationName></mainName>
            </businessEntity202001>
          </response>
        </ABRPayloadSearchResults>"""

        def fake_get(url, params=None, timeout=None):
            observed["url"] = url
            observed["params"] = params
            return _Response(status_code=200, text=xml, payload={})

        with patch("content_factory.vibe_marketing_views.http_client.get", side_effect=fake_get):
            response = self.client.get("/api/v1/vibe-marketing/lookups/abns/?q=94%20807%20394%20137")

        self.assertEqual(response.status_code, 200)
        self.assertIn("SearchByABNv202001", observed["url"])
        self.assertEqual(observed["params"]["searchString"], "94807394137")
        self.assertEqual(response.data["suggestions"][0]["abn"], "94 807 394 137")

    def test_autofill_rejects_personal_linkedin_profile_url(self):
        response = self.client.post(
            "/api/v1/vibe-marketing/autofill/",
            {
                "companyName": "Acme",
                "domain": "acme.com",
                "companyLinkedInUrl": "https://www.linkedin.com/in/founder",
            },
            format="json",
        )

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.data["field"], "companyLinkedInUrl")

    @override_settings(CONTENT_FACTORY_URL="", CONTENT_FACTORY_API_KEY="", IS_LOCAL_ENV=True)
    def test_autofill_returns_blocked_run_when_research_worker_is_unconfigured(self):
        response = self.client.post(
            "/api/v1/vibe-marketing/autofill/",
            {
                "companyName": "Acme",
                "domain": "acme.com",
            },
            format="json",
        )

        self.assertEqual(response.status_code, 202)
        self.assertEqual(response.data["status"], ContentFactoryRunStatus.BLOCKED)
        self.assertEqual(
            response.data["error"],
            "AI fill is unavailable. Check the Content Factory backend and try again.",
        )

        run = ContentFactoryRun.objects.get(run_id=response.data["run_id"])
        self.assertEqual(run.workflow, "startup_autofill")
        self.assertEqual(run.status, ContentFactoryRunStatus.BLOCKED)
        self.assertEqual(run.error, response.data["error"])
        self.assertEqual(run.result["error"], response.data["error"])
        self.assertIn("technical_error", run.result["diagnostics"])

    @override_settings(CONTENT_FACTORY_URL="https://content-factory.test", CONTENT_FACTORY_API_KEY="secret-key", IS_LOCAL_ENV=False)
    def test_autofill_run_polling_syncs_result_and_steps(self):
        organization, _created = Organization.objects.get_or_create(domain="acme.com", defaults={"name": "Acme"})
        self.company.organization = organization
        self.company.save(update_fields=["organization", "updated_at"])
        run = ContentFactoryRun.objects.create(
            run_id="autofill-run-2",
            workflow="startup_autofill",
            domain="acme.com",
            status=ContentFactoryRunStatus.QUEUED,
        )
        remote_payload = {
            "run_id": run.run_id,
            "workflow": "startup_autofill",
            "status": "completed",
            "current_step": "finalize",
            "result": {
                "autofill": {
                    "brandName": "Acme",
                    "companyLinkedInUrl": "https://www.linkedin.com/company/acme",
                    "companyContext": "## Positioning\nAcme builds workflow automation for founders.\n\n## Audience\nStartup founders and operators.\n\n## Product\nWorkflow automation for startup operations.",
                    "profileFields": {
                        "shortDescription": "Acme helps founders automate startup workflows.",
                        "problemSolved": "Startup teams lose time coordinating repeatable operating workflows manually.",
                        "targetAudience": "Startup founders and operators.",
                        "companyContext": "## Positioning\nAcme builds workflow automation for founders.",
                        "fieldConfidence": {"shortDescription": "high", "targetAudience": "high"},
                        "reviewNotes": [],
                    },
                    "offeringProfile": {
                        "coreOffering": "Workflow automation for founders.",
                        "targetUsers": "Startup founders and operators.",
                        "market": "Australia",
                        "categoryLanes": ["workflow automation", "startup operations"],
                        "excludedMeanings": ["home equity"],
                    },
                    "directCompetitors": [
                        {
                            "name": "Build Club",
                            "domain": "buildclub.ai",
                            "linkedinUrl": "https://www.linkedin.com/company/build-club-ai",
                            "type": "direct",
                            "score": 0.91,
                            "reason": "Targets founder workflow automation.",
                            "evidence": ["https://buildclub.ai", "LinkedIn public snippet"],
                            "confidence": "high",
                        }
                    ],
                    "seoCompetitors": [
                        {
                            "name": "Copy.ai",
                            "domain": "copy.ai",
                            "type": "seo",
                            "score": 0.41,
                            "reason": "Competes for broad AI workflow search demand.",
                            "evidence": ["public search snippet"],
                        }
                    ],
                    "adjacentOrganizations": [],
                    "competitors": [
                        {
                            "name": "Build Club",
                            "domain": "buildclub.ai",
                            "linkedinUrl": "https://www.linkedin.com/company/build-club-ai",
                            "type": "direct",
                            "score": 0.91,
                            "reason": "Targets founder workflow automation.",
                            "evidence": ["https://buildclub.ai", "LinkedIn public snippet"],
                            "confidence": "high",
                        }
                    ],
                    "competitorGroups": {
                        "directCompetitors": [
                            {
                                "name": "Build Club",
                                "domain": "buildclub.ai",
                                "linkedinUrl": "https://www.linkedin.com/company/build-club-ai",
                                "type": "direct",
                                "score": 0.91,
                                "reason": "Targets founder workflow automation.",
                                "evidence": ["https://buildclub.ai", "LinkedIn public snippet"],
                                "confidence": "high",
                            }
                        ],
                        "seoCompetitors": [
                            {
                                "name": "Copy.ai",
                                "domain": "copy.ai",
                                "type": "seo",
                                "score": 0.41,
                                "reason": "Competes for broad AI workflow search demand.",
                                "evidence": ["public search snippet"],
                            }
                        ],
                        "adjacentOrganizations": [],
                    },
                    "seedKeywords": [f"workflow automation {index}" for index in range(1, 21)],
                    "keywordCandidates": [
                        {
                            "keyword": "workflow automation for founders",
                            "volume": 320,
                            "difficulty": 28,
                            "source": "current_ranking",
                            "qualified": True,
                            "relevanceTier": "current-ranking",
                        },
                        {
                            "keyword": "home equity automation",
                            "volume": 9900,
                            "difficulty": 61,
                            "source": "current_ranking",
                            "qualified": False,
                            "rejectReason": "ambiguous-unrelated-meaning",
                        },
                    ],
                    "keywordGroups": [{"group": "Pain point", "keywords": ["startup workflow automation"]}],
                    "sources": [
                        {"url": "https://acme.com", "title": "Home", "type": "website"},
                        {
                            "url": "https://www.linkedin.com/company/acme",
                            "title": "Acme LinkedIn",
                            "type": "linkedin",
                            "query": "Acme LinkedIn",
                        },
                    ],
                    "linkedinProfile": {
                        "url": "https://www.linkedin.com/company/acme",
                        "title": "Acme LinkedIn",
                        "vanityName": "acme",
                        "description": "Public LinkedIn profile evidence.",
                    },
                    "linkedinSimilarSignals": [
                        {
                            "url": "https://www.linkedin.com/company/build-club-ai",
                            "title": "Build Club",
                            "type": "linkedin_similar",
                            "description": "Visible public similar-company signal.",
                        }
                    ],
                    "sourceCount": 2,
                    "competitorCount": 1,
                    "seedKeywordCount": 20,
                    "researchSummary": "Public research found direct founder-community overlap.",
                    "researchDepth": {
                        "ownedPagesCrawled": 2,
                        "publicSourcesReviewed": 8,
                        "linkedinPublicSignals": 1,
                        "linkedinSimilarSignals": 1,
                        "competitorCandidatesEvaluated": 7,
                        "competitorsReturned": 1,
                        "seedKeywordsGenerated": 20,
                    },
                    "minimumsMet": {"companyContext": True, "directCompetitors": False, "seedKeywords": True},
                    "warnings": [],
                }
            },
            "steps": {
                "resolve_company_identity": {"status": "completed", "attempts": 1, "message": "Resolved identity."},
                "crawl_owned_web": {"status": "completed", "attempts": 1, "message": "Crawled website."},
                "research_public_web": {"status": "completed", "attempts": 1, "message": "Researched public web."},
                "research_linkedin_public": {"status": "completed", "attempts": 1, "message": "Collected LinkedIn public signals."},
                "discover_competitor_candidates": {"status": "completed", "attempts": 1, "message": "Found candidates."},
                "rank_competitors": {"status": "completed", "attempts": 1, "message": "Ranked competitors."},
                "generate_keyword_landscape": {"status": "completed", "attempts": 1, "message": "Generated keywords."},
                "synthesize_company_profile": {"status": "completed", "attempts": 1, "message": "Synthesized context."},
                "finalize": {"status": "completed", "attempts": 1, "message": "Ready."},
            },
        }

        with patch(
            "content_factory.vibe_marketing_views.http_client.get",
            return_value=_Response(status_code=200, payload=remote_payload),
        ):
            response = self.client.get(f"/api/v1/vibe-marketing/runs/{run.run_id}/")
            status_response = self.client.get(f"/api/v1/vibe-marketing/runs/{run.run_id}/?view=status")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["status"], "completed")
        self.assertIn("## Positioning", response.data["result"]["autofill"]["companyContext"])
        self.assertEqual(
            response.data["result"]["autofill"]["profileFields"]["shortDescription"],
            "Acme helps founders automate startup workflows.",
        )
        self.assertEqual(response.data["result"]["autofill"]["seedKeywordCount"], 20)
        self.assertEqual(response.data["result"]["autofill"]["offeringProfile"]["excludedMeanings"], ["home equity"])
        self.assertEqual(response.data["result"]["autofill"]["keywordCandidates"][0]["keyword"], "workflow automation for founders")
        self.assertEqual(response.data["result"]["autofill"]["directCompetitors"][0]["domain"], "buildclub.ai")
        self.assertEqual(response.data["result"]["autofill"]["companyLinkedInUrl"], "https://www.linkedin.com/company/acme")
        self.assertEqual(response.data["result"]["autofill"]["researchDepth"]["linkedinSimilarSignals"], 1)
        self.assertEqual(status_response.status_code, 200)
        self.assertEqual(
            status_response.data["result"]["autofill"]["profileFields"]["shortDescription"],
            "Acme helps founders automate startup workflows.",
        )
        self.assertEqual(status_response.data["result"]["autofill"]["companyLinkedInUrl"], "https://www.linkedin.com/company/acme")
        self.assertEqual(status_response.data["result"]["autofill"]["seedKeywordCount"], 20)
        self.assertEqual(status_response.data["result"]["autofill"]["seedKeywords"][0], "workflow automation 1")

        run.refresh_from_db()
        self.assertEqual(run.status, ContentFactoryRunStatus.COMPLETED)
        self.assertTrue(ContentFactoryRunStep.objects.filter(run=run, step_key="resolve_company_identity").exists())
        self.assertTrue(ContentFactoryRunStep.objects.filter(run=run, step_key="generate_keyword_landscape").exists())

    @override_settings(CONTENT_FACTORY_URL="https://content-factory.test", CONTENT_FACTORY_API_KEY="secret-key", IS_LOCAL_ENV=False)
    def test_autofill_run_polling_preserves_partial_blocked_result(self):
        organization, _created = Organization.objects.get_or_create(domain="acme.com", defaults={"name": "Acme"})
        self.company.organization = organization
        self.company.save(update_fields=["organization", "updated_at"])
        run = ContentFactoryRun.objects.create(
            run_id="autofill-partial-1",
            workflow="startup_autofill",
            domain="acme.com",
            status=ContentFactoryRunStatus.RUNNING,
        )
        remote_payload = {
            "run_id": run.run_id,
            "workflow": "startup_autofill",
            "status": "blocked",
            "current_step": "finalize",
            "error": "Deep company research could not produce a complete company context with the required sections.",
            "result": {
                "status": "blocked",
                "error": "Deep company research could not produce a complete company context with the required sections.",
                "error_code": "COMPANY_CONTEXT_INCOMPLETE",
                "autofill": {
                    "partial": True,
                    "brandName": "Acme",
                    "companyContext": "Short context that needs review.",
                    "profileFields": {
                        "shortDescription": "Acme helps founders automate startup workflows.",
                        "problemSolved": "Startup teams lose time coordinating repeatable operating workflows manually.",
                        "targetAudience": "Startup founders and operators.",
                        "companyContext": "Short context that needs review.",
                        "fieldConfidence": {"shortDescription": "high", "problemSolved": "medium"},
                        "reviewNotes": ["Review problem wording before saving."],
                    },
                    "offeringProfile": {
                        "coreOffering": "Workflow automation for founders.",
                        "targetUsers": "Startup founders and operators.",
                        "market": "Australia",
                    },
                    "directCompetitors": [{"name": "Build Club", "domain": "buildclub.ai"}],
                    "seoCompetitors": [{"name": "Copy.ai", "domain": "copy.ai"}],
                    "adjacentOrganizations": [{"name": "LaunchVic", "domain": "launchvic.org"}],
                    "competitors": [{"name": "Build Club", "domain": "buildclub.ai"}],
                    "seedKeywords": [f"workflow automation {index}" for index in range(1, 21)],
                    "keywordCandidates": [{"keyword": "workflow automation", "qualified": True}],
                    "sourceCount": 12,
                    "competitorCount": 1,
                    "seedKeywordCount": 20,
                    "researchQuality": {"status": "partial", "errorCode": "COMPANY_CONTEXT_INCOMPLETE"},
                },
            },
            "steps": {
                "finalize": {
                    "status": "blocked",
                    "attempts": 1,
                    "message": "Profile needs review.",
                    "error": "Company context incomplete.",
                }
            },
        }

        with patch(
            "content_factory.vibe_marketing_views.http_client.get",
            return_value=_Response(status_code=200, payload=remote_payload),
        ):
            response = self.client.get(f"/api/v1/vibe-marketing/runs/{run.run_id}/")
            status_response = self.client.get(f"/api/v1/vibe-marketing/runs/{run.run_id}/?view=status")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["status"], ContentFactoryRunStatus.BLOCKED)
        self.assertTrue(response.data["result"]["autofill"]["partial"])
        self.assertEqual(response.data["result"]["autofill"]["seedKeywordCount"], 20)
        self.assertEqual(response.data["result"]["autofill"]["keywordCandidates"][0]["keyword"], "workflow automation")
        self.assertEqual(response.data["result"]["autofill"]["seoCompetitors"][0]["domain"], "copy.ai")
        self.assertEqual(response.data["result"]["autofill"]["adjacentOrganizations"][0]["domain"], "launchvic.org")
        self.assertEqual(response.data["result"]["autofill"]["directCompetitors"][0]["domain"], "buildclub.ai")
        self.assertEqual(
            response.data["result"]["autofill"]["profileFields"]["shortDescription"],
            "Acme helps founders automate startup workflows.",
        )
        self.assertEqual(response.data["result"]["autofill"]["profileFields"]["reviewNotes"][0], "Review problem wording before saving.")
        self.assertEqual(status_response.status_code, 200)
        self.assertTrue(status_response.data["result"]["autofill"]["partial"])
        self.assertEqual(
            status_response.data["result"]["autofill"]["profileFields"]["targetAudience"],
            "Startup founders and operators.",
        )
        self.assertEqual(status_response.data["result"]["autofill"]["researchQuality"]["status"], "partial")

        run.refresh_from_db()
        self.assertEqual(run.status, ContentFactoryRunStatus.BLOCKED)
        self.assertTrue(run.result["autofill"]["partial"])
        self.assertEqual(run.result["autofill"]["offeringProfile"]["targetUsers"], "Startup founders and operators.")
        self.assertEqual(run.result["autofill"]["keywordCandidates"][0]["keyword"], "workflow automation")
        self.assertEqual(run.result["autofill"]["profileFields"]["targetAudience"], "Startup founders and operators.")

    @override_settings(CONTENT_FACTORY_URL="https://content-factory.test", CONTENT_FACTORY_API_KEY="secret-key", IS_LOCAL_ENV=False)
    def test_autofill_run_polling_marks_worker_timeout_as_blocked(self):
        organization, _created = Organization.objects.get_or_create(domain="acme.com", defaults={"name": "Acme"})
        self.company.organization = organization
        self.company.save(update_fields=["organization", "updated_at"])
        run = ContentFactoryRun.objects.create(
            run_id="autofill-timeout-1",
            workflow="startup_autofill",
            domain="acme.com",
            status=ContentFactoryRunStatus.RUNNING,
            current_step="research_public_web",
        )

        with patch("content_factory.vibe_marketing_views.http_client.get", side_effect=http_client.RequestException("connect timeout")):
            response = self.client.get(f"/api/v1/vibe-marketing/runs/{run.run_id}/")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["status"], ContentFactoryRunStatus.BLOCKED)
        self.assertEqual(
            response.data["errors"][0],
            "AI fill is unavailable. Check the Content Factory backend and try again.",
        )
        self.assertIn("connect timeout", response.data["diagnostics"]["technical_error"])

        run.refresh_from_db()
        self.assertEqual(run.status, ContentFactoryRunStatus.BLOCKED)
        self.assertEqual(run.error, response.data["errors"][0])

    @override_settings(CONTENT_FACTORY_URL="https://content-factory.test", CONTENT_FACTORY_API_KEY="secret-key", IS_LOCAL_ENV=False)
    def test_run_detail_retries_remote_step_sync_on_transient_sqlite_lock(self):
        organization, _created = Organization.objects.get_or_create(domain="acme.com", defaults={"name": "Acme"})
        self.company.organization = organization
        self.company.save(update_fields=["organization", "updated_at"])
        run = ContentFactoryRun.objects.create(
            run_id="article-lock-sync-1",
            workflow="article_generation",
            domain="acme.com",
            status=ContentFactoryRunStatus.RUNNING,
        )
        remote_payload = {
            "run_id": run.run_id,
            "workflow": "article_generation",
            "status": "completed",
            "current_step": "finalize",
            "result": {"status": "success"},
            "steps": {"finalize": {"status": "completed", "attempts": 1, "message": "Done."}},
        }
        original_update_or_create = ContentFactoryRunStep.objects.update_or_create
        attempts = {"count": 0}

        def flaky_update_or_create(*args, **kwargs):
            if attempts["count"] == 0:
                attempts["count"] += 1
                raise OperationalError("database is locked")
            return original_update_or_create(*args, **kwargs)

        with patch("content_factory.vibe_marketing_views.http_client.get", return_value=_Response(status_code=200, payload=remote_payload)):
            with patch("content_factory.vibe_marketing_views.ContentFactoryRunStep.objects.update_or_create", side_effect=flaky_update_or_create):
                with patch("content_factory.vibe_marketing_views.time.sleep"):
                    response = self.client.get(f"/api/v1/vibe-marketing/runs/{run.run_id}/")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["status"], ContentFactoryRunStatus.COMPLETED)
        self.assertTrue(ContentFactoryRunStep.objects.filter(run=run, step_key="finalize").exists())

    @override_settings(CONTENT_FACTORY_URL="https://content-factory.test", CONTENT_FACTORY_API_KEY="secret-key", IS_LOCAL_ENV=False)
    def test_baseline_starts_durable_run_and_bootstrap_exposes_check(self):
        organization, _created = Organization.objects.get_or_create(domain="acme.com", defaults={"name": "Acme"})
        self.company.organization = organization
        self.company.save(update_fields=["organization", "updated_at"])
        organization.seed_keywords = ["founder workflow automation"]
        organization.competitors = ["competitor.example"]
        organization.save(update_fields=["seed_keywords", "competitors"])
        OrganizationContentConfig.objects.update_or_create(
            organization=organization,
            defaults={"brand_name": "Acme"},
        )

        def fake_post(url, json=None, headers=None, timeout=None):
            self.assertEqual(url, "https://content-factory.test/api/runs/baseline")
            self.assertEqual(json["domain"], "acme.com")
            self.assertEqual(json["seed_keywords"], ["founder workflow automation"])
            return _Response(status_code=202, payload={"run_id": "baseline-run-1", "status": "queued"})

        with patch("content_factory.vibe_marketing_views.http_client.post", side_effect=fake_post):
            response = self.client.post("/api/v1/vibe-marketing/baseline/", {}, format="json")

        self.assertEqual(response.status_code, 202)
        self.assertEqual(response.data["run_id"], "baseline-run-1")
        run = ContentFactoryRun.objects.get(run_id="baseline-run-1")
        self.assertEqual(run.workflow, "website_baseline")

        bootstrap = self.client.get("/api/v1/vibe-marketing/bootstrap/")
        self.assertEqual(bootstrap.status_code, 200)
        self.assertFalse(bootstrap.data["checks"]["baseline"]["passed"])
        self.assertEqual(bootstrap.data["recommendedNextAction"]["key"], "baseline")

    @override_settings(CONTENT_FACTORY_URL="https://content-factory.test", CONTENT_FACTORY_API_KEY="secret-key", IS_LOCAL_ENV=False)
    def test_baseline_polling_persists_snapshot(self):
        organization, _created = Organization.objects.get_or_create(domain="acme.com", defaults={"name": "Acme"})
        self.company.organization = organization
        self.company.save(update_fields=["organization", "updated_at"])
        run = ContentFactoryRun.objects.create(
            run_id="baseline-run-2",
            workflow="website_baseline",
            domain="acme.com",
            status=ContentFactoryRunStatus.QUEUED,
        )
        remote_payload = {
            "run_id": run.run_id,
            "workflow": "website_baseline",
            "status": "completed",
            "current_step": "finalize",
            "result": {
                "baseline": {
                    "domain": "acme.com",
                    "collectedAt": "2026-04-25T00:00:00+00:00",
                    "overallScore": 78,
                    "summary": "Website baseline is workable.",
                    "metrics": {
                        "technicalHealth": {"status": "measured", "score": 82},
                        "aiVisibility": {
                            "status": "measured",
                            "score": 75,
                            "providers": [
                                {"key": "chatgpt", "label": "ChatGPT", "status": "measured", "score": 80},
                                {"key": "claude", "label": "Claude", "status": "measured", "score": 70},
                            ],
                        },
                    },
                    "sourceStatus": {"technicalHealth": "measured", "aiVisibility": "measured", "traffic": "needs_connection"},
                    "recommendations": [{"title": "Connect Google", "source": "traffic"}],
                }
            },
            "steps": {
                "crawl_technical_health": {"status": "completed", "attempts": 1, "message": "Crawled."},
                "measure_lighthouse": {"status": "completed", "attempts": 1, "message": "Measured."},
                "measure_search_visibility": {"status": "completed", "attempts": 1, "message": "Measured."},
                "finalize": {"status": "completed", "attempts": 1, "message": "Ready."},
            },
        }

        with patch(
            "content_factory.vibe_marketing_views.http_client.get",
            return_value=_Response(status_code=200, payload=remote_payload),
        ):
            response = self.client.get(f"/api/v1/vibe-marketing/runs/{run.run_id}/")

        self.assertEqual(response.status_code, 200)
        snapshot = WebsiteBaselineSnapshot.objects.get(run_id="baseline-run-2")
        self.assertEqual(snapshot.overall_score, 78)
        self.assertEqual(snapshot.source_status["traffic"], "needs_connection")

        bootstrap = self.client.get("/api/v1/vibe-marketing/bootstrap/")
        self.assertTrue(bootstrap.data["checks"]["baseline"]["passed"])
        self.assertEqual(bootstrap.data["websiteBaseline"]["overallScore"], 78)
        providers = bootstrap.data["websiteBaseline"]["metrics"]["aiVisibility"]["providers"]
        self.assertEqual(providers[0]["key"], "chatgpt")
        self.assertEqual(providers[1]["score"], 70)

    def test_bootstrap_returns_topic_candidates_from_selection_options(self):
        organization, _created = Organization.objects.get_or_create(domain="acme.com", defaults={"name": "Acme"})
        self.company.organization = organization
        self.company.save(update_fields=["organization", "updated_at"])
        ContentFactoryRun.objects.create(
            run_id="discovery-selection-1",
            workflow="auto_discovery",
            domain="acme.com",
            status=ContentFactoryRunStatus.AWAITING_CONFIRMATION,
            current_step="finalize",
            result={
                "selection": {
                    "options": [
                        {
                            "id": "aus-founders-ai",
                            "keyword": "australian founders",
                            "title": "What Australian Founders Need to Know Before Investing in AI Products",
                            "reason": "Matches founder purchase intent.",
                            "volume": 120,
                            "difficulty": "medium",
                            "opportunityScore": 82,
                        }
                    ],
                    "selected": {
                        "id": "selected",
                        "keyword": "founder ai products",
                        "title": "Selected AI Product Brief",
                    },
                }
            },
        )

        response = self.client.get("/api/v1/vibe-marketing/bootstrap/")

        self.assertEqual(response.status_code, 200)
        candidates = response.data["topicCandidates"]
        self.assertGreaterEqual(len(candidates), 1)
        self.assertEqual(candidates[0]["id"], "topic:run:discovery-selection-1:australian-founders")
        self.assertEqual(candidates[0]["rawCandidateId"], "aus-founders-ai")
        self.assertEqual(candidates[0]["keyword"], "australian founders")
        self.assertEqual(candidates[0]["title"], "What Australian Founders Need to Know Before Investing in AI Products")
        self.assertEqual(candidates[0]["opportunityScore"], 82)
        self.assertEqual(candidates[0]["sourceRunId"], "discovery-selection-1")

    def test_bootstrap_preserves_content_island_metadata_from_selection_options(self):
        organization, _created = Organization.objects.get_or_create(domain="acme.com", defaults={"name": "Acme"})
        self.company.organization = organization
        self.company.save(update_fields=["organization", "updated_at"])
        ContentFactoryRun.objects.create(
            run_id="discovery-island-selection-1",
            workflow="auto_discovery",
            domain="acme.com",
            status=ContentFactoryRunStatus.AWAITING_CONFIRMATION,
            current_step="finalize",
            result={
                "content_island": {
                    "slug": "ai-growth",
                    "name": "AI Growth",
                    "keyword": "ai growth strategy",
                    "icon_key": "rocket",
                    "color_key": "blue",
                },
                "selection": {
                    "options": [
                        {
                            "id": "ai-growth-for-startups",
                            "keyword": "ai growth for startups",
                            "title": "AI Growth for Startups",
                            "volume": 120,
                            "difficulty": 24,
                            "opportunityScore": 91,
                        }
                    ]
                },
            },
        )

        response = self.client.get("/api/v1/vibe-marketing/bootstrap/")

        self.assertEqual(response.status_code, 200)
        candidate = response.data["topicCandidates"][0]
        self.assertEqual(candidate["pillarSlug"], "ai-growth")
        self.assertEqual(candidate["pillarName"], "AI Growth")
        self.assertEqual(candidate["pillarKeyword"], "ai growth strategy")
        self.assertEqual(candidate["pillarIconKey"], "rocket")
        self.assertEqual(candidate["pillarColorKey"], "blue")

    def test_bootstrap_exposes_all_strong_latest_discovery_options_with_island_visuals(self):
        organization, _created = Organization.objects.get_or_create(domain="acme.com", defaults={"name": "Acme"})
        self.company.organization = organization
        self.company.save(update_fields=["organization", "updated_at"])
        ContentFactoryRun.objects.create(
            run_id="discovery-island-selection-2",
            workflow="auto_discovery",
            domain="acme.com",
            status=ContentFactoryRunStatus.AWAITING_CONFIRMATION,
            current_step="finalize",
            run_request={
                "content_island": {
                    "slug": "ai-growth",
                    "name": "AI Growth",
                    "keyword": "ai growth strategy",
                    "icon_key": "rocket",
                    "color_key": "blue",
                }
            },
            result={
                "selection": {
                    "options": [
                        {
                            "id": "ai-detectors",
                            "keyword": "how do ai detectors work",
                            "title": "How AI Detectors Work",
                            "volume": 720,
                            "difficulty": 18,
                            "opportunityScore": 4516,
                        },
                        {
                            "id": "startup-company",
                            "keyword": "what is startup company",
                            "title": "What Is a Startup Company?",
                            "volume": 320,
                            "difficulty": 25,
                            "opportunityScore": 768,
                        },
                        {
                            "id": "hard-topic",
                            "keyword": "competitive ai startup marketing",
                            "title": "Competitive AI Startup Marketing",
                            "volume": 900,
                            "difficulty": 72,
                            "opportunityScore": 1200,
                        },
                        {
                            "id": "weak-topic",
                            "keyword": "tiny ai startup idea",
                            "title": "Tiny AI Startup Idea",
                            "volume": 20,
                            "difficulty": 20,
                            "opportunityScore": 100,
                        },
                    ]
                },
            },
        )

        response = self.client.get("/api/v1/vibe-marketing/bootstrap/")

        self.assertEqual(response.status_code, 200)
        candidates = {
            candidate["keyword"]: candidate
            for candidate in response.data["topicCandidates"]
        }
        self.assertIn("how do ai detectors work", candidates)
        self.assertIn("what is startup company", candidates)
        self.assertNotIn("competitive ai startup marketing", candidates)
        self.assertNotIn("tiny ai startup idea", candidates)
        for candidate in candidates.values():
            self.assertEqual(candidate["sourceRunId"], "discovery-island-selection-2")
            self.assertEqual(candidate["pillarSlug"], "ai-growth")
            self.assertEqual(candidate["pillarIconKey"], "rocket")
            self.assertEqual(candidate["pillarColorKey"], "blue")

    def test_bootstrap_accumulates_topic_candidates_across_content_islands(self):
        organization, _created = Organization.objects.get_or_create(domain="acme.com", defaults={"name": "Acme"})
        self.company.organization = organization
        self.company.save(update_fields=["organization", "updated_at"])
        older_run = ContentFactoryRun.objects.create(
            run_id="discovery-green-island",
            workflow="auto_discovery",
            domain="acme.com",
            status=ContentFactoryRunStatus.AWAITING_CONFIRMATION,
            current_step="finalize",
            run_request={
                "content_island": {
                    "slug": "learning-ai",
                    "name": "Learning AI",
                    "keyword": "learning ai",
                    "icon_key": "brain",
                    "color_key": "green",
                }
            },
            result={
                "selection": {
                    "options": [
                        {"id": "green-1", "keyword": "ai learning path", "title": "AI Learning Path", "volume": 600, "difficulty": 12, "opportunityScore": 9200},
                        {"id": "green-2", "keyword": "machine learning beginner", "title": "Machine Learning Beginner", "volume": 420, "difficulty": 20, "opportunityScore": 7100},
                        {"id": "green-3", "keyword": "ai workshops australia", "title": "AI Workshops Australia", "volume": 250, "difficulty": 18, "opportunityScore": 5600},
                    ]
                },
            },
        )
        newer_run = ContentFactoryRun.objects.create(
            run_id="discovery-orange-island",
            workflow="auto_discovery",
            domain="acme.com",
            status=ContentFactoryRunStatus.AWAITING_CONFIRMATION,
            current_step="finalize",
            run_request={
                "content_island": {
                    "slug": "startup-fundraising",
                    "name": "Startup Fundraising",
                    "keyword": "startup fundraising",
                    "icon_key": "tools",
                    "color_key": "orange",
                }
            },
            result={
                "selection": {
                    "options": [
                        {"id": "orange-1", "keyword": "tech central", "title": "Tech Central", "volume": 1000, "difficulty": 3, "opportunityScore": 10000},
                        {"id": "orange-2", "keyword": "startup pitch updates", "title": "Startup Pitch Updates", "volume": 520, "difficulty": 14, "opportunityScore": 7800},
                        {"id": "orange-3", "keyword": "investor update template", "title": "Investor Update Template", "volume": 300, "difficulty": 16, "opportunityScore": 6100},
                    ]
                },
            },
        )
        ContentFactoryRun.objects.filter(run_id=older_run.run_id).update(updated_at=timezone.now() - timedelta(minutes=10))
        ContentFactoryRun.objects.filter(run_id=newer_run.run_id).update(updated_at=timezone.now())

        response = self.client.get("/api/v1/vibe-marketing/bootstrap/?view=summary")

        self.assertEqual(response.status_code, 200)
        candidates = response.data["topicCandidates"]
        colors = [candidate["pillarColorKey"] for candidate in candidates if candidate.get("pillarColorKey")]
        self.assertIn("green", colors)
        self.assertIn("orange", colors)
        self.assertEqual(colors[:4], ["orange", "green", "orange", "green"])
        self.assertEqual({candidate["sourceRunId"] for candidate in candidates if candidate.get("pillarColorKey")}, {
            "discovery-green-island",
            "discovery-orange-island",
        })

    def test_bootstrap_uses_dedicated_recent_discovery_runs_for_topic_candidates(self):
        organization, _created = Organization.objects.get_or_create(domain="acme.com", defaults={"name": "Acme"})
        self.company.organization = organization
        self.company.save(update_fields=["organization", "updated_at"])
        discovery_run = ContentFactoryRun.objects.create(
            run_id="discovery-outside-latest-runs",
            workflow="auto_discovery",
            domain="acme.com",
            status=ContentFactoryRunStatus.AWAITING_CONFIRMATION,
            current_step="finalize",
            result={
                "selection": {
                    "options": [
                        {
                            "id": "older-discovery-topic",
                            "keyword": "older discovery topic",
                            "title": "Older Discovery Topic",
                            "volume": 500,
                            "difficulty": 18,
                            "opportunityScore": 8400,
                        }
                    ]
                }
            },
        )
        ContentFactoryRun.objects.filter(run_id=discovery_run.run_id).update(updated_at=timezone.now() - timedelta(hours=1))
        for index in range(7):
            run = ContentFactoryRun.objects.create(
                run_id=f"newer-article-run-{index}",
                workflow="article_generation",
                domain="acme.com",
                status=ContentFactoryRunStatus.COMPLETED,
                current_step="finalize",
                result={"title": f"Article {index}"},
            )
            ContentFactoryRun.objects.filter(run_id=run.run_id).update(updated_at=timezone.now() + timedelta(minutes=index))

        response = self.client.get("/api/v1/vibe-marketing/bootstrap/?view=summary")

        self.assertEqual(response.status_code, 200)
        keywords = [candidate["keyword"] for candidate in response.data["topicCandidates"]]
        self.assertIn("older discovery topic", keywords)

    def test_duplicate_keyword_merge_preserves_island_metadata_and_fills_stored_metrics(self):
        organization, _created = Organization.objects.get_or_create(domain="acme.com", defaults={"name": "Acme"})
        self.company.organization = organization
        self.company.save(update_fields=["organization", "updated_at"])
        ContentFactoryRun.objects.create(
            run_id="discovery-green-duplicate",
            workflow="auto_discovery",
            domain="acme.com",
            status=ContentFactoryRunStatus.AWAITING_CONFIRMATION,
            current_step="finalize",
            run_request={
                "content_island": {
                    "slug": "learning-ai",
                    "name": "Learning AI",
                    "keyword": "learning ai",
                    "icon_key": "brain",
                    "color_key": "green",
                }
            },
            result={
                "selection": {
                    "options": [
                        {
                            "id": "shared-topic",
                            "keyword": "shared ai startup topic",
                            "title": "Shared AI Startup Topic",
                            "volume": 120,
                            "difficulty": 30,
                        }
                    ]
                }
            },
        )
        ResearchedKeyword.objects.create(
            organization=organization,
            keyword="shared ai startup topic",
            volume=880,
            difficulty=12,
            difficulty_source="dataforseo_labs",
            opportunity_index=9900,
            status=KeywordStatus.PENDING,
        )

        response = self.client.get("/api/v1/vibe-marketing/bootstrap/?view=summary")

        self.assertEqual(response.status_code, 200)
        candidate = next(candidate for candidate in response.data["topicCandidates"] if candidate["keyword"] == "shared ai startup topic")
        self.assertEqual(candidate["sourceRunId"], "discovery-green-duplicate")
        self.assertEqual(candidate["pillarSlug"], "learning-ai")
        self.assertEqual(candidate["pillarIconKey"], "brain")
        self.assertEqual(candidate["pillarColorKey"], "green")
        self.assertEqual(candidate["volume"], 880)
        self.assertEqual(candidate["difficulty"], 12)
        self.assertEqual(candidate["difficultySource"], "dataforseo_labs")
        self.assertEqual(candidate["opportunityScore"], 9900)

    @override_settings(CONTENT_FACTORY_URL="https://content-factory.test", CONTENT_FACTORY_API_KEY="secret-key", IS_LOCAL_ENV=False)
    def test_scoped_discovery_forwards_content_island_metadata(self):
        organization, _created = Organization.objects.get_or_create(domain="acme.com", defaults={"name": "Acme"})
        self.company.organization = organization
        self.company.save(update_fields=["organization", "updated_at"])
        OrganizationContentConfig.objects.get_or_create(organization=organization)
        captured = {}

        def fake_post(url, json=None, headers=None, timeout=None):
            captured["url"] = url
            captured["json"] = json
            return _Response(status_code=202, payload={"run_id": "discovery-island-run-1", "status": "queued"})

        with patch.object(http_client, "post", side_effect=fake_post):
            response = self.client.post(
                "/api/v1/vibe-marketing/discovery/",
                {
                    "clientRequestId": "vibe-topic-generation-ai-growth-1",
                    "contentIslandSlug": "ai-growth",
                    "contentIslandName": "AI Growth",
                    "contentIslandKeyword": "ai growth strategy",
                    "contentIslandIconKey": "rocket",
                    "contentIslandColorKey": "blue",
                    "requestedTopicCount": 4,
                },
                format="json",
            )

        self.assertEqual(response.status_code, 202)
        self.assertEqual(captured["url"], "https://content-factory.test/api/runs/discovery")
        self.assertEqual(captured["json"]["content_island_slug"], "ai-growth")
        self.assertEqual(captured["json"]["content_island_name"], "AI Growth")
        self.assertEqual(captured["json"]["content_island_keyword"], "ai growth strategy")
        self.assertEqual(captured["json"]["content_island_icon_key"], "rocket")
        self.assertEqual(captured["json"]["content_island_color_key"], "blue")
        self.assertEqual(captured["json"]["requested_topic_count"], 4)
        self.assertEqual(captured["json"]["client_request_id"], "vibe-topic-generation-ai-growth-1")
        self.assertEqual(captured["json"]["roo_points_action"], "content_island_topic_generation")
        self.assertEqual(captured["json"]["roo_points_cost"], 1)
        self.assertEqual(captured["json"]["roo_points_required"], 1)
        self.assertEqual(captured["json"]["roo_points_billing_status"], "charged")
        self.assertTrue(captured["json"]["roo_points_ledger_id"])
        self.user.points_account.refresh_from_db()
        self.assertEqual(self.user.points_account.balance, 19)

    @override_settings(CONTENT_FACTORY_URL="https://content-factory.test", CONTENT_FACTORY_API_KEY="secret-key", IS_LOCAL_ENV=False)
    def test_scoped_discovery_returns_service_unavailable_when_dispatch_fails(self):
        organization, _created = Organization.objects.get_or_create(domain="acme.com", defaults={"name": "Acme"})
        self.company.organization = organization
        self.company.save(update_fields=["organization", "updated_at"])
        OrganizationContentConfig.objects.get_or_create(organization=organization)

        with patch.object(http_client, "post", side_effect=http_client.RequestException("connection refused")):
            response = self.client.post(
                "/api/v1/vibe-marketing/discovery/",
                {
                    "clientRequestId": "vibe-topic-generation-ai-growth-fail",
                    "contentIslandSlug": "ai-growth",
                    "contentIslandName": "AI Growth",
                    "contentIslandKeyword": "ai growth strategy",
                    "contentIslandIconKey": "rocket",
                    "contentIslandColorKey": "blue",
                    "requestedTopicCount": 4,
                },
                format="json",
            )

        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.data["status"], ContentFactoryRunStatus.BLOCKED)
        self.assertIn("Content Factory worker is unavailable", response.data["errors"][0])
        self.assertTrue(response.data["retryable"])
        self.assertIn("connection refused", response.data["diagnostics"]["technical_error"])

        run = ContentFactoryRun.objects.get(run_id=response.data["run_id"])
        self.assertEqual(run.workflow, "auto_discovery")
        self.assertEqual(run.status, ContentFactoryRunStatus.BLOCKED)
        self.assertEqual(run.run_request["content_island_slug"], "ai-growth")
        self.user.points_account.refresh_from_db()
        self.assertEqual(self.user.points_account.balance, 20)

    def test_bootstrap_returns_first_article_mode_without_domain(self):
        self.company.domain = ""
        self.company.organization = None
        self.company.save(update_fields=["domain", "organization", "updated_at"])

        response = self.client.get("/api/v1/vibe-marketing/bootstrap/")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["startPageMode"], "first_article_setup")
        self.assertFalse(response.data["hasCompletedArticleFlow"])

    def test_bootstrap_returns_first_article_mode_before_first_article(self):
        organization, _created = Organization.objects.get_or_create(domain="acme.com", defaults={"name": "Acme"})
        self.company.organization = organization
        self.company.save(update_fields=["organization", "updated_at"])

        response = self.client.get("/api/v1/vibe-marketing/bootstrap/")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["startPageMode"], "first_article_setup")
        self.assertFalse(response.data["hasCompletedArticleFlow"])

    def test_bootstrap_returns_effective_review_draft_for_connected_legacy_content_only_config(self):
        organization, _created = Organization.objects.get_or_create(domain="acme.com", defaults={"name": "Acme"})
        self.company.organization = organization
        self.company.save(update_fields=["organization", "updated_at"])
        OrganizationContentConfig.objects.update_or_create(
            organization=organization,
            defaults={
                "github_repo": "acme/site",
                "github_token_encrypted": "token",
                "github_token_expires_at": timezone.now() + timezone.timedelta(hours=1),
                "article_delivery_mode": "content_only",
                "publish_targets": [{"id": "react_article_system", "state": "ready"}],
            },
        )

        response = self.client.get("/api/v1/vibe-marketing/bootstrap/")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["settings"]["articleDeliveryMode"], "content_only")
        self.assertEqual(response.data["settings"]["articleDeliveryModeEffective"], "review_draft")

    def test_bootstrap_returns_topic_picker_mode_after_written_article(self):
        organization, _created = Organization.objects.get_or_create(domain="acme.com", defaults={"name": "Acme"})
        self.company.organization = organization
        self.company.save(update_fields=["organization", "updated_at"])
        WrittenArticle.objects.create(
            organization=organization,
            title="Founder SEO Automation",
            slug="founder-seo-automation",
            category="featured",
            primary_keyword="founder seo automation",
            article_url="https://acme.com/articles/founder-seo-automation",
        )

        response = self.client.get("/api/v1/vibe-marketing/bootstrap/")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["startPageMode"], "topic_picker")
        self.assertTrue(response.data["hasCompletedArticleFlow"])
        self.assertEqual(response.data["writtenTopics"][0]["keyword"], "founder seo automation")

    def test_bootstrap_merges_stored_keywords_and_excludes_unavailable_topics(self):
        organization, _created = Organization.objects.get_or_create(domain="acme.com", defaults={"name": "Acme"})
        self.company.organization = organization
        self.company.save(update_fields=["organization", "updated_at"])
        ResearchedKeyword.objects.create(
            organization=organization,
            keyword="best crm for ai startups",
            volume=900,
            difficulty=18,
            opportunity_index=91,
            status=KeywordStatus.PENDING,
        )
        ResearchedKeyword.objects.create(
            organization=organization,
            keyword="startup launch checklist",
            volume=1500,
            difficulty=44,
            opportunity_index=70,
            status=KeywordStatus.IN_PROGRESS,
        )
        ResearchedKeyword.objects.create(
            organization=organization,
            keyword="old written topic",
            volume=500,
            difficulty=20,
            opportunity_index=88,
            status=KeywordStatus.WRITTEN,
        )
        ResearchedKeyword.objects.create(
            organization=organization,
            keyword="cooldown topic",
            volume=700,
            difficulty=22,
            opportunity_index=86,
            status=KeywordStatus.PENDING,
            cooldown_until=timezone.now() + timedelta(days=3),
        )
        ResearchedKeyword.objects.create(
            organization=organization,
            keyword="how to calculate equity in a house",
            volume=1200,
            difficulty=25,
            opportunity_index=95,
            status=KeywordStatus.PENDING,
        )
        TopicFeedback.objects.create(
            organization=organization,
            keyword="how to calculate equity in a house",
            feedback_type="declined",
            reason_code="not_appropriate",
            decline_scope="similar",
            source="homepage_topic_card",
        )

        response = self.client.get("/api/v1/vibe-marketing/bootstrap/")

        self.assertEqual(response.status_code, 200)
        keywords = [candidate["keyword"] for candidate in response.data["topicCandidates"]]
        self.assertEqual(keywords, ["best crm for ai startups"])
        self.assertEqual(response.data["topicCandidates"][0]["source"], "researched_keyword")
        self.assertEqual(response.data["topicCandidates"][0]["opportunityScore"], 91)
        self.assertEqual(response.data["topicCandidates"][0]["relatedKeywords"], [])
        self.assertEqual(response.data["topicCandidates"][0]["paaQuestions"], [])
        self.assertEqual(response.data["declinedTopicFeedback"][0]["keyword"], "how to calculate equity in a house")

    def test_bootstrap_returns_rich_topic_selection_fields_for_stored_keywords(self):
        organization, _created = Organization.objects.get_or_create(domain="acme.com", defaults={"name": "Acme"})
        self.company.organization = organization
        self.company.save(update_fields=["organization", "updated_at"])
        keyword = ResearchedKeyword.objects.create(
            organization=organization,
            keyword="best crm for ai startups",
            volume=900,
            difficulty=18,
            opportunity_index=91,
            status=KeywordStatus.PENDING,
            related_keywords=["startup crm tools"],
            monthly_searches=[400, 520, 610, 760, 920],
        )
        related_keyword = ResearchedKeyword.objects.create(
            organization=organization,
            keyword="startup crm comparison",
            volume=700,
            difficulty=24,
            opportunity_index=80,
            status=KeywordStatus.PENDING,
        )
        cluster = SemanticCluster.objects.create(
            organization=organization,
            cluster_id=1,
            pillar_keyword="best crm for ai startups",
            total_volume=1600,
        )
        ClusterMembership.objects.create(keyword=keyword, cluster=cluster, is_pillar=True, similarity_score=1)
        ClusterMembership.objects.create(keyword=related_keyword, cluster=cluster, similarity_score=0.91)
        KeywordVelocity.objects.create(
            keyword=keyword,
            absolute_volume=920,
            velocity_score=0.28,
            trend_status="rising",
            daily_volumes=[400, 520, 610, 760, 920],
        )
        AISaturation.objects.create(
            keyword=keyword,
            domain="acme.com",
            ai_overview_present=True,
            ai_overview_quality="partial",
            featured_snippet_present=True,
            saturation_score=0.3,
            hostility_score=0.2,
            hostility_recommendation="high_priority",
            serp_features=["featured_snippet"],
        )
        PAQuestion.objects.create(
            keyword=keyword,
            domain="acme.com",
            question="What CRM should an AI startup use?",
            answer_snippet="A practical CRM depends on sales motion and founder capacity.",
            depth=1,
            order=0,
            has_ai_overview=True,
        )

        response = self.client.get("/api/v1/vibe-marketing/bootstrap/")

        self.assertEqual(response.status_code, 200)
        candidate = response.data["topicCandidates"][0]
        self.assertEqual(candidate["keyword"], "best crm for ai startups")
        self.assertEqual(candidate["velocity"]["dailyVolumes"], [400, 520, 610, 760, 920])
        self.assertEqual(candidate["velocity"]["trendStatus"], "rising")
        self.assertEqual(candidate["monthlySearches"], [400, 520, 610, 760, 920])
        self.assertEqual(candidate["trendStatus"], "rising")
        self.assertEqual(candidate["trendPercent"], 28)
        self.assertEqual(candidate["relatedKeywords"], ["startup crm tools", "startup crm comparison"])
        self.assertEqual(candidate["paaQuestions"][0]["question"], "What CRM should an AI startup use?")
        self.assertTrue(candidate["paaQuestions"][0]["hasAiOverview"])
        self.assertTrue(candidate["aiSaturation"]["aiOverviewPresent"])
        self.assertEqual(candidate["aiSaturation"]["serpFeatures"], ["featured_snippet"])
        self.assertEqual(candidate["pillarSlug"], "best-crm-for-ai-startups")
        self.assertEqual(candidate["pillarName"], "best crm for ai startups")
        self.assertEqual(candidate["pillarKeyword"], "best crm for ai startups")

    def test_bootstrap_returns_topic_pillars_from_semantic_clusters(self):
        organization, _created = Organization.objects.get_or_create(domain="acme.com", defaults={"name": "Acme"})
        self.company.organization = organization
        self.company.save(update_fields=["organization", "updated_at"])
        OrganizationContentConfig.objects.update_or_create(
            organization=organization,
            defaults={
                "brand_name": "Acme",
                "pillar_strategy": {
                    "pillars": [
                        {
                            "name": "AI Growth Systems",
                            "slug": "ai-growth-systems",
                            "keyword": "ai startup growth",
                            "description": "Growth strategy, automation, and startup execution ideas.",
                        }
                    ]
                },
            },
        )
        cluster = SemanticCluster.objects.create(
            organization=organization,
            cluster_id=7,
            pillar_keyword="ai startup growth",
            total_volume=5000,
        )
        available_one = ResearchedKeyword.objects.create(
            organization=organization,
            keyword="ai startup growth playbook",
            volume=1200,
            difficulty=22,
            opportunity_index=92,
            status=KeywordStatus.PENDING,
        )
        available_two = ResearchedKeyword.objects.create(
            organization=organization,
            keyword="machine learning startup strategy",
            volume=900,
            difficulty=28,
            opportunity_index=84,
            status=KeywordStatus.PENDING,
        )
        unavailable = ResearchedKeyword.objects.create(
            organization=organization,
            keyword="ai startup launch checklist",
            volume=700,
            difficulty=30,
            opportunity_index=80,
            status=KeywordStatus.IN_PROGRESS,
        )
        declined = ResearchedKeyword.objects.create(
            organization=organization,
            keyword="ai startup funding",
            volume=650,
            difficulty=35,
            opportunity_index=79,
            status=KeywordStatus.PENDING,
        )
        ClusterMembership.objects.create(keyword=available_one, cluster=cluster, is_pillar=True, similarity_score=1)
        ClusterMembership.objects.create(keyword=available_two, cluster=cluster, similarity_score=0.94)
        ClusterMembership.objects.create(keyword=unavailable, cluster=cluster, similarity_score=0.89)
        ClusterMembership.objects.create(keyword=declined, cluster=cluster, similarity_score=0.85)
        TopicFeedback.objects.create(
            organization=organization,
            keyword="ai startup funding",
            feedback_type="declined",
            reason_code="not_appropriate",
            decline_scope="similar",
            source="homepage_topic_card",
        )

        response = self.client.get("/api/v1/vibe-marketing/bootstrap/")

        self.assertEqual(response.status_code, 200)
        pillars = response.data["topicPillars"]
        self.assertEqual(len(pillars), 1)
        pillar = pillars[0]
        self.assertEqual(pillar["source"], "semantic_cluster")
        self.assertEqual(pillar["slug"], "ai-growth-systems")
        self.assertEqual(pillar["name"], "AI Growth Systems")
        self.assertEqual(pillar["ideaCount"], 2)
        self.assertEqual(pillar["iconKey"], "brain")
        self.assertEqual(pillar["colorKey"], "green")
        self.assertEqual(
            [candidate["keyword"] for candidate in pillar["topicCandidates"]],
            ["ai startup growth playbook", "machine learning startup strategy"],
        )
        self.assertEqual(pillar["topicCandidates"][0]["pillarSlug"], "ai-growth-systems")
        self.assertEqual(pillar["topicCandidates"][0]["pillarName"], "AI Growth Systems")
        self.assertEqual(pillar["topicCandidates"][0]["pillarKeyword"], "ai startup growth")

    def test_bootstrap_falls_back_to_pillar_strategy_without_clusters(self):
        organization, _created = Organization.objects.get_or_create(domain="acme.com", defaults={"name": "Acme"})
        self.company.organization = organization
        self.company.save(update_fields=["organization", "updated_at"])
        OrganizationContentConfig.objects.update_or_create(
            organization=organization,
            defaults={
                "brand_name": "Acme",
                "pillar_strategy": {
                    "pillars": [
                        {
                            "name": "Machine Learning Fundamentals",
                            "slug": "machine-learning-fundamentals",
                            "description": "Educational content about ML concepts, algorithms, and techniques.",
                            "topics": ["what is supervised learning", "machine learning examples for founders"],
                        }
                    ]
                },
            },
        )

        response = self.client.get("/api/v1/vibe-marketing/bootstrap/")

        self.assertEqual(response.status_code, 200)
        pillars = response.data["topicPillars"]
        self.assertEqual(len(pillars), 1)
        pillar = pillars[0]
        self.assertEqual(pillar["source"], "pillar_strategy")
        self.assertEqual(pillar["slug"], "machine-learning-fundamentals")
        self.assertEqual(pillar["ideaCount"], 2)
        self.assertEqual(pillar["topicCandidates"][0]["keyword"], "what is supervised learning")
        self.assertEqual(pillar["topicCandidates"][0]["pillarSlug"], "machine-learning-fundamentals")

    def test_bootstrap_excludes_close_variants_of_written_topics(self):
        organization, _created = Organization.objects.get_or_create(domain="acme.com", defaults={"name": "Acme"})
        self.company.organization = organization
        self.company.save(update_fields=["organization", "updated_at"])
        WrittenArticle.objects.create(
            organization=organization,
            title="What Is Artificial Intelligence With Example",
            slug="what-is-artificial-intelligence-with-example",
            category="featured",
            primary_keyword="what is artificial intelligence with example",
        )
        ResearchedKeyword.objects.create(
            organization=organization,
            keyword="what artificial intelligence is",
            volume=1000,
            difficulty=30,
            opportunity_index=95,
            status=KeywordStatus.PENDING,
        )
        ResearchedKeyword.objects.create(
            organization=organization,
            keyword="how does artificial intelligence works",
            volume=900,
            difficulty=30,
            opportunity_index=94,
            status=KeywordStatus.PENDING,
        )
        ResearchedKeyword.objects.create(
            organization=organization,
            keyword="artificial intelligence for startups",
            volume=700,
            difficulty=36,
            opportunity_index=91,
            status=KeywordStatus.PENDING,
        )

        response = self.client.get("/api/v1/vibe-marketing/bootstrap/")

        self.assertEqual(response.status_code, 200)
        keywords = [candidate["keyword"] for candidate in response.data["topicCandidates"]]
        self.assertEqual(keywords, ["artificial intelligence for startups"])
        hidden = {
            candidate["keyword"]: candidate
            for candidate in response.data["hiddenTopicCandidates"]
        }
        self.assertTrue(hidden["what artificial intelligence is"]["alreadyWritten"])
        self.assertEqual(hidden["what artificial intelligence is"]["coveredTopic"]["matchType"], "lexical_variant")

    def test_topic_feedback_endpoint_declines_and_restores_topic(self):
        organization, _created = Organization.objects.get_or_create(domain="acme.com", defaults={"name": "Acme"})
        self.company.organization = organization
        self.company.save(update_fields=["organization", "updated_at"])

        response = self.client.post(
            "/api/v1/vibe-marketing/topic-feedback/",
            {
                "keyword": "how to calculate equity in a house",
                "sessionId": "discovery-selection-1",
                "feedbackType": "declined",
                "reasonCode": "not_appropriate",
                "declineScope": "similar",
                "source": "homepage_topic_card",
            },
            format="json",
        )

        self.assertEqual(response.status_code, 201)
        feedback = TopicFeedback.objects.get(organization=organization)
        self.assertEqual(feedback.keyword_normalized, "how to calculate equity in a house")
        self.assertEqual(feedback.session_id, "discovery-selection-1")
        self.assertEqual(feedback.reason_code, "not_appropriate")
        self.assertIsNone(feedback.restored_at)

        duplicate = self.client.post(
            "/api/v1/vibe-marketing/topic-feedback/",
            {"keyword": "How   To Calculate Equity In A House", "reasonCode": "off_topic"},
            format="json",
        )
        self.assertEqual(duplicate.status_code, 200)
        self.assertEqual(TopicFeedback.objects.filter(organization=organization, restored_at__isnull=True).count(), 1)
        feedback.refresh_from_db()
        self.assertEqual(feedback.reason_code, "off_topic")

        list_response = self.client.get("/api/v1/vibe-marketing/topic-feedback/")
        self.assertEqual(list_response.status_code, 200)
        self.assertEqual(list_response.data["count"], 1)

        restore_response = self.client.post(f"/api/v1/vibe-marketing/topic-feedback/{feedback.id}/restore/")
        self.assertEqual(restore_response.status_code, 200)
        feedback.refresh_from_db()
        self.assertIsNotNone(feedback.restored_at)

    def test_article_start_rejects_duplicate_written_topic(self):
        organization, _created = Organization.objects.get_or_create(domain="acme.com", defaults={"name": "Acme"})
        self.company.organization = organization
        self.company.save(update_fields=["organization", "updated_at"])
        config, _created = OrganizationContentConfig.objects.get_or_create(organization=organization)
        config.github_repo = "acme/site"
        config.baseline_skipped_at = timezone.now()
        config.save(update_fields=["github_repo", "baseline_skipped_at", "updated_at"])
        WrittenArticle.objects.create(
            organization=organization,
            title="Founder SEO Automation",
            slug="founder-seo-automation",
            category="featured",
            primary_keyword="founder seo automation",
        )

        with patch("content_factory.vibe_marketing_views.http_client.post") as post:
            response = self.client.post(
                "/api/v1/vibe-marketing/article/",
                {"topic": "Founder SEO Automation", "targetKeyword": "founder seo automation"},
                format="json",
            )

        self.assertEqual(response.status_code, 400)
        self.assertIn("already been written", response.data["detail"])
        post.assert_not_called()

    def test_article_start_rejects_close_variant_of_written_topic(self):
        organization, _created = Organization.objects.get_or_create(domain="acme.com", defaults={"name": "Acme"})
        self.company.organization = organization
        self.company.save(update_fields=["organization", "updated_at"])
        config, _created = OrganizationContentConfig.objects.get_or_create(organization=organization)
        config.github_repo = "acme/site"
        config.baseline_skipped_at = timezone.now()
        config.save(update_fields=["github_repo", "baseline_skipped_at", "updated_at"])
        WrittenArticle.objects.create(
            organization=organization,
            title="What Is Artificial Intelligence With Example",
            slug="what-is-artificial-intelligence-with-example",
            category="featured",
            primary_keyword="what is artificial intelligence with example",
        )

        with patch("content_factory.vibe_marketing_views.http_client.post") as post:
            response = self.client.post(
                "/api/v1/vibe-marketing/article/",
                {"topic": "What Artificial Intelligence Is", "targetKeyword": "what artificial intelligence is"},
                format="json",
            )

        self.assertEqual(response.status_code, 400)
        self.assertIn("already been written", response.data["detail"])
        self.assertEqual(response.data["coveredTopic"]["matchType"], "lexical_variant")
        self.assertEqual(response.data["writtenArticle"]["keyword"], "what is artificial intelligence with example")
        post.assert_not_called()

    @override_settings(CONTENT_FACTORY_URL="https://content-factory.test", CONTENT_FACTORY_API_KEY="secret-key", IS_LOCAL_ENV=False)
    def test_article_start_maps_selected_candidate_payload_to_content_factory_contract(self):
        organization, _created = Organization.objects.get_or_create(domain="acme.com", defaults={"name": "Acme"})
        self.company.organization = organization
        self.company.save(update_fields=["organization", "updated_at"])
        config, _created = OrganizationContentConfig.objects.get_or_create(organization=organization)
        config.github_repo = "acme/site"
        config.baseline_skipped_at = timezone.now()
        config.save(update_fields=["github_repo", "baseline_skipped_at", "updated_at"])
        ContentFactoryRun.objects.create(
            run_id="discovery-selection-1",
            workflow="auto_discovery",
            domain="acme.com",
            status=ContentFactoryRunStatus.AWAITING_CONFIRMATION,
            current_step="finalize",
            result={
                "selection": {
                    "options": [
                        {
                            "id": "aus-founders-ai",
                            "keyword": "australian founders",
                            "title": "What Australian Founders Need to Know Before Investing in AI Products",
                            "reason": "Matches founder purchase intent.",
                            "volume": 120,
                            "difficulty": "medium",
                            "opportunityScore": 82,
                        }
                    ],
                }
            },
        )
        captured = {}

        def fake_post(url, json=None, headers=None, timeout=None):
            captured.update(json or {})
            return _Response(status_code=202, payload={"run_id": "article-selected-1", "status": "queued"})

        with patch("content_factory.vibe_marketing_views.http_client.post", side_effect=fake_post):
            response = self.client.post(
                "/api/v1/vibe-marketing/article/",
                {
                    "clientRequestId": "vibe-article-selected-1",
                    "topicCandidateId": "topic:run:discovery-selection-1:australian-founders",
                    "selectedTitle": "What Australian Founders Need to Know Before Investing in AI Products",
                    "targetKeyword": "australian founders",
                    "sourceRunId": "discovery-selection-1",
                    "deliveryMode": "content_only",
                },
                format="json",
            )

        self.assertEqual(response.status_code, 202, response.data)
        self.assertEqual(captured["topic"], "What Australian Founders Need to Know Before Investing in AI Products")
        self.assertEqual(captured["target_keyword"], "australian founders")
        self.assertEqual(captured["custom_title"], "What Australian Founders Need to Know Before Investing in AI Products")
        self.assertEqual(captured["source_run_id"], "discovery-selection-1")
        self.assertEqual(captured["client_request_id"], "vibe-article-selected-1")
        self.assertEqual(captured["roo_points_action"], "article_generation")
        self.assertEqual(captured["roo_points_cost"], 6)
        self.assertEqual(captured["roo_points_billing_status"], "charged")
        self.assertTrue(captured["roo_points_ledger_id"])
        self.assertNotIn("source_discovery_run_id", captured)
        self.user.points_account.refresh_from_db()
        self.assertEqual(self.user.points_account.balance, 14)

    @override_settings(CONTENT_FACTORY_URL="https://content-factory.test", CONTENT_FACTORY_API_KEY="secret-key", IS_LOCAL_ENV=False)
    def test_article_start_accepts_legacy_frontend_stored_keyword_topic_id(self):
        organization, _created = Organization.objects.get_or_create(domain="acme.com", defaults={"name": "Acme"})
        self.company.organization = organization
        self.company.save(update_fields=["organization", "updated_at"])
        config, _created = OrganizationContentConfig.objects.get_or_create(organization=organization)
        config.github_repo = "acme/site"
        config.baseline_skipped_at = timezone.now()
        config.save(update_fields=["github_repo", "baseline_skipped_at", "updated_at"])
        keyword = ResearchedKeyword.objects.create(
            organization=organization,
            keyword="doctor jobs sydney",
            volume=120,
            difficulty=0,
            opportunity_index=900,
            status=KeywordStatus.PENDING,
        )
        legacy_frontend_id = f"topic:keyword-{keyword.id}:doctor-jobs-sydney"
        canonical_backend_id = f"topic:keyword{keyword.id}:doctor-jobs-sydney"
        captured = {}

        def fake_post(url, json=None, headers=None, timeout=None):
            captured.update(json or {})
            return _Response(status_code=202, payload={"run_id": "article-stored-keyword-1", "status": "queued"})

        with patch("content_factory.vibe_marketing_views.http_client.post", side_effect=fake_post):
            response = self.client.post(
                "/api/v1/vibe-marketing/article/",
                {
                    "clientRequestId": "vibe-article-stored-keyword-1",
                    "topicCandidateId": legacy_frontend_id,
                    "selectedTitle": "doctor jobs sydney",
                    "targetKeyword": "doctor jobs sydney",
                    "deliveryMode": "content_only",
                    "deliveryModeExplicit": True,
                },
                format="json",
            )

        self.assertEqual(response.status_code, 202, response.data)
        self.assertEqual(captured["topic"], "doctor jobs sydney")
        self.assertEqual(captured["target_keyword"], "doctor jobs sydney")
        self.assertEqual(captured["topic_candidate_id"], legacy_frontend_id)
        self.assertEqual(captured["topic_selection"]["resolved"]["id"], canonical_backend_id)
        self.assertEqual(captured["topic_selection"]["resolved"]["rawCandidateId"], f"keyword:{keyword.id}")

    @override_settings(CONTENT_FACTORY_URL="https://content-factory.test", CONTENT_FACTORY_API_KEY="secret-key", IS_LOCAL_ENV=False)
    def test_article_start_rejects_legacy_frontend_topic_id_with_stale_keyword(self):
        organization, _created = Organization.objects.get_or_create(domain="acme.com", defaults={"name": "Acme"})
        self.company.organization = organization
        self.company.save(update_fields=["organization", "updated_at"])
        config, _created = OrganizationContentConfig.objects.get_or_create(organization=organization)
        config.github_repo = "acme/site"
        config.baseline_skipped_at = timezone.now()
        config.save(update_fields=["github_repo", "baseline_skipped_at", "updated_at"])
        keyword = ResearchedKeyword.objects.create(
            organization=organization,
            keyword="doctor jobs sydney",
            volume=120,
            difficulty=0,
            opportunity_index=900,
            status=KeywordStatus.PENDING,
        )
        legacy_frontend_id = f"topic:keyword-{keyword.id}:doctor-jobs-sydney"

        with patch("content_factory.vibe_marketing_views.http_client.post") as post:
            response = self.client.post(
                "/api/v1/vibe-marketing/article/",
                {
                    "topicCandidateId": legacy_frontend_id,
                    "selectedTitle": "doctor jobs sydney",
                    "targetKeyword": "doctor australia jobs",
                    "deliveryMode": "content_only",
                },
                format="json",
            )

        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.data["field"], "topicCandidateId")
        self.assertIn("target_keyword", response.data["conflicts"])
        post.assert_not_called()

    @override_settings(CONTENT_FACTORY_URL="https://content-factory.test", CONTENT_FACTORY_API_KEY="secret-key", IS_LOCAL_ENV=False)
    def test_article_start_uses_review_draft_when_connected_config_is_legacy_content_only(self):
        organization, _created = Organization.objects.get_or_create(domain="acme.com", defaults={"name": "Acme"})
        self.company.organization = organization
        self.company.save(update_fields=["organization", "updated_at"])
        config, _created = OrganizationContentConfig.objects.get_or_create(organization=organization)
        config.github_repo = "acme/site"
        config.github_token_encrypted = "token"
        config.github_token_expires_at = timezone.now() + timezone.timedelta(hours=1)
        config.article_delivery_mode = "content_only"
        config.publish_targets = [{"id": "react_article_system", "state": "ready"}]
        config.baseline_skipped_at = timezone.now()
        config.save(
            update_fields=[
                "github_repo",
                "github_token_encrypted",
                "github_token_expires_at",
                "article_delivery_mode",
                "publish_targets",
                "baseline_skipped_at",
                "updated_at",
            ]
        )
        captured = {}

        def fake_post(url, json=None, headers=None, timeout=None):
            captured.update(json or {})
            return _Response(status_code=202, payload={"run_id": "article-review-draft-1", "status": "queued"})

        with patch("content_factory.vibe_marketing_views.http_client.post", side_effect=fake_post):
            response = self.client.post(
                "/api/v1/vibe-marketing/article/",
                {
                    "topic": "Founder workflow automation",
                    "targetKeyword": "founder workflow automation",
                    "deliveryMode": "content_only",
                },
                format="json",
            )

        self.assertEqual(response.status_code, 202)
        self.assertNotIn("delivery_mode", captured)
        self.assertFalse(captured["delivery_mode_confirmed"])
        self.assertFalse(captured["delivery_mode_explicit"])

    @override_settings(CONTENT_FACTORY_URL="https://content-factory.test", CONTENT_FACTORY_API_KEY="secret-key", IS_LOCAL_ENV=False)
    def test_article_start_honours_explicit_advanced_content_only(self):
        organization, _created = Organization.objects.get_or_create(domain="acme.com", defaults={"name": "Acme"})
        self.company.organization = organization
        self.company.save(update_fields=["organization", "updated_at"])
        config, _created = OrganizationContentConfig.objects.get_or_create(organization=organization)
        config.github_repo = "acme/site"
        config.github_token_encrypted = "token"
        config.github_token_expires_at = timezone.now() + timezone.timedelta(hours=1)
        config.article_delivery_mode = "content_only"
        config.publish_targets = [{"id": "react_article_system", "state": "ready"}]
        config.baseline_skipped_at = timezone.now()
        config.save(
            update_fields=[
                "github_repo",
                "github_token_encrypted",
                "github_token_expires_at",
                "article_delivery_mode",
                "publish_targets",
                "baseline_skipped_at",
                "updated_at",
            ]
        )
        captured = {}

        def fake_post(url, json=None, headers=None, timeout=None):
            captured.update(json or {})
            return _Response(status_code=202, payload={"run_id": "article-content-only-1", "status": "queued"})

        with patch("content_factory.vibe_marketing_views.http_client.post", side_effect=fake_post):
            response = self.client.post(
                "/api/v1/vibe-marketing/article/",
                {
                    "topic": "Founder workflow automation",
                    "targetKeyword": "founder workflow automation",
                    "deliveryMode": "content_only",
                    "deliveryModeExplicit": True,
                },
                format="json",
            )

        self.assertEqual(response.status_code, 202)
        self.assertEqual(captured["delivery_mode"], "content_only")
        self.assertTrue(captured["delivery_mode_explicit"])

    @override_settings(CONTENT_FACTORY_URL="https://content-factory.test", CONTENT_FACTORY_API_KEY="secret-key", IS_LOCAL_ENV=False)
    def test_blank_article_start_returns_validation_error_without_local_blocked_run(self):
        organization, _created = Organization.objects.get_or_create(domain="acme.com", defaults={"name": "Acme"})
        self.company.organization = organization
        self.company.save(update_fields=["organization", "updated_at"])
        before_count = ContentFactoryRun.objects.count()

        with patch("content_factory.vibe_marketing_views.http_client.post") as post:
            response = self.client.post("/api/v1/vibe-marketing/article/", {}, format="json")

        self.assertEqual(response.status_code, 400)
        self.assertIn("Choose a discovered topic", response.data["detail"])
        post.assert_not_called()
        self.assertEqual(ContentFactoryRun.objects.count(), before_count)

    def test_content_only_completed_run_serializes_package_evidence_without_publish_ready(self):
        organization, _created = Organization.objects.get_or_create(domain="acme.com", defaults={"name": "Acme"})
        self.company.organization = organization
        self.company.save(update_fields=["organization", "updated_at"])
        ContentFactoryRun.objects.create(
            run_id="article-content-only-1",
            workflow="article_generation",
            domain="acme.com",
            status=ContentFactoryRunStatus.COMPLETED,
            current_step="finalize",
            acceptance_summary={
                "content_packaged": True,
                "evidence_summary": {
                    "content_package_path": "/tmp/run/delivery_package.json",
                    "article_markdown_path": "/tmp/run/article.md",
                    "article_html_path": "/tmp/run/article.html",
                    "article_json_path": "/tmp/run/article.json",
                    "article_meta_path": "/tmp/run/article_meta.json",
                    "image_manifest_path": "/tmp/run/image_manifest.json",
                    "image_manifest_status": "complete",
                    "hero_image_url": "https://storage.example/hero.png?token=secret",
                    "generated_inline_image_count": 4,
                    "image_errors": [],
                    "content_package_title": "Content Package Article",
                    "content_package_slug": "content-package-article",
                    "content_package_target_keyword": "content package keyword",
                },
            },
            result={"delivery_mode": "content_only"},
        )

        response = self.client.get("/api/v1/vibe-marketing/bootstrap/")

        self.assertEqual(response.status_code, 200)
        checks = response.data["checks"]
        self.assertTrue(checks["write"]["passed"])
        self.assertTrue(checks["contentPackage"]["passed"])
        self.assertFalse(checks["publish"]["passed"])
        package = response.data["publishEvidence"]["contentPackage"]
        self.assertEqual(package["title"], "Content Package Article")
        self.assertEqual(package["slug"], "content-package-article")
        self.assertEqual(package["targetKeyword"], "content package keyword")
        self.assertTrue(package["heroImagePresent"])
        self.assertEqual(package["generatedInlineImageCount"], 4)
        self.assertEqual(package["imageErrorCount"], 0)
        self.assertEqual(package["artifactPaths"]["article.md"], "/tmp/run/article.md")

    def test_bootstrap_read_does_not_persist_completed_article_memory(self):
        organization, _created = Organization.objects.get_or_create(domain="acme.com", defaults={"name": "Acme"})
        OrganizationContentConfig.objects.get_or_create(organization=organization)
        self.company.organization = organization
        self.company.save(update_fields=["organization", "updated_at"])
        ContentFactoryRun.objects.create(
            run_id="article-read-only-bootstrap",
            workflow="article_generation",
            domain="acme.com",
            status=ContentFactoryRunStatus.COMPLETED,
            current_step="finalize",
            acceptance_summary={
                "content_packaged": True,
                "evidence_summary": {
                    "content_package_title": "Read Only Bootstrap Article",
                    "content_package_slug": "read-only-bootstrap-article",
                    "content_package_target_keyword": "read only bootstrap",
                },
            },
            result={"delivery_mode": "content_only"},
        )

        with CaptureQueriesContext(connection) as captured:
            response = self.client.get("/api/v1/vibe-marketing/bootstrap/")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(WrittenArticle.objects.filter(organization=organization).count(), 0)
        write_queries = [
            query["sql"]
            for query in captured.captured_queries
            if query["sql"].lstrip().upper().startswith(("INSERT ", "UPDATE ", "DELETE "))
        ]
        self.assertEqual(write_queries, [])

    def test_bootstrap_article_setup_state_uses_pending_setup_outside_latest_runs(self):
        organization, _created = Organization.objects.get_or_create(domain="acme.com", defaults={"name": "Acme"})
        config, _created = OrganizationContentConfig.objects.get_or_create(organization=organization)
        self.company.organization = organization
        self.company.save(update_fields=["organization", "updated_at"])
        config.github_repo = "Acme/site"
        config.article_system = {
            "state": "missing",
            "pending_article_system_setup": {
                "setupRunId": "setup-run-canonical",
                "setup_run_id": "setup-run-canonical",
                "sourceScanRunId": "scan-run-canonical",
                "source_scan_run_id": "scan-run-canonical",
                "status": "preview_failed",
                "routePath": "/articles",
                "route_path": "/articles",
            },
        }
        config.save(update_fields=["github_repo", "article_system", "updated_at"])
        setup_run = ContentFactoryRun.objects.create(
            run_id="setup-run-canonical",
            workflow="article_system_setup",
            domain="acme.com",
            github_repo="Acme/site",
            status=ContentFactoryRunStatus.BLOCKED,
            current_step="preview_failed",
            result={
                "article_system_setup": {
                    "setup_run_id": "setup-run-canonical",
                    "status": "preview_failed",
                    "preview_url": "https://preview.example/articles",
                }
            },
        )
        ContentFactoryRun.objects.filter(run_id=setup_run.run_id).update(updated_at=timezone.now() - timedelta(days=1))
        for index in range(8):
            run = ContentFactoryRun.objects.create(
                run_id=f"newer-article-run-{index}",
                workflow="article_generation",
                domain="acme.com",
                github_repo="Acme/site",
                status=ContentFactoryRunStatus.COMPLETED,
            )
            ContentFactoryRun.objects.filter(run_id=run.run_id).update(updated_at=timezone.now() + timedelta(minutes=index))

        response = self.client.get("/api/v1/vibe-marketing/bootstrap/?view=summary")

        self.assertEqual(response.status_code, 200)
        self.assertFalse(any(run["runId"] == "setup-run-canonical" for run in response.data["latestRuns"]))
        state = response.data["articleSetupState"]
        self.assertEqual(state["setupRunId"], "setup-run-canonical")
        self.assertEqual(state["setupStatus"], "preview_failed")
        self.assertEqual(state["scanRunId"], "scan-run-canonical")
        self.assertEqual(state["routePath"], "/articles")

    def test_bootstrap_article_setup_state_keeps_saved_route_without_fake_setup_run(self):
        organization, _created = Organization.objects.get_or_create(domain="acme.com", defaults={"name": "Acme"})
        config, _created = OrganizationContentConfig.objects.get_or_create(organization=organization)
        self.company.organization = organization
        self.company.save(update_fields=["organization", "updated_at"])
        config.github_repo = "Acme/site"
        config.article_system = {
            "state": "missing",
            "pending_article_system_setup": {
                "status": "pending_generation",
                "routePath": "/articles",
                "route_path": "/articles",
                "mode": "existing",
            },
        }
        config.save(update_fields=["github_repo", "article_system", "updated_at"])

        response = self.client.get("/api/v1/vibe-marketing/bootstrap/?view=summary")

        self.assertEqual(response.status_code, 200)
        state = response.data["articleSetupState"]
        self.assertEqual(state["routePath"], "/articles")
        self.assertIsNone(state["setupRunId"])
        self.assertIsNone(state["setupStatus"])
        self.assertIsNone(state["setupRunStatus"])
        self.assertFalse(state["setupBlocked"])
        self.assertFalse(response.data["checks"]["scaffold"]["setupBlocked"])

    def test_bootstrap_article_setup_state_keeps_completed_scan_without_scan_run(self):
        organization, _created = Organization.objects.get_or_create(domain="acme.com", defaults={"name": "Acme"})
        config, _created = OrganizationContentConfig.objects.get_or_create(organization=organization)
        self.company.organization = organization
        self.company.save(update_fields=["organization", "updated_at"])
        scanned_at = timezone.now() - timedelta(hours=2)
        config.github_repo = "Acme/site"
        config.last_scanned_sha = "abc123"
        config.last_scanned_at = scanned_at
        config.article_system = {
            "state": "missing",
            "scan": {
                "githubRepo": "Acme/site",
                "defaultBranch": "main",
                "defaultBranchSha": "abc123",
                "scanRunId": "scan-run-old",
                "status": "completed",
                "completedAt": scanned_at.isoformat(),
            },
        }
        config.save(update_fields=["github_repo", "last_scanned_sha", "last_scanned_at", "article_system", "updated_at"])
        for index in range(8):
            run = ContentFactoryRun.objects.create(
                run_id=f"recent-discovery-run-{index}",
                workflow="content_factory_discovery",
                domain="acme.com",
                github_repo="Acme/site",
                status=ContentFactoryRunStatus.COMPLETED,
            )
            ContentFactoryRun.objects.filter(run_id=run.run_id).update(updated_at=timezone.now() + timedelta(minutes=index))

        response = self.client.get("/api/v1/vibe-marketing/bootstrap/?view=summary")

        self.assertEqual(response.status_code, 200)
        self.assertFalse(any(run["workflow"] in {"repo_scan", "content_factory_scan"} for run in response.data["latestRuns"]))
        state = response.data["articleSetupState"]
        self.assertEqual(state["scanStatus"], "completed")
        self.assertEqual(state["scanRunId"], "scan-run-old")
        self.assertEqual(state["defaultBranch"], "main")
        self.assertEqual(state["defaultBranchSha"], "abc123")
        self.assertFalse(state["scanNeedsRescan"])

    def test_bootstrap_includes_resumable_article_drafts(self):
        organization, _created = Organization.objects.get_or_create(domain="acme.com", defaults={"name": "Acme"})
        OrganizationContentConfig.objects.get_or_create(organization=organization)
        self.company.organization = organization
        self.company.save(update_fields=["organization", "updated_at"])

        ContentFactoryRun.objects.create(
            run_id="article-running-draft-1",
            workflow="article_generation",
            domain="acme.com",
            status=ContentFactoryRunStatus.RUNNING,
            current_step="draft_section:intro",
            run_request={"topic": "Running Draft", "target_keyword": "running draft"},
        )
        ContentFactoryRun.objects.create(
            run_id="article-blocked-draft-1",
            workflow="confirmed_topic",
            domain="acme.com",
            status=ContentFactoryRunStatus.BLOCKED,
            current_step="verify_static",
            resume_available=True,
            run_request={"topic": "Blocked Draft", "target_keyword": "blocked draft"},
        )
        ContentFactoryRun.objects.create(
            run_id="article-ready-draft-1",
            workflow="article_generation",
            domain="acme.com",
            status=ContentFactoryRunStatus.COMPLETED,
            current_step="finalize",
            result={
                "delivery_package": {
                    "title": "Ready Draft",
                    "slug": "ready-draft",
                    "target_keyword": "ready draft",
                }
            },
        )
        ContentFactoryRun.objects.create(
            run_id="article-cancelled-draft-1",
            workflow="article_generation",
            domain="acme.com",
            status=ContentFactoryRunStatus.CANCELLED,
            run_request={"topic": "Cancelled Draft", "target_keyword": "cancelled draft"},
        )
        WrittenArticle.objects.create(
            organization=organization,
            title="Published Article",
            slug="published-article",
            category="featured",
            primary_keyword="published article",
        )
        ContentFactoryRun.objects.create(
            run_id="article-published-memory-1",
            workflow="article_generation",
            domain="acme.com",
            status=ContentFactoryRunStatus.COMPLETED,
            current_step="finalize",
            result={
                "delivery_package": {
                    "title": "Published Article",
                    "slug": "published-article",
                    "target_keyword": "published article",
                }
            },
        )

        response = self.client.get("/api/v1/vibe-marketing/bootstrap/")

        self.assertEqual(response.status_code, 200)
        drafts = response.data["draftArticles"]
        draft_ids = {item["runId"] for item in drafts}
        self.assertIn("article-running-draft-1", draft_ids)
        self.assertIn("article-blocked-draft-1", draft_ids)
        self.assertIn("article-ready-draft-1", draft_ids)
        self.assertNotIn("article-cancelled-draft-1", draft_ids)
        self.assertNotIn("article-published-memory-1", draft_ids)
        blocked = next(item for item in drafts if item["runId"] == "article-blocked-draft-1")
        self.assertEqual(blocked["actionKind"], "resume")
        self.assertEqual(blocked["actionLabel"], "Resume")
        ready = next(item for item in drafts if item["runId"] == "article-ready-draft-1")
        self.assertEqual(ready["stageLabel"], "Ready for review")
        self.assertEqual(ready["actionKind"], "continue")

    @override_settings(CONTENT_FACTORY_URL="https://content-factory.test", CONTENT_FACTORY_API_KEY="secret-key", IS_LOCAL_ENV=False)
    def test_blocked_article_draft_restart_creates_replacement_run(self):
        organization, _created = Organization.objects.get_or_create(domain="acme.com", defaults={"name": "Acme"})
        self.company.organization = organization
        self.company.save(update_fields=["organization", "updated_at"])
        config, _created = OrganizationContentConfig.objects.get_or_create(organization=organization)
        config.github_repo = "acme/site"
        config.save(update_fields=["github_repo", "updated_at"])
        run = ContentFactoryRun.objects.create(
            run_id="article-blocked-restart-1",
            workflow="article_generation",
            domain="acme.com",
            github_repo="acme/site",
            status=ContentFactoryRunStatus.BLOCKED,
            current_step="collect_research_bundle",
            resume_available=False,
            run_request={
                "run_id": "article-blocked-restart-1",
                "topic": "Restartable Draft",
                "target_keyword": "restartable draft",
                "delivery_mode": "content_only",
                "delivery_mode_explicit": True,
                "context": "Keep this context",
                "roo_points_authorized": True,
                "roo_points_action": "article_generation",
                "roo_points_cost": 6,
                "roo_points_required": 6,
                "roo_points_billing_status": "charged",
                "roo_points_ledger_id": "ledger-article-original",
            },
        )
        captured = {}

        def fake_post(url, json=None, headers=None, timeout=None):
            captured.update(json or {})
            return _Response(status_code=202, payload={"run_id": "article-restarted-1", "status": "queued"})

        with patch("content_factory.vibe_marketing_views.http_client.post", side_effect=fake_post):
            response = self.client.post(f"/api/v1/vibe-marketing/runs/{run.run_id}/restart/", {}, format="json")

        self.assertEqual(response.status_code, 202)
        self.assertEqual(response.data["runId"], "article-restarted-1")
        self.assertNotEqual(response.data["runId"], run.run_id)
        self.assertEqual(captured["topic"], "Restartable Draft")
        self.assertEqual(captured["target_keyword"], "restartable draft")
        self.assertEqual(captured["restart_source_run_id"], run.run_id)
        self.assertEqual(captured["roo_points_action"], "article_generation")
        self.assertEqual(captured["roo_points_billing_status"], "reused")
        self.assertEqual(captured["roo_points_ledger_id"], "ledger-article-original")
        self.assertNotIn("run_id", captured)
        run.refresh_from_db()
        self.assertEqual(run.result["restart_child_run_id"], "article-restarted-1")
        self.user.points_account.refresh_from_db()
        self.assertEqual(self.user.points_account.balance, 20)

    @override_settings(CONTENT_FACTORY_URL="https://content-factory.test", CONTENT_FACTORY_API_KEY="secret-key", IS_LOCAL_ENV=False)
    def test_content_only_preview_unavailable_restart_omits_implicit_delivery_mode(self):
        organization, _created = Organization.objects.get_or_create(domain="acme.com", defaults={"name": "Acme"})
        self.company.organization = organization
        self.company.save(update_fields=["organization", "updated_at"])
        config, _created = OrganizationContentConfig.objects.get_or_create(organization=organization)
        config.github_repo = "acme/site"
        config.github_token_encrypted = "token"
        config.github_token_expires_at = timezone.now() + timezone.timedelta(hours=1)
        config.article_delivery_mode = "content_only"
        config.publish_targets = [{"id": "react_article_system", "state": "ready"}]
        config.save(update_fields=["github_repo", "github_token_encrypted", "github_token_expires_at", "article_delivery_mode", "publish_targets", "updated_at"])
        run = ContentFactoryRun.objects.create(
            run_id="article-content-only-preview-missing-1",
            workflow="article_generation",
            domain="acme.com",
            github_repo="acme/site",
            status=ContentFactoryRunStatus.COMPLETED,
            current_step="finalize",
            resume_available=False,
            run_request={
                "run_id": "article-content-only-preview-missing-1",
                "topic": "Content Only Draft",
                "target_keyword": "content only draft",
                "delivery_mode": "content_only",
                "delivery_mode_explicit": False,
                "delivery_mode_confirmed": False,
                "roo_points_authorized": True,
                "roo_points_action": "article_generation",
                "roo_points_cost": 6,
                "roo_points_required": 6,
                "roo_points_billing_status": "charged",
                "roo_points_ledger_id": "ledger-content-only-original",
            },
            result={
                "livePreview": {
                    "status": "not_available",
                    "retryable": False,
                    "proof": {"previewUnavailableReason": "content_only_no_render_artifact"},
                }
            },
        )
        captured = {}

        def fake_post(url, json=None, headers=None, timeout=None):
            captured.update(json or {})
            return _Response(status_code=202, payload={"run_id": "article-exact-preview-restart-1", "status": "queued"})

        with patch("content_factory.vibe_marketing_views.http_client.post", side_effect=fake_post):
            response = self.client.post(f"/api/v1/vibe-marketing/runs/{run.run_id}/restart/", {}, format="json")

        self.assertEqual(response.status_code, 202)
        self.assertEqual(response.data["runId"], "article-exact-preview-restart-1")
        self.assertNotIn("delivery_mode", captured)
        self.assertFalse(captured["delivery_mode_confirmed"])
        self.assertFalse(captured["delivery_mode_explicit"])
        run.refresh_from_db()
        self.assertEqual(run.result["restart_child_run_id"], "article-exact-preview-restart-1")

    def test_nonrestartable_article_draft_returns_clear_error(self):
        organization, _created = Organization.objects.get_or_create(domain="acme.com", defaults={"name": "Acme"})
        self.company.organization = organization
        self.company.save(update_fields=["organization", "updated_at"])
        run = ContentFactoryRun.objects.create(
            run_id="article-revision-restart-1",
            workflow="article_revision",
            domain="acme.com",
            status=ContentFactoryRunStatus.BLOCKED,
            current_step="apply_component_feedback",
            resume_available=False,
            run_request={"source_run_id": "article-source-1"},
        )

        response = self.client.post(f"/api/v1/vibe-marketing/runs/{run.run_id}/restart/", {}, format="json")

        self.assertEqual(response.status_code, 409)
        self.assertIn("Only article generation drafts", response.data["detail"])

    @override_settings(CONTENT_FACTORY_URL="https://content-factory.test", CONTENT_FACTORY_API_KEY="secret-key", IS_LOCAL_ENV=False)
    def test_blocked_article_restart_requires_existing_billing_authorization(self):
        organization, _created = Organization.objects.get_or_create(domain="acme.com", defaults={"name": "Acme"})
        self.company.organization = organization
        self.company.save(update_fields=["organization", "updated_at"])
        config, _created = OrganizationContentConfig.objects.get_or_create(organization=organization)
        config.github_repo = "acme/site"
        config.save(update_fields=["github_repo", "updated_at"])
        run = ContentFactoryRun.objects.create(
            run_id="article-blocked-legacy-restart",
            workflow="article_generation",
            domain="acme.com",
            github_repo="acme/site",
            status=ContentFactoryRunStatus.BLOCKED,
            resume_available=False,
            run_request={
                "topic": "Legacy Draft",
                "target_keyword": "legacy draft",
                "delivery_mode": "content_only",
            },
        )

        with patch("content_factory.vibe_marketing_views.http_client.post") as post:
            response = self.client.post(f"/api/v1/vibe-marketing/runs/{run.run_id}/restart/", {}, format="json")

        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.data["code"], "roo_points_billing_required")
        post.assert_not_called()
        self.user.points_account.refresh_from_db()
        self.assertEqual(self.user.points_account.balance, 20)

    @override_settings(CONTENT_FACTORY_URL="", CONTENT_FACTORY_API_KEY="", IS_LOCAL_ENV=True)
    def test_article_start_blocks_when_content_factory_is_unconfigured_without_marking_keyword(self):
        organization, _created = Organization.objects.get_or_create(domain="acme.com", defaults={"name": "Acme"})
        self.company.organization = organization
        self.company.save(update_fields=["organization", "updated_at"])
        config, _created = OrganizationContentConfig.objects.get_or_create(organization=organization)
        config.github_repo = "acme/site"
        config.baseline_skipped_at = timezone.now()
        config.save(update_fields=["github_repo", "baseline_skipped_at", "updated_at"])

        with patch("content_factory.vibe_marketing_views.http_client.post") as post:
            response = self.client.post(
                "/api/v1/vibe-marketing/article/",
                {"topic": "Founder workflow automation", "targetKeyword": "founder workflow automation"},
                format="json",
            )

        self.assertEqual(response.status_code, 202)
        self.assertEqual(response.data["status"], ContentFactoryRunStatus.BLOCKED)
        post.assert_not_called()
        run = ContentFactoryRun.objects.get(run_id=response.data["run_id"])
        self.assertEqual(run.status, ContentFactoryRunStatus.BLOCKED)
        self.assertIn("content_factory_url_configured", run.result["diagnostics"])
        self.assertFalse(
            ResearchedKeyword.objects.filter(
                organization=organization,
                keyword_normalized="founder workflow automation",
                status=KeywordStatus.IN_PROGRESS,
            ).exists()
        )

    @override_settings(CONTENT_FACTORY_URL="https://content-factory.test", CONTENT_FACTORY_API_KEY="secret-key", IS_LOCAL_ENV=False)
    def test_article_start_marks_keyword_in_progress_after_successful_content_factory_dispatch(self):
        organization, _created = Organization.objects.get_or_create(domain="acme.com", defaults={"name": "Acme"})
        self.company.organization = organization
        self.company.save(update_fields=["organization", "updated_at"])
        config, _created = OrganizationContentConfig.objects.get_or_create(organization=organization)
        config.github_repo = "acme/site"
        config.baseline_skipped_at = timezone.now()
        config.save(update_fields=["github_repo", "baseline_skipped_at", "updated_at"])

        with patch(
            "content_factory.vibe_marketing_views.http_client.post",
            return_value=_Response(status_code=202, payload={"run_id": "article-queued-1", "status": "queued"}),
        ):
            response = self.client.post(
                "/api/v1/vibe-marketing/article/",
                {"topic": "Founder workflow automation", "targetKeyword": "founder workflow automation"},
                format="json",
            )

        self.assertEqual(response.status_code, 202)
        self.assertEqual(response.data["status"], ContentFactoryRunStatus.QUEUED)
        self.assertTrue(
            ResearchedKeyword.objects.filter(
                organization=organization,
                keyword_normalized="founder workflow automation",
                status=KeywordStatus.IN_PROGRESS,
            ).exists()
        )

    @override_settings(CONTENT_FACTORY_URL="", CONTENT_FACTORY_API_KEY="", IS_LOCAL_ENV=True)
    def test_article_run_status_blocks_when_content_factory_is_unconfigured(self):
        organization, _created = Organization.objects.get_or_create(domain="acme.com", defaults={"name": "Acme"})
        self.company.organization = organization
        self.company.save(update_fields=["organization", "updated_at"])
        run = ContentFactoryRun.objects.create(
            run_id="article-unconfigured-status-1",
            workflow="article_generation",
            domain="acme.com",
            status=ContentFactoryRunStatus.RUNNING,
            current_step="fetch_org_config",
        )

        with patch("content_factory.vibe_marketing_views.http_client.get") as get:
            response = self.client.get(f"/api/v1/vibe-marketing/runs/{run.run_id}/")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["status"], ContentFactoryRunStatus.BLOCKED)
        self.assertIn("Content Factory worker is unavailable", response.data["errors"][0])
        get.assert_not_called()
        run.refresh_from_db()
        self.assertEqual(run.status, ContentFactoryRunStatus.BLOCKED)
        self.assertEqual(run.current_step, "fetch_org_config")

    @override_settings(CONTENT_FACTORY_URL="https://content-factory.test", CONTENT_FACTORY_API_KEY="secret-key", IS_LOCAL_ENV=False)
    def test_article_run_status_blocks_when_content_factory_run_is_missing(self):
        organization, _created = Organization.objects.get_or_create(domain="acme.com", defaults={"name": "Acme"})
        self.company.organization = organization
        self.company.save(update_fields=["organization", "updated_at"])
        run = ContentFactoryRun.objects.create(
            run_id="article-missing-remote-1",
            workflow="article_generation",
            domain="acme.com",
            status=ContentFactoryRunStatus.RUNNING,
            current_step="fetch_org_config",
        )

        with patch("content_factory.vibe_marketing_views.http_client.get", return_value=_Response(status_code=404, payload={})):
            response = self.client.get(f"/api/v1/vibe-marketing/runs/{run.run_id}/")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["status"], ContentFactoryRunStatus.BLOCKED)
        self.assertIn(run.run_id, response.data["errors"][0])
        run.refresh_from_db()
        self.assertEqual(run.status, ContentFactoryRunStatus.BLOCKED)

    @override_settings(CONTENT_FACTORY_URL="https://content-factory.test", CONTENT_FACTORY_API_KEY="secret-key", IS_LOCAL_ENV=False)
    def test_article_run_status_timeout_preserves_local_running_state(self):
        organization, _created = Organization.objects.get_or_create(domain="acme.com", defaults={"name": "Acme"})
        self.company.organization = organization
        self.company.save(update_fields=["organization", "updated_at"])
        run = ContentFactoryRun.objects.create(
            run_id="article-status-timeout-1",
            workflow="article_generation",
            domain="acme.com",
            status=ContentFactoryRunStatus.RUNNING,
            current_step="draft_section:section-01",
        )

        timeout = http_client.exceptions.ReadTimeout(
            "HTTPConnectionPool(host='10.126.0.4', port=8000): Read timed out. (read timeout=15.0)"
        )
        with (
            patch("content_factory.vibe_marketing_views.http_client.get", side_effect=timeout),
            patch("content_factory.vibe_marketing_views._ensure_article_live_preview", side_effect=lambda current: current),
        ):
            response = self.client.get(f"/api/v1/vibe-marketing/runs/{run.run_id}/")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["status"], ContentFactoryRunStatus.RUNNING)
        self.assertEqual(response.data["errors"], [])
        run.refresh_from_db()
        self.assertEqual(run.status, ContentFactoryRunStatus.RUNNING)
        self.assertEqual(run.current_step, "draft_section:section-01")
        self.assertEqual(run.error, "")

    @override_settings(CONTENT_FACTORY_URL="https://content-factory.test", CONTENT_FACTORY_API_KEY="secret-key", IS_LOCAL_ENV=False)
    def test_article_run_status_timeout_does_not_downgrade_completed_state(self):
        organization, _created = Organization.objects.get_or_create(domain="acme.com", defaults={"name": "Acme"})
        self.company.organization = organization
        self.company.save(update_fields=["organization", "updated_at"])
        run = ContentFactoryRun.objects.create(
            run_id="article-completed-status-timeout-1",
            workflow="article_generation",
            domain="acme.com",
            status=ContentFactoryRunStatus.COMPLETED,
            current_step="finalize",
            result={
                "delivery_package": {
                    "title": "Reliable Content Harnesses",
                    "slug": "reliable-content-harnesses",
                    "target_keyword": "content harness",
                }
            },
        )

        timeout = http_client.exceptions.ReadTimeout(
            "HTTPConnectionPool(host='10.126.0.4', port=8000): Read timed out. (read timeout=15.0)"
        )
        with (
            patch("content_factory.vibe_marketing_views.http_client.get", side_effect=timeout),
            patch("content_factory.vibe_marketing_views._ensure_article_live_preview", side_effect=lambda current: current),
        ):
            response = self.client.get(f"/api/v1/vibe-marketing/runs/{run.run_id}/")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["status"], ContentFactoryRunStatus.COMPLETED)
        self.assertEqual(response.data["errors"], [])
        run.refresh_from_db()
        self.assertEqual(run.status, ContentFactoryRunStatus.COMPLETED)
        self.assertEqual(run.error, "")

    @override_settings(CONTENT_FACTORY_URL="https://content-factory.test", CONTENT_FACTORY_API_KEY="secret-key", IS_LOCAL_ENV=False)
    def test_article_run_status_heals_stale_timeout_blocked_run_with_completed_artifacts(self):
        organization, _created = Organization.objects.get_or_create(domain="acme.com", defaults={"name": "Acme"})
        self.company.organization = organization
        self.company.save(update_fields=["organization", "updated_at"])
        timeout_error = "HTTPConnectionPool(host='10.126.0.4', port=8000): Read timed out. (read timeout=15.0)"
        run = ContentFactoryRun.objects.create(
            run_id="article-stale-timeout-blocked-1",
            workflow="article_revision",
            domain="acme.com",
            status=ContentFactoryRunStatus.BLOCKED,
            current_step="ready_for_review",
            error=timeout_error,
            result={
                "errors": [timeout_error],
                "delivery_package": {
                    "title": "Reliable Content Harnesses",
                    "slug": "reliable-content-harnesses",
                    "target_keyword": "content harness",
                },
            },
        )

        timeout = http_client.exceptions.ReadTimeout(timeout_error)
        with (
            patch("content_factory.vibe_marketing_views.http_client.get", side_effect=timeout),
            patch("content_factory.vibe_marketing_views._ensure_article_live_preview", side_effect=lambda current: current),
        ):
            response = self.client.get(f"/api/v1/vibe-marketing/runs/{run.run_id}/")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["status"], ContentFactoryRunStatus.COMPLETED)
        self.assertEqual(response.data["errors"], [])
        run.refresh_from_db()
        self.assertEqual(run.status, ContentFactoryRunStatus.COMPLETED)
        self.assertEqual(run.error, "")
        self.assertEqual(run.result["status"], ContentFactoryRunStatus.COMPLETED)
        self.assertNotIn("errors", run.result)

    @override_settings(CONTENT_FACTORY_URL="https://content-factory.test", CONTENT_FACTORY_API_KEY="secret-key", IS_LOCAL_ENV=False)
    def test_article_run_status_persists_real_remote_blocked_status(self):
        organization, _created = Organization.objects.get_or_create(domain="acme.com", defaults={"name": "Acme"})
        self.company.organization = organization
        self.company.save(update_fields=["organization", "updated_at"])
        run = ContentFactoryRun.objects.create(
            run_id="article-real-remote-blocked-1",
            workflow="article_generation",
            domain="acme.com",
            status=ContentFactoryRunStatus.RUNNING,
            current_step="verify_static",
        )
        remote_payload = {
            "run_id": run.run_id,
            "workflow": "article_generation",
            "status": ContentFactoryRunStatus.BLOCKED,
            "current_step": "verify_static",
            "error": "Repository access token is missing.",
        }

        with (
            patch("content_factory.vibe_marketing_views.http_client.get", return_value=_Response(status_code=200, payload=remote_payload)),
            patch("content_factory.vibe_marketing_views._ensure_article_live_preview", side_effect=lambda current: current),
        ):
            response = self.client.get(f"/api/v1/vibe-marketing/runs/{run.run_id}/")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["status"], ContentFactoryRunStatus.BLOCKED)
        self.assertIn("Repository access token", response.data["errors"][0])
        run.refresh_from_db()
        self.assertEqual(run.status, ContentFactoryRunStatus.BLOCKED)
        self.assertEqual(run.error, "Repository access token is missing.")

    @override_settings(CONTENT_FACTORY_URL="https://content-factory.test", CONTENT_FACTORY_API_KEY="secret-key", IS_LOCAL_ENV=False)
    def test_article_run_status_does_not_downgrade_terminal_local_state(self):
        organization, _created = Organization.objects.get_or_create(domain="acme.com", defaults={"name": "Acme"})
        self.company.organization = organization
        self.company.save(update_fields=["organization", "updated_at"])
        run = ContentFactoryRun.objects.create(
            run_id="article-terminal-local-1",
            workflow="article_generation",
            domain="acme.com",
            status=ContentFactoryRunStatus.FAILED,
            current_step="synthesize_repository_contract",
            error="Task failed with unhandled exception: TimeLimitExceeded(5600)",
        )

        remote_payload = {
            "run_id": run.run_id,
            "workflow": "article_generation",
            "status": ContentFactoryRunStatus.RUNNING,
            "current_step": "synthesize_repository_contract",
        }
        with patch("content_factory.vibe_marketing_views.http_client.get", return_value=_Response(status_code=200, payload=remote_payload)):
            response = self.client.get(f"/api/v1/vibe-marketing/runs/{run.run_id}/")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["status"], ContentFactoryRunStatus.FAILED)
        self.assertIn("TimeLimitExceeded", response.data["errors"][0])
        run.refresh_from_db()
        self.assertEqual(run.status, ContentFactoryRunStatus.FAILED)
        self.assertEqual(run.current_step, "synthesize_repository_contract")

    def test_article_generation_requires_baseline_or_skip(self):
        organization, _created = Organization.objects.get_or_create(domain="acme.com", defaults={"name": "Acme"})
        self.company.organization = organization
        self.company.save(update_fields=["organization", "updated_at"])
        config, _created = OrganizationContentConfig.objects.get_or_create(organization=organization)
        config.github_repo = "acme/site"
        config.save(update_fields=["github_repo", "updated_at"])

        response = self.client.post(
            "/api/v1/vibe-marketing/article/",
            {"topic": "Founder workflow automation"},
            format="json",
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn("baseline", response.data["detail"])

        skip = self.client.post("/api/v1/vibe-marketing/baseline/skip/", {}, format="json")
        self.assertEqual(skip.status_code, 200)
        with patch("content_factory.vibe_marketing_views.http_client.post", return_value=_Response(status_code=202, payload={"run_id": "article-run-1", "status": "queued"})):
            response = self.client.post(
                "/api/v1/vibe-marketing/article/",
                {"topic": "Founder workflow automation"},
                format="json",
            )
        self.assertEqual(response.status_code, 202)

    def test_google_baseline_connection_accepts_search_console_scope(self):
        connection = GoogleConnection.objects.create(
            user=self.user,
            google_email="founder@example.com",
            refresh_token="refresh-token",
            scope=GSC_SCOPE,
        )

        status_payload = google_baseline_connection_status(self.user)
        self.assertTrue(status_payload["connected"])
        self.assertTrue(status_payload["hasBaselineScopes"])
        self.assertEqual(status_payload["status"], "connected")

        connection.scope = f"{GSC_SCOPE} {GA4_SCOPE}"
        connection.save(update_fields=["scope", "updated_at"])

        complete = google_baseline_connection_status(self.user)
        self.assertTrue(complete["hasBaselineScopes"])
        self.assertEqual(complete["status"], "connected")

    def test_google_baseline_collects_search_console_metrics_without_ga4_scope(self):
        GoogleConnection.objects.create(
            user=self.user,
            google_email="founder@example.com",
            refresh_token="refresh-token",
            scope=GSC_SCOPE,
        )
        service = _SearchConsoleService([{"siteUrl": "sc-domain:acme.com"}])

        with patch("content_factory.google_baseline.get_refreshed_credentials", return_value=SimpleNamespace()):
            with patch("googleapiclient.discovery.build", return_value=service):
                result = collect_verified_google_metrics(
                    user=self.user,
                    domain="https://www.acme.com",
                    now=datetime(2026, 4, 27, tzinfo=datetime_timezone.utc),
                )

        traffic = result["traffic"]
        self.assertEqual(traffic["status"], "measured")
        self.assertEqual(result["sourceStatus"]["googleSearchConsole"], "measured")
        self.assertEqual(traffic["googleSearchConsole"]["siteUrl"], "sc-domain:acme.com")
        self.assertEqual(traffic["googleSearchConsole"]["last28Days"]["clicks"], 25)
        self.assertEqual(traffic["googleSearchConsole"]["daily"][0]["keys"], ["2026-03-31"])
        self.assertEqual(traffic["googleSearchConsole"]["topQueries"][0]["keys"], ["startup automation"])
        self.assertEqual(traffic["googleSearchConsole"]["topPages"][0]["keys"], ["https://acme.com/blog"])
        self.assertEqual(traffic["googleAnalytics"]["status"], "needs_connection")
        self.assertGreater(traffic["score"], 0)

    def test_google_baseline_returns_needs_connection_when_no_search_console_property_matches(self):
        GoogleConnection.objects.create(
            user=self.user,
            google_email="founder@example.com",
            refresh_token="refresh-token",
            scope=GSC_SCOPE,
        )
        service = _SearchConsoleService([{"siteUrl": "sc-domain:other.com"}])

        with patch("content_factory.google_baseline.get_refreshed_credentials", return_value=SimpleNamespace()):
            with patch("googleapiclient.discovery.build", return_value=service):
                result = collect_verified_google_metrics(
                    user=self.user,
                    domain="acme.com",
                    now=datetime(2026, 4, 27, tzinfo=datetime_timezone.utc),
                )

        self.assertEqual(result["traffic"]["status"], "needs_connection")
        self.assertEqual(result["sourceStatus"]["googleSearchConsole"], "needs_connection")
        self.assertIn("No verified Search Console property", result["traffic"]["message"])
        self.assertIn("No verified Search Console property", result["traffic"]["googleSearchConsole"]["message"])

    def test_baseline_google_refresh_merges_search_console_metrics_into_snapshot(self):
        organization, _created = Organization.objects.get_or_create(domain="acme.com", defaults={"name": "Acme"})
        self.company.organization = organization
        self.company.save(update_fields=["organization", "updated_at"])
        OrganizationContentConfig.objects.get_or_create(organization=organization)
        WebsiteBaselineSnapshot.objects.create(
            organization=organization,
            domain="acme.com",
            run_id="baseline-google-refresh",
            overall_score=76,
            summary={"text": "Website baseline is workable."},
            metrics={"traffic": {"status": "needs_connection", "score": None}},
            source_status={"traffic": "needs_connection"},
            raw_payload={
                "metrics": {"traffic": {"status": "needs_connection", "score": None}},
                "sourceStatus": {"traffic": "needs_connection"},
            },
        )
        google_metrics = {
            "traffic": {
                "status": "measured",
                "verified": True,
                "score": 43,
                "googleSearchConsole": {
                    "status": "measured",
                    "siteUrl": "sc-domain:acme.com",
                    "last28Days": {"clicks": 25, "impressions": 400, "ctr": 0.0625, "position": 7.1},
                    "last90Days": {"clicks": 50, "impressions": 900, "ctr": 0.055, "position": 8.0},
                    "daily": [{"keys": ["2026-04-01"], "clicks": 6, "impressions": 100, "ctr": 0.06, "position": 7.4}],
                    "topQueries": [{"keys": ["startup automation"], "clicks": 7, "impressions": 120, "ctr": 0.058, "position": 6.3}],
                    "topPages": [{"keys": ["https://acme.com/blog"], "clicks": 5, "impressions": 90, "ctr": 0.056, "position": 5.8}],
                },
            },
            "sourceStatus": {"googleSearchConsole": "measured", "googleAnalytics": "needs_connection"},
        }

        with patch("content_factory.vibe_marketing_views.collect_verified_google_metrics", return_value=google_metrics):
            response = self.client.post("/api/v1/vibe-marketing/baseline/google-refresh/", {}, format="json")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["websiteBaseline"]["sourceStatus"]["traffic"], "measured")
        self.assertEqual(response.data["websiteBaseline"]["metrics"]["traffic"]["googleSearchConsole"]["last28Days"]["clicks"], 25)
        snapshot = WebsiteBaselineSnapshot.objects.get(run_id="baseline-google-refresh")
        self.assertEqual(snapshot.source_status["traffic"], "measured")
        self.assertEqual(snapshot.metrics["traffic"]["googleSearchConsole"]["topQueries"][0]["keys"], ["startup automation"])

    def test_github_connect_returns_auth_url_when_credentials_missing(self):
        response = self.client.post(
            "/api/v1/vibe-marketing/github/connect/",
            {"githubRepo": "acme/site"},
            format="json",
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["status"], "auth_required")
        self.assertIn("https://github.com/apps/mlai-tools/installations/new", response.data["auth_url"])

        config = OrganizationContentConfig.objects.get(organization__domain="acme.com")
        self.assertEqual(config.github_repo, "acme/site")
        self.assertEqual(config.connected_slack_user_id, f"mlai_user:{self.user.id}")
        self.assertNotIn("github_token", response.data)

    def test_github_connect_reuses_valid_org_credentials(self):
        organization, _created = Organization.objects.get_or_create(domain="acme.com", defaults={"name": "Acme"})
        self.company.organization = organization
        self.company.save(update_fields=["organization", "updated_at"])
        OrganizationContentConfig.objects.update_or_create(
            organization=organization,
            defaults={
                "github_repo": "acme/site",
                "github_token_encrypted": "org-token",
                "github_token_expires_at": timezone.now() + timezone.timedelta(hours=1),
            },
        )

        response = self.client.post(
            "/api/v1/vibe-marketing/github/connect/",
            {"githubRepo": "acme/site"},
            format="json",
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["status"], "already_connected")
        self.assertEqual(response.data["connection_state"], "connected")
        self.assertEqual(response.data["github_repo"], "acme/site")
        self.assertNotIn("org-token", str(response.data))

        bootstrap = self.client.get("/api/v1/vibe-marketing/bootstrap/")
        self.assertTrue(bootstrap.data["checks"]["github"]["passed"])

    def test_github_connect_force_reconnect_returns_auth_url_without_overwriting_repo(self):
        organization, _created = Organization.objects.get_or_create(domain="acme.com", defaults={"name": "Acme"})
        self.company.organization = organization
        self.company.save(update_fields=["organization", "updated_at"])
        OrganizationContentConfig.objects.update_or_create(
            organization=organization,
            defaults={
                "github_repo": "acme/site",
                "github_token_encrypted": "org-token",
                "github_token_expires_at": timezone.now() + timezone.timedelta(hours=1),
            },
        )

        with (
            patch("content_factory.vibe_marketing_views.ensure_valid_org_token") as mock_ensure_valid_org_token,
            patch("content_factory.vibe_marketing_views.build_github_auth_url", return_value="https://github.example/install") as mock_auth_url,
        ):
            response = self.client.post(
                "/api/v1/vibe-marketing/github/connect/",
                {"githubRepo": "other/site", "forceReconnect": True},
                format="json",
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["status"], "auth_required")
        self.assertEqual(response.data["connection_state"], "auth_required")
        self.assertEqual(response.data["github_repo"], "acme/site")
        self.assertEqual(response.data["auth_url"], "https://github.example/install")
        mock_ensure_valid_org_token.assert_not_called()
        mock_auth_url.assert_called_once()

        config = OrganizationContentConfig.objects.get(organization=organization)
        self.assertEqual(config.github_repo, "acme/site")

    def test_github_connect_returns_structured_error_when_auth_url_unavailable(self):
        with patch("content_factory.vibe_marketing_views.build_github_auth_url", side_effect=RuntimeError("bad config")):
            response = self.client.post(
                "/api/v1/vibe-marketing/github/connect/",
                {"forceReconnect": True},
                format="json",
            )

        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.data["status"], "auth_unavailable")
        self.assertEqual(response.data["connection_state"], "auth_required")
        self.assertEqual(response.data["error"], "github_auth_url_failed")
        self.assertIn("GitHub authorization could not be opened", response.data["detail"])

    def test_github_connect_refreshes_expired_org_credentials_server_side(self):
        organization, _created = Organization.objects.get_or_create(domain="acme.com", defaults={"name": "Acme"})
        self.company.organization = organization
        self.company.save(update_fields=["organization", "updated_at"])
        config, _created = OrganizationContentConfig.objects.update_or_create(
            organization=organization,
            defaults={
                "github_repo": "acme/site",
                "github_token_encrypted": "old-org-token",
                "github_refresh_token_encrypted": "org-refresh",
                "github_token_expires_at": timezone.now() - timezone.timedelta(minutes=1),
            },
        )

        def fake_ensure_valid_org_token(domain):
            self.assertEqual(domain, "acme.com")
            config.github_token_encrypted = "new-org-token"
            config.github_token_expires_at = timezone.now() + timezone.timedelta(hours=1)
            config.save(update_fields=["github_token_encrypted", "github_token_expires_at", "updated_at"])
            return "new-org-token"

        with patch("content_factory.vibe_marketing_views.ensure_valid_org_token", side_effect=fake_ensure_valid_org_token):
            response = self.client.post(
                "/api/v1/vibe-marketing/github/connect/",
                {"githubRepo": "acme/site"},
                format="json",
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["status"], "already_connected")
        self.assertNotIn("new-org-token", str(response.data))
        config.refresh_from_db()
        self.assertEqual(config.github_token_encrypted, "new-org-token")

    def test_github_connect_promotes_matching_user_credentials_to_org(self):
        actor_id = f"mlai_user:{self.user.id}"
        UserIntegration.objects.create(
            slack_user_id=actor_id,
            github_access_token="user-token",
            github_refresh_token="user-refresh",
            github_token_expires_at=timezone.now() + timezone.timedelta(hours=1),
            github_user_name="octocat",
            github_repo="acme/site",
            github_installation_id="inst-1",
            github_scopes=["repo"],
        )

        response = self.client.post(
            "/api/v1/vibe-marketing/github/connect/",
            {"githubRepo": "acme/site"},
            format="json",
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["status"], "already_connected")
        self.assertEqual(response.data["credential_source"], "user_promoted")
        self.assertNotIn("user-token", str(response.data))

        config = OrganizationContentConfig.objects.get(organization__domain="acme.com")
        self.assertEqual(config.github_token_encrypted, "user-token")
        self.assertEqual(config.github_refresh_token_encrypted, "user-refresh")
        self.assertEqual(config.github_repo, "acme/site")
        self.assertEqual(config.github_installation_id, "inst-1")

    def test_github_repos_lists_connected_installation_repositories(self):
        organization, _created = Organization.objects.get_or_create(domain="acme.com", defaults={"name": "Acme"})
        self.company.organization = organization
        self.company.save(update_fields=["organization", "updated_at"])
        OrganizationContentConfig.objects.update_or_create(
            organization=organization,
            defaults={
                "github_repo": "MLAI-AUS-Inc/mlai-au",
                "github_token_encrypted": "org-token",
                "github_token_expires_at": timezone.now() + timezone.timedelta(hours=1),
                "github_installation_id": "inst-1",
            },
        )

        def fake_github_request(method, path, *, token, body=None, expected=(200,)):
            self.assertEqual(method, "GET")
            self.assertEqual(token, "org-token")
            self.assertIn("/user/installations/inst-1/repositories", path)
            return {
                "repositories": [
                    {
                        "full_name": "MLAI-AUS-Inc/mlai-au",
                        "name": "mlai-au",
                        "private": True,
                        "default_branch": "main",
                        "owner": {"login": "MLAI-AUS-Inc"},
                    }
                ]
            }

        with patch("content_factory.vibe_marketing_views._github_api_request", side_effect=fake_github_request):
            response = self.client.get("/api/v1/vibe-marketing/github/repos/")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["status"], "connected")
        self.assertEqual(response.data["selectedRepo"], "MLAI-AUS-Inc/mlai-au")
        self.assertEqual(response.data["repos"][0]["fullName"], "MLAI-AUS-Inc/mlai-au")
        self.assertEqual(response.data["repos"][0]["defaultBranch"], "main")

    @override_settings(CONTENT_FACTORY_URL="https://content-factory.test", CONTENT_FACTORY_API_KEY="secret-key", IS_LOCAL_ENV=False)
    def test_scan_forwards_article_surface_hint_to_content_factory(self):
        organization, _created = Organization.objects.get_or_create(domain="acme.com", defaults={"name": "Acme"})
        self.company.organization = organization
        self.company.save(update_fields=["organization", "updated_at"])
        OrganizationContentConfig.objects.update_or_create(
            organization=organization,
            defaults={"github_repo": "acme/site"},
        )

        class FakeResponse:
            status_code = 202
            content = b"{}"

            def json(self):
                return {"run_id": "scan-hint-1", "status": "queued", "workflow": "repo_scan"}

        def fake_post(url, json=None, headers=None, timeout=None):
            self.assertEqual(url, "https://content-factory.test/api/runs/scan")
            self.assertEqual(json["github_repo"], "acme/site")
            self.assertEqual(json["scan_purpose"], "setup")
            self.assertIs(json["scaffold_if_missing"], True)
            self.assertEqual(json["article_surface_mode"], "existing")
            self.assertIs(json["auto_setup_preview"], True)
            self.assertEqual(
                json["article_surface_hint"],
                {"source": "user_input", "listing_url": "https://www.acme.com/articles", "route_path": "/articles"},
            )
            return FakeResponse()

        with patch("content_factory.vibe_marketing_views.http_client.post", side_effect=fake_post):
            response = self.client.post(
                "/api/v1/vibe-marketing/scan/",
                {
                    "githubRepo": "acme/site",
                    "articleSurfaceMode": "existing",
                    "articleSurfaceUrl": "https://www.acme.com/articles",
                    "autoSetupPreview": True,
                },
                format="json",
            )

        self.assertEqual(response.status_code, 202)
        self.assertEqual(response.data["runId"], "scan-hint-1")
        run = ContentFactoryRun.objects.get(run_id="scan-hint-1")
        self.assertEqual(run.run_request["article_surface_hint"]["route_path"], "/articles")
        self.assertEqual(run.result["scan_purpose"], "setup")
        self.assertEqual(run.run_request["scan_purpose"], "setup")
        config = OrganizationContentConfig.objects.get(organization=organization)
        pending = config.article_system["pending_article_system_setup"]
        self.assertEqual(pending["mode"], "existing")
        self.assertEqual(pending["routePath"], "/articles")
        self.assertEqual(pending["sourceScanRunId"], "scan-hint-1")

    @override_settings(CONTENT_FACTORY_URL="https://content-factory.test", CONTENT_FACTORY_API_KEY="secret-key", IS_LOCAL_ENV=False)
    def test_inventory_scan_does_not_require_article_surface_url(self):
        organization, _created = Organization.objects.get_or_create(domain="acme.com", defaults={"name": "Acme"})
        self.company.organization = organization
        self.company.save(update_fields=["organization", "updated_at"])
        OrganizationContentConfig.objects.update_or_create(
            organization=organization,
            defaults={"github_repo": "acme/site"},
        )

        class FakeResponse:
            status_code = 202
            content = b"{}"

            def json(self):
                return {"run_id": "scan-inventory-1", "status": "queued", "workflow": "repo_scan"}

        def fake_post(url, json=None, headers=None, timeout=None):
            self.assertEqual(url, "https://content-factory.test/api/runs/scan")
            self.assertEqual(json["github_repo"], "acme/site")
            self.assertEqual(json["scan_purpose"], "inventory")
            self.assertIs(json["scaffold_if_missing"], False)
            self.assertIs(json["auto_setup_preview"], False)
            self.assertEqual(json["article_surface_mode"], "not_sure")
            self.assertNotIn("article_surface_hint", json)
            return FakeResponse()

        with patch("content_factory.vibe_marketing_views.http_client.post", side_effect=fake_post):
            response = self.client.post(
                "/api/v1/vibe-marketing/scan/",
                {
                    "githubRepo": "acme/site",
                    "scanPurpose": "inventory",
                },
                format="json",
            )

        self.assertEqual(response.status_code, 202)
        self.assertEqual(response.data["runId"], "scan-inventory-1")
        run = ContentFactoryRun.objects.get(run_id="scan-inventory-1")
        self.assertEqual(run.run_request["scan_purpose"], "inventory")
        self.assertEqual(run.result["scan_purpose"], "inventory")
        config = OrganizationContentConfig.objects.get(organization=organization)
        self.assertNotIn("pending_article_system_setup", config.article_system or {})

    @override_settings(CONTENT_FACTORY_URL="", CONTENT_FACTORY_API_KEY="", IS_LOCAL_ENV=True)
    def test_article_system_setup_requires_content_factory_remote(self):
        organization, _created = Organization.objects.get_or_create(domain="acme.com", defaults={"name": "Acme"})
        self.company.organization = organization
        self.company.save(update_fields=["organization", "updated_at"])
        OrganizationContentConfig.objects.update_or_create(
            organization=organization,
            defaults={"github_repo": "acme/site"},
        )

        response = self.client.post(
            "/api/v1/vibe-marketing/article-system-setup/",
            {
                "githubRepo": "acme/site",
                "articleSurfaceMode": "existing",
                "articleSurfaceUrl": "https://www.acme.com/articles",
            },
            format="json",
        )

        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.data["status"], ContentFactoryRunStatus.BLOCKED)
        self.assertNotIn("setupRunId", response.data)
        run = ContentFactoryRun.objects.get(workflow="article_system_setup", domain="acme.com")
        self.assertEqual(run.status, ContentFactoryRunStatus.BLOCKED)
        self.assertTrue(run.result["content_factory_dispatch_blocked"])
        self.assertNotIn("setup_run_id", run.result)
        config = OrganizationContentConfig.objects.get(organization=organization)
        pending = config.article_system["pending_article_system_setup"]
        self.assertEqual(pending["routePath"], "/articles")
        self.assertNotIn("setupRunId", pending)

    @override_settings(CONTENT_FACTORY_URL="https://content-factory.test", CONTENT_FACTORY_API_KEY="secret-key", IS_LOCAL_ENV=False)
    def test_article_system_setup_records_confirmed_content_factory_run(self):
        organization, _created = Organization.objects.get_or_create(domain="acme.com", defaults={"name": "Acme"})
        self.company.organization = organization
        self.company.save(update_fields=["organization", "updated_at"])
        OrganizationContentConfig.objects.update_or_create(
            organization=organization,
            defaults={"github_repo": "acme/site"},
        )

        class FakeResponse:
            status_code = 202
            content = b"{}"

            def json(self):
                return {"run_id": "setup-confirmed-1", "status": "queued", "workflow": "article_system_setup"}

        def fake_post(url, json=None, headers=None, timeout=None):
            self.assertEqual(url, "https://content-factory.test/api/runs/article-system-setup")
            self.assertEqual(json["github_repo"], "acme/site")
            self.assertEqual(json["scan_purpose"], "setup")
            self.assertEqual(json["article_surface_hint"]["route_path"], "/articles")
            return FakeResponse()

        with patch("content_factory.vibe_marketing_views.http_client.post", side_effect=fake_post):
            response = self.client.post(
                "/api/v1/vibe-marketing/article-system-setup/",
                {
                    "githubRepo": "acme/site",
                    "articleSurfaceMode": "existing",
                    "articleSurfaceUrl": "https://www.acme.com/articles",
                },
                format="json",
            )

        self.assertEqual(response.status_code, 202)
        self.assertEqual(response.data["setupRunId"], "setup-confirmed-1")
        run = ContentFactoryRun.objects.get(run_id="setup-confirmed-1")
        self.assertEqual(run.workflow, "article_system_setup")
        self.assertEqual(run.status, ContentFactoryRunStatus.QUEUED)
        self.assertEqual(run.result["setup_run_id"], "setup-confirmed-1")
        config = OrganizationContentConfig.objects.get(organization=organization)
        pending = config.article_system["pending_article_system_setup"]
        self.assertEqual(pending["routePath"], "/articles")
        self.assertEqual(pending["setupRunId"], "setup-confirmed-1")
        self.assertEqual(pending["setupStatus"], ContentFactoryRunStatus.QUEUED)

    def test_scan_rejects_mismatched_article_surface_domain(self):
        response = self.client.post(
            "/api/v1/vibe-marketing/scan/",
            {
                "githubRepo": "acme/site",
                "articleSurfaceMode": "existing",
                "articleSurfaceUrl": "https://example.com/blog",
            },
            format="json",
        )

        self.assertEqual(response.status_code, 400)
        self.assertIn("company website domain", response.data["detail"])

    def test_unrelated_settings_update_not_blocked_by_enabled_daily_prerequisites(self):
        organization, _created = Organization.objects.get_or_create(domain="acme.com", defaults={"name": "Acme"})
        self.company.organization = organization
        self.company.save(update_fields=["organization", "updated_at"])
        config, _created = OrganizationContentConfig.objects.get_or_create(organization=organization)
        config.daily_discovery_enabled = True
        config.save(update_fields=["daily_discovery_enabled", "updated_at"])

        response = self.client.put(
            "/api/v1/vibe-marketing/settings/",
            {"githubRepo": "acme/site"},
            format="json",
        )

        self.assertEqual(response.status_code, 200)
        config.refresh_from_db()
        self.assertEqual(config.github_repo, "acme/site")

    def test_enabling_daily_still_requires_prerequisites(self):
        response = self.client.put(
            "/api/v1/vibe-marketing/settings/",
            {"dailyDiscoveryEnabled": True},
            format="json",
        )

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.data["detail"], "Daily generation prerequisites are not complete.")
