from datetime import datetime, timedelta, timezone as datetime_timezone
from types import SimpleNamespace
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.db import OperationalError, connection
from django.test import TestCase, override_settings
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
from integrations import http_client
from integrations.models import GoogleConnection, UserIntegration
from organizations.models import Organization
from startup_updates.models import StartupProfile
from workflow_runs.models import ContentFactoryRun, ContentFactoryRunStep, ContentFactoryRunStatus


User = get_user_model()


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
        self.client.force_authenticate(user=self.user)

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
            self.assertEqual(json["min_seed_keywords"], 20)
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
        self.assertEqual(run.run_request["persist"], False)
        self.assertEqual(run.run_request["company_linkedin_url"], "https://www.linkedin.com/company/acme")
        self.assertEqual(run.run_request["existing_fields"]["companyContext"], "")
        self.assertEqual(run.run_request["existing_fields"]["companyLinkedInUrl"], "https://www.linkedin.com/company/acme")

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
            self.assertEqual(json["research_depth"], "deep")
            self.assertTrue(json["strict_deep_research"])
            self.assertEqual(json["min_direct_competitors"], 3)
            self.assertEqual(json["min_seed_keywords"], 20)
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
                    "competitors": ["buildclub.ai", "aussiefoundersclub.com"],
                    "seedKeywords": ["ai events melbourne", "founder automation"],
                    "founderNames": ["Sam Donegan"],
                    "stage": "Seed",
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

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["status"], "completed")
        self.assertIn("## Positioning", response.data["result"]["autofill"]["companyContext"])
        self.assertEqual(response.data["result"]["autofill"]["seedKeywordCount"], 20)
        self.assertEqual(response.data["result"]["autofill"]["offeringProfile"]["excludedMeanings"], ["home equity"])
        self.assertEqual(response.data["result"]["autofill"]["keywordCandidates"][0]["keyword"], "workflow automation for founders")
        self.assertEqual(response.data["result"]["autofill"]["directCompetitors"][0]["domain"], "buildclub.ai")
        self.assertEqual(response.data["result"]["autofill"]["companyLinkedInUrl"], "https://www.linkedin.com/company/acme")
        self.assertEqual(response.data["result"]["autofill"]["researchDepth"]["linkedinSimilarSignals"], 1)

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
                    "metrics": {"technicalHealth": {"status": "measured", "score": 82}},
                    "sourceStatus": {"technicalHealth": "measured", "traffic": "needs_connection"},
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
        self.assertEqual(candidates[0]["id"], "aus-founders-ai")
        self.assertEqual(candidates[0]["keyword"], "australian founders")
        self.assertEqual(candidates[0]["title"], "What Australian Founders Need to Know Before Investing in AI Products")
        self.assertEqual(candidates[0]["opportunityScore"], 82)
        self.assertEqual(candidates[0]["sourceRunId"], "discovery-selection-1")

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
        self.assertEqual(candidate["relatedKeywords"], ["startup crm comparison"])
        self.assertEqual(candidate["paaQuestions"][0]["question"], "What CRM should an AI startup use?")
        self.assertTrue(candidate["paaQuestions"][0]["hasAiOverview"])
        self.assertTrue(candidate["aiSaturation"]["aiOverviewPresent"])
        self.assertEqual(candidate["aiSaturation"]["serpFeatures"], ["featured_snippet"])

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
        captured = {}

        def fake_post(url, json=None, headers=None, timeout=None):
            captured.update(json or {})
            return _Response(status_code=202, payload={"run_id": "article-selected-1", "status": "queued"})

        with patch("content_factory.vibe_marketing_views.http_client.post", side_effect=fake_post):
            response = self.client.post(
                "/api/v1/vibe-marketing/article/",
                {
                    "topicCandidateId": "aus-founders-ai",
                    "selectedTitle": "What Australian Founders Need to Know Before Investing in AI Products",
                    "targetKeyword": "australian founders",
                    "sourceRunId": "discovery-selection-1",
                    "deliveryMode": "content_only",
                },
                format="json",
            )

        self.assertEqual(response.status_code, 202)
        self.assertEqual(captured["topic"], "What Australian Founders Need to Know Before Investing in AI Products")
        self.assertEqual(captured["target_keyword"], "australian founders")
        self.assertEqual(captured["custom_title"], "What Australian Founders Need to Know Before Investing in AI Products")
        self.assertEqual(captured["source_run_id"], "discovery-selection-1")
        self.assertNotIn("source_discovery_run_id", captured)

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
