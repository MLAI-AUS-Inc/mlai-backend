"""P1.6 (IF-P1-08): a startup with no website still gets a copy-ready article.

A company saved without a domain has the ``startup-{pk}.invalid`` placeholder
organisation. For the new-journey cohort, with
``MY_STARTUP_NO_WEBSITE_ARTICLES_ENABLED`` on, the bootstrap is the real one,
portable drafts depend on the one-line description, GitHub and topic research
are refused before any state or dispatch, and the free first article is
delivered at a balance of 0. Every other cohort and every switch-off keeps
today's behaviour.
"""
from types import SimpleNamespace
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.test import TestCase, override_settings
from rest_framework.test import APIClient, APIRequestFactory

from integrations.services.article_generation import InsufficientRooPointsError
from roo.models import Ledger
from startup_updates.models import StartupProfile
from workflow_runs.models import ContentFactoryRun

from founder_tools.models import VibeRaisingCompany

from . import first_article_entitlement as first_article
from . import vibe_marketing_views as views
from .founder_journey_testing import ALL_P1_FLAGS_ON
from .no_website_capabilities import (NO_WEBSITE_CAPABILITY_REASON, NO_WEBSITE_GITHUB_REFUSAL, NO_WEBSITE_TOPICS_REFUSAL,
                                      applies, apply_no_website_capabilities)
from .service_views import ContentFactoryCallbackView, ContentFactoryRunView

COMPANIES_URL = "/api/v1/founder-tools/companies/"
BOOTSTRAP_URL = "/api/v1/vibe-marketing/bootstrap/"
FACADE_BOOTSTRAP_URL = "/api/v1/my-startup/vibe-marketing/bootstrap/"
ARTICLE_URL = "/api/v1/vibe-marketing/article/"
DISCOVERY_URL = "/api/v1/vibe-marketing/discovery/"
GITHUB_CONNECT_URL = "/api/v1/vibe-marketing/github/connect/"
AUTOFILL_URL = "/api/v1/vibe-marketing/autofill/"
BASELINE_URL = "/api/v1/vibe-marketing/baseline/"
NO_WEBSITE = {"createNew": True, "firstProfile": True, "name": "Ledger Mates",
              "shortDescription": "Bookkeeping for tradies"}
FREE = {"ARTICLES_FREE_FIRST_ARTICLE_ENABLED": True, "ARTICLES_FIRST_ARTICLE_GOAL_ENABLED": False,
        "ARTICLES_FREE_FIRST_ARTICLE_DAILY_CAP": 0, "ARTICLES_FREE_FIRST_ARTICLE_SPIKE_THRESHOLD": 25,
        "ROO_API_KEY": "synthetic-test-key", "INTERNAL_API_KEY": "synthetic-test-key"}
FLAGS_ON = {**ALL_P1_FLAGS_ON, **FREE}
WITHOUT_FLAG = {**FLAGS_ON, "MY_STARTUP_NO_WEBSITE_ARTICLES_ENABLED": False}
WITHOUT_V2 = {**FLAGS_ON, "MY_STARTUP_FOUNDER_JOURNEY_V2_ENABLED": False}
PORTABLE_START = {"topic": "How tradies keep their books tidy", "deliveryMode": "content_only",
                  "deliveryModeConfirmed": True, "deliveryModeExplicit": True, "expectedCostPoints": 0}


class _NoWebsiteBase:
    def setUp(self):
        super().setUp()
        self.enterContext(patch("founder_tools.services.reconcile_user_slack_id_from_email"))
        cache.clear()
        self.addCleanup(cache.clear)
        self.user = get_user_model().objects.create_user(email="ledger@synthetic.example.test", first_name="Ada",
                                                         last_name="Lovelace", role="participant")
        self.client = APIClient()
        self.client.force_authenticate(user=self.user)
        # Nothing on these paths may leave the backend: no site fetch, no
        # GitHub, no Content Factory research or DataForSEO-backed discovery.
        self.http = self.enterContext(patch.object(views, "http_client"))
        self.research = self.enterContext(patch.object(views, "start_article_research"))
        self.page_refresh = self.enterContext(patch.object(views, "_refresh_website_verification_for_page"))
        self.github_url = self.enterContext(patch.object(views, "build_github_auth_url"))

    def create_no_website_startup(self, body=NO_WEBSITE, key="press-1"):
        response = self.client.post(COMPANIES_URL, body, format="json", HTTP_IDEMPOTENCY_KEY=key)
        # The one-field create answers 201; today's create answers 200.
        self.assertIn(response.status_code, {200, 201}, response.data)
        company = VibeRaisingCompany.objects.select_related("organization").get(pk=response.data["id"])
        self.assertEqual(company.organization.domain, f"startup-{company.pk}.invalid")
        return company

    def assert_nothing_left_the_backend(self):
        self.http.post.assert_not_called()
        self.http.get.assert_not_called()
        self.http.request.assert_not_called()
        self.research.assert_not_called()
        self.page_refresh.assert_not_called()
        self.github_url.assert_not_called()


@override_settings(**FLAGS_ON)
class NoWebsiteBootstrapTests(_NoWebsiteBase, TestCase):
    def test_bootstrap_is_the_real_one_with_capabilities_and_cohort(self):
        company = self.create_no_website_startup()
        for url in (BOOTSTRAP_URL, FACADE_BOOTSTRAP_URL):
            for view in ("summary", "full"):
                with self.subTest(url=url, view=view):
                    response = self.client.get(url, {"company_id": str(company.pk), "view": view})
                    self.assertEqual(response.status_code, 200, response.data)
                    self.assertNotIn("Company domain is required.", str(response.data))
                    self.assertEqual(response.data["organization"]["id"], company.organization_id)
                    self.assertEqual(response.data["founderJourney"]["cohort"], "new_journey")
                    capabilities = response.data["articleCapabilities"]
                    self.assertTrue(capabilities["canGeneratePortableDraft"])
                    self.assertTrue(capabilities["canResearch"])
                    self.assertFalse(capabilities["canGenerateArticle"])
                    self.assertNotEqual(capabilities["nextStep"], "repository")
                    self.assert_no_github_step(capabilities)
                    self.assertEqual(capabilities["firstArticle"]["status"], "available")
                    self.assertEqual(capabilities["prices"]["article"], 0)
        self.assert_nothing_left_the_backend()

    def test_portable_drafts_need_the_one_line_description(self):
        company = self.create_no_website_startup()
        StartupProfile.objects.filter(organization=company.organization).update(short_description="")
        response = self.client.get(BOOTSTRAP_URL, {"company_id": str(company.pk)})
        self.assertEqual(response.status_code, 200, response.data)
        capabilities = response.data["articleCapabilities"]
        self.assertFalse(capabilities["canGeneratePortableDraft"])
        self.assertFalse(capabilities["canResearch"])
        self.assertNotEqual(capabilities["nextStep"], "repository")
        self.assert_no_github_step(capabilities)

    def assert_no_github_step(self, capabilities):
        """No stage, code or sentence sends this founder to GitHub."""
        self.assertNotIn(capabilities["stage"], {"github", "repository"})
        self.assertEqual((capabilities["reasonCode"], capabilities["publishingReasonCode"]), ("", ""))
        for key in ("reason", "publishingReason"):
            self.assertEqual(capabilities[key], NO_WEBSITE_CAPABILITY_REASON)
            self.assertNotIn("GitHub", capabilities[key])
            self.assertFalse({"\u2013", "\u2014"} & set(capabilities[key]))

    def test_capability_overrides_apply_only_to_new_journey_startups_without_a_website(self):
        company = self.create_no_website_startup()
        today = {"canResearch": True, "canGeneratePortableDraft": True, "canGenerateArticle": True,
                 "canPublishArticle": False, "nextStep": "repository", "stage": "github",
                 "reasonCode": "github_required", "reason": "Connect GitHub to get started.",
                 "publishingReasonCode": "github_required", "publishingReason": "Connect GitHub to get started."}
        changed = apply_no_website_capabilities(today, organization=company.organization)
        self.assertEqual(changed, {**today, "canGenerateArticle": False, "nextStep": "articles", "stage": "integration",
                                   "reasonCode": "", "reason": NO_WEBSITE_CAPABILITY_REASON,
                                   "publishingReasonCode": "", "publishingReason": NO_WEBSITE_CAPABILITY_REASON})
        self.assertEqual(today["nextStep"], "repository")  # never mutated in place
        with override_settings(MY_STARTUP_NO_WEBSITE_ARTICLES_ENABLED=False):
            self.assertIs(apply_no_website_capabilities(today, organization=company.organization), today)
        with override_settings(MY_STARTUP_FOUNDER_JOURNEY_V2_ENABLED=False):
            self.assertIs(apply_no_website_capabilities(today, organization=company.organization), today)

    def test_topics_and_github_are_refused_before_any_dispatch_or_state(self):
        company = self.create_no_website_startup()
        discovery = self.client.post(DISCOVERY_URL, {"company_id": str(company.pk), "keywords": ["books"]},
                                     format="json")
        self.assertEqual((discovery.status_code, discovery.data), (409, NO_WEBSITE_TOPICS_REFUSAL))
        github = self.client.post(GITHUB_CONNECT_URL, {"company_id": str(company.pk)}, format="json")
        self.assertEqual((github.status_code, github.data), (409, NO_WEBSITE_GITHUB_REFUSAL))
        self.assertEqual(NO_WEBSITE_GITHUB_REFUSAL, {"detail": "Add your website before connecting GitHub.",
                                                     "reason": "no_website"})
        # No GitHub authorization URL, so no OAuth state, was minted.
        self.github_url.assert_not_called()
        self.assert_nothing_left_the_backend()

    def test_no_autofill_discovery_baseline_or_dataforseo_request_leaves_the_backend(self):
        # DataForSEO is only reached through Content Factory, so blocking every
        # outbound HTTP request from any module proves none of them is made.
        company = self.create_no_website_startup()
        scope = {"company_id": str(company.pk)}
        with patch("requests.sessions.Session.request") as outbound, \
             patch.object(views, "_queue_content_factory_run") as queued:
            for view in ("summary", "full"):
                self.assertEqual(self.client.get(BOOTSTRAP_URL, {**scope, "view": view}).status_code, 200)
            topics = self.client.post(DISCOVERY_URL, {**scope, "keywords": ["books"]}, format="json")
            autofill = self.client.post(AUTOFILL_URL, {**scope, "companyName": "Ledger Mates"}, format="json")
            baseline = self.client.post(BASELINE_URL, scope, format="json")
        self.assertEqual((topics.status_code, topics.data["reason"]), (409, "no_website"))
        self.assertEqual((autofill.status_code, autofill.data["reason"]), (409, "no_website"))
        # The baseline keeps today's domain refusal: there is no site to measure.
        self.assertEqual((baseline.status_code, baseline.data), (400, {"detail": "Company domain is required."}))
        outbound.assert_not_called()
        queued.assert_not_called()
        self.assertFalse(ContentFactoryRun.objects.filter(organization=company.organization).exists())
        self.assert_nothing_left_the_backend()


@override_settings(**FLAGS_ON)
class NoWebsiteFirstArticleTests(_NoWebsiteBase, TestCase):
    """P1.6 acceptance: a startup saved with no domain gets a delivered draft at balance 0."""

    def setUp(self):
        super().setUp()
        self.enterContext(patch.object(views, "get_content_factory_article_cost_points", return_value=12))
        # The audience approval the one-press start makes (P0 IF-08, be-journey)
        # is outside this package; the entitlement fixture stubs it the same way.
        self.enterContext(patch.object(views, "_refresh_article_editorial_payload", return_value=None))
        self.enterContext(patch.object(views, "_validate_article_start_admission"))
        self.enterContext(patch.object(views, "_roo_points_balance_for_user", return_value=0))
        self.debit = self.enterContext(patch.object(views, "charge_content_factory_request_for_user",
            side_effect=InsufficientRooPointsError({"requiredPoints": 12, "currentBalance": 0})))
        self.enterContext(patch.object(first_article, "_queue_alert"))
        self.enterContext(patch.object(views, "founder_actor_id_for_user", return_value="mlai_user:ledger"))
        self.enterContext(patch.object(views, "_content_factory_remote_config", return_value={
            "enabled": True, "base_url": "https://worker.invalid", "api_key_configured": True, "is_local_env": True}))
        self.enterContext(patch.object(views, "_content_factory_headers", return_value={}))
        self.enterContext(patch.object(views, "_lookup_content_factory_dispatch_by_key", return_value=("pending", {})))
        from requests import RequestException
        self.http.RequestException = RequestException
        self.sent = []

        def worker_accepts(url, json=None, **kwargs):
            self.sent.append((url, json))
            return SimpleNamespace(status_code=202, content=b"fixture",
                                   json=lambda: {"run_id": json["client_request_id"], "status": "queued"})

        self.http.post.side_effect = worker_accepts

    def worker(self, method, view, payload, **kwargs):
        request = getattr(APIRequestFactory(), method)("/synthetic", payload, format="json",
                                                       HTTP_X_API_KEY="synthetic-test-key")
        return view.as_view()(request, **kwargs)

    def test_a_startup_without_a_website_gets_a_delivered_free_draft(self):
        company = self.create_no_website_startup()
        organization = company.organization
        response = self.client.post(f"{ARTICLE_URL}?company_id={company.pk}", PORTABLE_START, format="json")
        self.assertEqual(response.status_code, 202, response.data)
        self.debit.assert_not_called()
        url, sent = self.sent[-1]
        self.assertTrue(url.endswith("/api/runs/article"))
        self.assertEqual((sent["delivery_mode"], sent["github_repo"]), ("content_only", ""))
        self.assertEqual(sent["domain"], organization.domain)
        self.assertEqual(sent["roo_points_billing_status"], "entitled")
        run = ContentFactoryRun.objects.get(run_id=response.data["runId"])
        self.assertEqual(run.organization_id, organization.pk)

        # Content Factory delivers the article with the existing worker seams.
        package = {"title": "How tradies keep their books tidy", "article_json": {"title": "Tidy books"}}
        update = self.worker("put", ContentFactoryRunView, {
            "workflow": run.workflow, "domain": organization.domain, "status": "completed",
            "current_step": "finalize", "result": {"content_package": package}}, run_id=run.run_id)
        self.assertEqual(update.status_code, 200, update.data)
        with patch("content_factory.service_views.upsert_live_progress_card"):
            callback = self.worker("post", ContentFactoryCallbackView, {
                "job_id": run.run_id, "domain": organization.domain, "event_type": "content_ready",
                "slack_user_id": "", "content_package": package})
        self.assertEqual(callback.status_code, 200, callback.data)
        run.refresh_from_db()
        self.assertEqual(run.status, "completed")
        state = first_article.entitlement_state(organization, self.user)
        self.assertEqual((state["status"], state["run_id"]), ("used", run.run_id))
        self.assertFalse(Ledger.objects.filter(kind="SPEND").exists())
        self.debit.assert_not_called()
        self.research.assert_not_called()

    def test_without_the_switches_today_a_no_website_start_needs_a_domain(self):
        company = self.create_no_website_startup()
        for name, flags in (("no-website flag off", WITHOUT_FLAG), ("V2 off", WITHOUT_V2)):
            with self.subTest(name), override_settings(**flags):
                start = self.client.post(f"{ARTICLE_URL}?company_id={company.pk}", PORTABLE_START, format="json")
                self.assertEqual((start.status_code, start.data), (400, {"detail": "Company domain is required."}))
                bootstrap = self.client.get(BOOTSTRAP_URL, {"company_id": str(company.pk)})
                self.assertEqual(bootstrap.status_code, 200)
                # Today's no-domain bootstrap: closed capability gates.
                self.assertFalse(bootstrap.data["articleCapabilities"]["canGeneratePortableDraft"])
                self.assertEqual(bootstrap.data["settings"]["githubConnectionState"], "missing_domain")
                self.assertFalse(applies(company.organization))
        self.http.post.assert_not_called()


@override_settings(**FLAGS_ON)
class NoWebsiteLegacyTests(_NoWebsiteBase, TestCase):
    """A no-website startup in a legacy organisation keeps today's behaviour with every flag on."""

    def legacy_company(self):
        # Made the legacy way, before launch: today's create, no marker, and an
        # organisation older than LAUNCHED_AT.
        with override_settings(MY_STARTUP_ONE_FIELD_START_ENABLED=False):
            company = self.create_no_website_startup({"createNew": True, "name": "Old Ledger",
                                                      "shortDescription": "Bookkeeping for tradies"})
        from organizations.models import Organization
        from .founder_journey_cohort import cohort
        from .founder_journey_testing import LEGACY_CREATED_AT
        Organization.objects.filter(pk=company.organization_id).update(created_at=LEGACY_CREATED_AT)
        company.organization.refresh_from_db()
        self.assertEqual(cohort(company.organization), "legacy")
        return company

    def test_legacy_no_website_startup_keeps_todays_bootstrap_and_refusals(self):
        company = self.legacy_company()
        self.assertFalse(applies(company.organization))
        bootstrap = self.client.get(BOOTSTRAP_URL, {"company_id": str(company.pk)})
        self.assertEqual(bootstrap.status_code, 200)
        self.assertEqual(bootstrap.data["settings"]["githubConnectionState"], "missing_domain")
        self.assertFalse(bootstrap.data["articleCapabilities"]["canGeneratePortableDraft"])
        self.assertEqual(bootstrap.data["founderJourney"]["cohort"], "legacy")
        for url, body in ((ARTICLE_URL, PORTABLE_START), (DISCOVERY_URL, {"keywords": ["books"]}),
                          (GITHUB_CONNECT_URL, {})):
            with self.subTest(url=url):
                response = self.client.post(f"{url}?company_id={company.pk}", body, format="json")
                self.assertEqual((response.status_code, response.data), (400, {"detail": "Company domain is required."}))
        self.assert_nothing_left_the_backend()
