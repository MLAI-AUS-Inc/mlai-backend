from types import SimpleNamespace
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from rest_framework.test import APIClient

from content_factory.google_baseline import GA4_SCOPE, GSC_SCOPE, google_baseline_connection_status
from content_factory.models import OrganizationContentConfig, WebsiteBaselineSnapshot
from founder_tools.models import VibeRaisingCompany, VibeRaisingProfile
from integrations.models import GoogleConnection
from organizations.models import Organization
from workflow_runs.models import ContentFactoryRun, ContentFactoryRunStep, ContentFactoryRunStatus


User = get_user_model()


class _Response(SimpleNamespace):
    text = ""

    @property
    def content(self):
        return b"{}"

    def json(self):
        return self.payload


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
        self.assertEqual(response.data["result"]["autofill"]["directCompetitors"][0]["domain"], "buildclub.ai")
        self.assertEqual(response.data["result"]["autofill"]["companyLinkedInUrl"], "https://www.linkedin.com/company/acme")
        self.assertEqual(response.data["result"]["autofill"]["researchDepth"]["linkedinSimilarSignals"], 1)

        run.refresh_from_db()
        self.assertEqual(run.status, ContentFactoryRunStatus.COMPLETED)
        self.assertTrue(ContentFactoryRunStep.objects.filter(run=run, step_key="resolve_company_identity").exists())
        self.assertTrue(ContentFactoryRunStep.objects.filter(run=run, step_key="generate_keyword_landscape").exists())

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

    def test_google_baseline_connection_requires_all_verified_data_scopes(self):
        connection = GoogleConnection.objects.create(
            user=self.user,
            google_email="founder@example.com",
            refresh_token="refresh-token",
            scope=GSC_SCOPE,
        )

        partial = google_baseline_connection_status(self.user)
        self.assertTrue(partial["connected"])
        self.assertFalse(partial["hasBaselineScopes"])
        self.assertEqual(partial["status"], "needs_reconnect")

        connection.scope = f"{GSC_SCOPE} {GA4_SCOPE}"
        connection.save(update_fields=["scope", "updated_at"])

        complete = google_baseline_connection_status(self.user)
        self.assertTrue(complete["hasBaselineScopes"])
        self.assertEqual(complete["status"], "connected")
