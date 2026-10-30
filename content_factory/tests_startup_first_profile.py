"""P1.5 and P1.6 (IF-P1-07): first-profile research that never comes back empty.

Gated by MY_STARTUP_ONE_FIELD_START_ENABLED and is_new_journey(org). Every
outbound call (CF dispatch, the homepage fetch, the topic batch) is mocked.
"""
from datetime import timedelta
from unittest.mock import patch

from django.test import TestCase, override_settings
from django.utils import timezone
from rest_framework.response import Response
from rest_framework.test import APIClient

from content_factory import startup_first_profile as first_profile
from content_factory.founder_journey_cohort import FIRST_PROFILE_KEY
from content_factory.founder_journey_testing import ALL_P1_FLAGS_ON, make_named_legacy_org, make_new_journey_org
from content_factory.models import OrganizationContentConfig
from content_factory.website_contract import WebsiteAuthorityError
from founder_tools.models import VibeRaisingCompany
from founder_tools.profile_fields import company_avatar_url
from startup_updates.models import StartupProfile
from workflow_runs.models import ContentFactoryRun

AUTOFILL_URL = "/api/v1/vibe-marketing/autofill/"
VIEWS = "content_factory.vibe_marketing_views"
FETCH = "content_factory.website_live_fetch.fetch_live_route"
FLAGS = {**ALL_P1_FLAGS_ON, "ARTICLES_FREE_SETUP_RESEARCH_ENABLED": True}
WITHOUT_ONE_FIELD = {**FLAGS, "MY_STARTUP_ONE_FIELD_START_ENABLED": False}
WITHOUT_V2 = {**FLAGS, "MY_STARTUP_FOUNDER_JOURNEY_V2_ENABLED": False}
HOMEPAGE = b"""<html><head><title>Banksia Studio | Garden design in Melbourne</title>
<meta name="description" content="Native garden design for Melbourne homes.">
<meta property="og:site_name" content="Banksia Studio">
<meta property="og:image" content="/images/banksia-og.png">
<link rel="shortcut icon" href="https://banksia.studio/favicon.ico">
</head><body>Hi</body></html>"""
AUTOFILL = {
    "brandName": "Banksia Studio",
    "profileFields": {"shortDescription": "Banksia designs native gardens for Melbourne homes.",
                      "targetAudience": "Melbourne homeowners who want a native garden",
                      "problemSolved": "Gardens that need too much water."},
    "seedKeywords": ["native garden design", {"keyword": "drought tolerant plants"}],
    "researchQuality": {"status": "complete"},
    "siteProfile": {
        "platform": "Squarespace", "blogUrl": "https://banksia.studio/journal",
        "existingPosts": [{"url": "https://banksia.studio/journal/first", "title": "Our first garden"},
                          {"url": "http://banksia.studio/insecure", "title": "Dropped"}],
        "logoUrl": "https://banksia.studio/logo.png",
        "primaryCallToAction": {"label": "Book a consult", "url": "https://banksia.studio/book"},
        "homepage": {"title": "Banksia Studio", "description": "Native gardens."},
    },
}


class _Base:
    def setUp(self):
        patcher = patch("founder_tools.services.reconcile_user_slack_id_from_email")
        patcher.start()
        self.addCleanup(patcher.stop)
        self.built = make_new_journey_org("banksia.studio")
        self.company = self.built.company
        self.org = self.built.organization
        self.client = APIClient()
        self.client.force_authenticate(user=self.built.owner)
        self.dispatched = []

    # -- helpers -------------------------------------------------------
    def fake_queue(self, *, endpoint, workflow, context, config, payload, **_kwargs):
        self.dispatched.append(dict(payload))
        return ContentFactoryRun.objects.create(
            run_id=f"autofill-{len(self.dispatched)}-{self.company.pk.hex[:6]}", workflow=workflow,
            domain=context.organization.domain, organization=context.organization, status="queued",
            run_request=dict(payload))

    def autofill(self, body=None, *, company=None, client=None, fetch=None):
        company = company or self.company
        payload = {"companyId": str(company.pk), "domain": company.domain, "draftOnly": True,
                   "draftMode": True, "researchDepth": "first_profile", **(body or {})}
        fetch = fetch if fetch is not None else patch(FETCH, return_value=(HOMEPAGE, {}))
        with patch(f"{VIEWS}._require_roo_points_for_ai_agent", return_value=(None, 100)), \
                patch(f"{VIEWS}._queue_content_factory_run", side_effect=self.fake_queue), fetch as fetcher:
            response = (client or self.client).post(AUTOFILL_URL, payload, format="json")
        return response, fetcher

    def state(self, org=None):
        config = OrganizationContentConfig.objects.get(organization=org or self.org)
        return (config.pillar_strategy or {}).get(FIRST_PROFILE_KEY) or {}

    def set_state(self, **values):
        config = OrganizationContentConfig.objects.get(organization=self.org)
        strategy = dict(config.pillar_strategy or {})
        strategy[FIRST_PROFILE_KEY] = {**first_profile._empty_state(), **strategy.get(FIRST_PROFILE_KEY, {}), **values}
        config.pillar_strategy = strategy
        config.save()

    def make_run(self, *, status="completed", result=None, run_id="fp-run-1", created_at=None):
        run = ContentFactoryRun.objects.create(
            run_id=run_id, workflow="startup_autofill", domain=self.org.domain, organization=self.org,
            status=status, run_request={"company_id": str(self.company.pk), "domain": self.org.domain,
                                        "research_depth": "first_profile", "client_request_id": "first-profile:x:1"},
            result=result if result is not None else {"autofill": AUTOFILL})
        if created_at is not None:
            ContentFactoryRun.objects.filter(pk=run.pk).update(created_at=created_at)
            run.refresh_from_db()
        return run

    def apply(self, run, **kwargs):
        with patch(f"{VIEWS}.start_article_research",
                   return_value=Response({"runId": "topics-1"}, status=202)) as topics:
            result = first_profile.apply_first_profile(run, **kwargs)
        return result, topics


@override_settings(**FLAGS)
class FirstProfileAutofillRequestTests(_Base, TestCase):
    def test_cf_payload_is_budgeted_draft_mode_with_a_fresh_request_id_per_attempt(self):
        response, _ = self.autofill()
        self.assertEqual(response.status_code, 202, response.data)
        ContentFactoryRun.objects.filter(workflow="startup_autofill").update(status="completed")
        second, _ = self.autofill()
        self.assertEqual(second.status_code, 202, second.data)
        first_payload, second_payload = self.dispatched
        for payload in self.dispatched:
            self.assertEqual(payload["research_depth"], "first_profile")
            self.assertEqual(payload["budget_seconds"], 90)
            self.assertIs(payload["draft_mode"], True)
            self.assertTrue(payload["client_request_id"].startswith(f"first-profile:{self.company.pk}:"))
            self.assertNotIn("delivery_mode_confirmed", payload)
        self.assertNotEqual(first_payload["client_request_id"], second_payload["client_request_id"])

    def test_company_name_and_location_are_not_required_and_no_workspace_is_made(self):
        before = VibeRaisingCompany.objects.count()
        response, _ = self.autofill({"domain": ""})
        self.assertEqual(response.status_code, 202, response.data)
        self.assertEqual(VibeRaisingCompany.objects.count(), before)
        self.assertEqual(self.dispatched[0]["company_id"], str(self.company.pk))
        self.assertEqual(self.dispatched[0]["company_name"], self.company.name)

    def test_homepage_meta_is_stored_before_dispatch(self):
        response, fetcher = self.autofill()
        self.assertEqual(response.status_code, 202)
        fetcher.assert_called_once_with("https://banksia.studio/", "banksia.studio")
        state = self.state()
        self.assertEqual(state["homepageMeta"], {
            "title": "Banksia Studio | Garden design in Melbourne",
            "description": "Native garden design for Melbourne homes.",
            "ogSiteName": "Banksia Studio", "ogImage": "https://banksia.studio/images/banksia-og.png",
            "icon": "https://banksia.studio/favicon.ico"})
        self.assertEqual(state["runId"], response.data["runId"])

    def test_homepage_fetch_errors_are_swallowed(self):
        failing = patch(FETCH, side_effect=WebsiteAuthorityError("live_route_unavailable", "down"))
        response, _ = self.autofill(fetch=failing)
        self.assertEqual(response.status_code, 202, response.data)
        self.assertIsNone(self.state().get("homepageMeta"))
        self.assertEqual(len(self.dispatched), 1)

    def make_no_website(self):
        self.org.domain = f"startup-{self.company.pk}.invalid"
        self.org.save()
        VibeRaisingCompany.objects.filter(pk=self.company.pk).update(domain="")
        self.company.refresh_from_db()

    def test_a_no_website_org_is_refused_before_dispatch(self):
        self.make_no_website()
        for domain in ("", f"startup-{self.company.pk}.invalid"):
            with self.subTest(domain=domain):
                response, fetcher = self.autofill({"domain": domain})
                self.assertEqual(response.status_code, 409)
                self.assertEqual(response.data, {"detail": "Add your website first.", "reason": "no_website"})
                self.assertEqual(self.dispatched, [])
                fetcher.assert_not_called()

    def test_a_no_website_org_that_brings_its_website_attaches_and_researches_it(self):
        # The legacy company-setup page posts the typed website for the active
        # company: that request runs today's attach-and-research path.
        self.make_no_website()
        body = {"companyId": str(self.company.pk), "companyName": "Banksia Studio",
                "domain": "https://banksia.studio/", "location": "Melbourne"}
        with patch(f"{VIEWS}._require_roo_points_for_ai_agent", return_value=(None, 100)), \
                patch(f"{VIEWS}._queue_content_factory_run", side_effect=self.fake_queue), \
                patch(FETCH, return_value=(HOMEPAGE, {})):
            response = self.client.post(AUTOFILL_URL, body, format="json")
        self.assertEqual(response.status_code, 202, response.data)
        self.assertEqual(len(self.dispatched), 1)
        self.assertNotEqual(self.dispatched[0].get("research_depth"), "first_profile")
        self.company.refresh_from_db()
        self.assertEqual(self.company.domain, "banksia.studio")

    def test_the_no_website_refusal_needs_its_own_flag(self):
        self.make_no_website()
        with override_settings(MY_STARTUP_NO_WEBSITE_ARTICLES_ENABLED=False):
            response, fetcher = self.autofill({"domain": ""})
        # Today's 400 for a request with no website, not the new refusal.
        self.assertEqual(response.status_code, 400)
        self.assertNotIn("reason", response.data)
        self.assertEqual(self.dispatched, [])
        fetcher.assert_not_called()

    def test_a_new_journey_draft_drops_an_unusable_linkedin(self):
        response, _ = self.autofill({"companyLinkedInUrl": "not a linkedin page"})
        self.assertEqual(response.status_code, 202, response.data)
        self.assertEqual(self.dispatched[-1]["existing_fields"]["companyLinkedInUrl"], "")
        self.assertEqual(self.dispatched[-1]["company_linkedin_url"], "")
        ContentFactoryRun.objects.filter(workflow="startup_autofill").update(status="completed")
        bare, _ = self.autofill({"companyLinkedInUrl": "linkedin.com/company/x"})
        self.assertEqual(bare.status_code, 202, bare.data)
        self.assertEqual(self.dispatched[-1]["existing_fields"]["companyLinkedInUrl"],
                         "https://www.linkedin.com/company/x")


class LegacyAutofillRequestTests(_Base, TestCase):
    """researchDepth is ignored, and nothing new runs, for the legacy cohort or with a flag off."""

    def assert_today(self, response, fetcher):
        self.assertEqual(response.status_code, 202, response.data)
        payload = self.dispatched[-1]
        self.assertEqual(payload["research_depth"], "deep")
        self.assertNotIn("budget_seconds", payload)
        fetcher.assert_not_called()

    def test_talathrive_with_every_flag_on(self):
        self.assert_named_legacy_org_today("talathrive")

    def test_pawshare_with_every_flag_on(self):
        self.assert_named_legacy_org_today("pawshare")

    def assert_named_legacy_org_today(self, name):
        with override_settings(**FLAGS):
            built = make_named_legacy_org(name)
            client = APIClient()
            client.force_authenticate(user=built.owner)
            # Today a legacy autofill still needs the company name.
            nameless, _ = self.autofill(company=built.company, client=client)
            self.assertEqual(nameless.status_code, 400)
            response, fetcher = self.autofill({"companyName": built.company.name},
                                              company=built.company, client=client)
            self.assert_today(response, fetcher)
            config = OrganizationContentConfig.objects.get(organization=built.organization)
            self.assertNotIn(FIRST_PROFILE_KEY, config.pillar_strategy)
            ContentFactoryRun.objects.filter(workflow="startup_autofill").update(status="completed")
            invalid, _ = self.autofill({"companyName": built.company.name,
                                        "companyLinkedInUrl": "not a linkedin page"},
                                       company=built.company, client=client)
            self.assertEqual(invalid.status_code, 400)

    def test_a_new_journey_org_with_a_switch_off(self):
        for label, flags in (("one-field off", WITHOUT_ONE_FIELD), ("V2 off", WITHOUT_V2)):
            with self.subTest(label), override_settings(**flags):
                ContentFactoryRun.objects.filter(workflow="startup_autofill").update(status="completed")
                self.assertEqual(self.autofill()[0].status_code, 400)  # today: a name is required
                response, fetcher = self.autofill({"companyName": self.company.name})
                self.assert_today(response, fetcher)
                self.assertEqual(self.state(), {})


@override_settings(**FLAGS)
class ApplyFirstProfileTests(_Base, TestCase):
    def test_fills_empty_fields_logo_seeds_and_site_profile(self):
        self.set_state(derivedName="New journey", runId="fp-run-1")
        config = OrganizationContentConfig.objects.get(organization=self.org)
        article_system, tech_stack = dict(config.article_system or {}), dict(config.tech_stack or {})
        result, _ = self.apply(self.make_run())
        self.assertEqual(result["status"], "applied", result)
        self.assertFalse(result["fallback"])
        self.company.refresh_from_db()
        self.org.refresh_from_db()
        self.assertEqual(self.company.name, "Banksia Studio")
        self.assertEqual(self.org.name, "Banksia Studio")
        profile = StartupProfile.objects.get(organization=self.org)
        self.assertEqual(profile.short_description, "Banksia designs native gardens for Melbourne homes.")
        self.assertEqual(profile.target_audience, "Melbourne homeowners who want a native garden")
        self.assertEqual(profile.problem_solved, "Gardens that need too much water.")
        self.assertEqual(company_avatar_url(self.company), "https://banksia.studio/logo.png")
        self.assertEqual(self.org.seed_keywords, ["native garden design", "drought tolerant plants"])
        state = self.state()
        self.assertEqual(state["siteProfile"]["platform"], "squarespace")
        self.assertEqual(state["siteProfile"]["blogUrl"], "https://banksia.studio/journal")
        self.assertEqual(state["siteProfile"]["existingPosts"],
                         [{"url": "https://banksia.studio/journal/first", "title": "Our first garden"}])
        self.assertEqual(state["siteProfile"]["primaryCallToAction"],
                         {"label": "Book a consult", "url": "https://banksia.studio/book"})
        self.assertEqual(state["appliedRunId"], "fp-run-1")
        config.refresh_from_db()
        self.assertEqual(config.article_system or {}, article_system)
        self.assertEqual(config.tech_stack or {}, tech_stack)

    def test_never_overwrites_what_the_founder_wrote(self):
        self.set_state(derivedName="Banksia", runId="fp-run-1")
        profile = StartupProfile.objects.get_or_create(organization=self.org)[0]
        profile.short_description = "My own words."
        profile.save()
        self.company.name = "Banksia Gardens Co"  # typed, not the derived name
        self.company.save()
        self.apply(self.make_run())
        self.company.refresh_from_db()
        profile.refresh_from_db()
        self.assertEqual(self.company.name, "Banksia Gardens Co")
        self.assertEqual(profile.short_description, "My own words.")
        self.assertEqual(profile.target_audience, "Melbourne homeowners who want a native garden")

    def test_runs_once_per_org_and_the_sweep_does_nothing_after(self):
        self.set_state(runId="fp-run-1")
        run = self.make_run()
        first, topics = self.apply(run)
        self.assertEqual(first["status"], "applied")
        self.assertEqual(topics.call_count, 1)
        again, topics_again = self.apply(run)
        self.assertEqual(again["status"], "already_applied")
        topics_again.assert_not_called()
        with patch(f"{VIEWS}.start_article_research") as swept_topics:
            self.assertEqual(first_profile.sweep()["applied"], 0)
        swept_topics.assert_not_called()

    def test_homepage_fallback_fills_name_and_one_liner_when_research_fails(self):
        self.set_state(derivedName="New journey", runId="fp-run-1", homepageMeta={
            "title": "Banksia Studio | Garden design", "description": "Native garden design for Melbourne homes.",
            "ogSiteName": None, "ogImage": "https://banksia.studio/og.png", "icon": None})
        result, _ = self.apply(self.make_run(status="failed", result={"error": "Research could not finish."}))
        self.assertEqual(result["status"], "applied")
        self.assertTrue(result["fallback"])
        self.company.refresh_from_db()
        self.assertEqual(self.company.name, "Banksia Studio")
        self.assertEqual(StartupProfile.objects.get(organization=self.org).short_description,
                         "Native garden design for Melbourne homes.")
        self.assertEqual(company_avatar_url(self.company), "https://banksia.studio/og.png")

    def test_a_fallback_result_without_a_one_liner_uses_the_homepage(self):
        self.set_state(runId="fp-run-1", homepageMeta={"title": "Banksia", "description": "Gardens that thrive.",
                                                       "ogSiteName": None, "ogImage": None, "icon": None})
        self.apply(self.make_run(result={"autofill": {"researchQuality": {"status": "fallback", "partial": True},
                                                 "profileFields": {"targetAudience": "Gardeners"}}}))
        self.assertEqual(StartupProfile.objects.get(organization=self.org).short_description, "Gardens that thrive.")

    def test_one_retry_for_a_transient_failure_outside_the_free_setup_cap(self):
        self.set_state(runId="fp-run-1", homepageMeta={"title": "Banksia Studio", "description": "Native gardens.",
                                                       "ogSiteName": None, "ogImage": None, "icon": None})
        failed = self.make_run(status="failed", result={"error": "Model busy", "retryable": True})
        with patch(f"{VIEWS}._queue_content_factory_run", side_effect=self.fake_queue), \
                patch("content_factory.billing.record_free_setup_use") as recorded:
            result, topics = self.apply(failed)
        self.assertEqual(result["status"], "retried")
        recorded.assert_not_called()
        topics.assert_not_called()
        [retry_payload] = self.dispatched
        self.assertNotEqual(retry_payload["client_request_id"], failed.run_request["client_request_id"])
        state = self.state()
        self.assertEqual(state["retryCount"], 1)
        self.assertEqual(state["runId"], result["runId"])
        self.assertIsNone(state["appliedRunId"])
        # The founder already sees a name and a one-liner while the retry runs.
        self.assertEqual(StartupProfile.objects.get(organization=self.org).short_description, "Native gardens.")
        # The retry's result may replace what the homepage filled.
        retry = ContentFactoryRun.objects.get(run_id=result["runId"])
        retry.status, retry.result = "completed", {"autofill": AUTOFILL}
        retry.save()
        final, _ = self.apply(retry)
        self.assertEqual(final["status"], "applied")
        self.assertEqual(StartupProfile.objects.get(organization=self.org).short_description,
                         "Banksia designs native gardens for Melbourne homes.")

    def test_never_more_than_one_retry(self):
        self.set_state(runId="fp-run-2", retryCount=1)
        with patch(f"{VIEWS}._queue_content_factory_run", side_effect=self.fake_queue):
            result, _ = self.apply(self.make_run(status="failed", run_id="fp-run-2",
                                            result={"error": "Model busy", "retryable": True}))
        self.assertEqual(result["status"], "applied")
        self.assertEqual(self.dispatched, [])

    def test_a_draft_audience_is_seeded_without_activation_or_an_offer(self):
        self.set_state(runId="fp-run-1")
        result, _ = self.apply(self.make_run())
        config = OrganizationContentConfig.objects.get(organization=self.org)
        catalog = config.pillar_strategy["editorial_catalog"]
        [audience] = catalog["audience_options"]
        self.assertEqual(audience["status"], "draft")
        self.assertIsNone(audience["approved_by"])
        self.assertTrue(audience["allow_no_offer"])
        self.assertEqual(audience["description"], "Melbourne homeowners who want a native garden")
        self.assertEqual(catalog.get("cta_options", []), [])
        self.assertEqual(catalog["approval_receipts"].get("audience", {}), {})
        self.assertEqual(result["audienceId"], audience["id"])
        self.assertEqual(self.state()["draftAudienceId"], audience["id"])

    def test_the_free_topic_batch_starts_once_and_never_at_a_price(self):
        self.set_state(runId="fp-run-1")
        result, topics = self.apply(self.make_run())
        topics.assert_called_once()
        data = topics.call_args.kwargs["data"]
        self.assertEqual(data["expectedCostPoints"], 0)
        self.assertEqual(data["requestedTopicCount"], 4)
        self.assertEqual(result["topicBatchRunId"], "topics-1")
        self.assertEqual(self.state()["topicBatchRunId"], "topics-1")

    @override_settings(ARTICLES_FREE_SETUP_RESEARCH_ENABLED=False)
    def test_no_topic_batch_without_free_setup_research(self):
        self.set_state(runId="fp-run-1")
        result, topics = self.apply(self.make_run())
        topics.assert_not_called()
        self.assertIsNone(result["topicBatchRunId"])

    def test_every_new_org_ends_up_with_seed_keywords(self):
        self.set_state(runId="fp-run-1")
        self.apply(self.make_run(status="failed", result={}))
        self.org.refresh_from_db()
        self.assertTrue(self.org.seed_keywords)
        self.assertTrue(all(isinstance(item, str) and item for item in self.org.seed_keywords))

    def test_a_pending_or_superseded_run_does_nothing(self):
        self.set_state(runId="fp-run-1")
        self.assertEqual(self.apply(self.make_run(status="running"))[0]["status"], "pending")
        self.set_state(runId="another-run")
        self.assertEqual(self.apply(self.make_run(run_id="fp-run-3"))[0]["status"], "superseded")


@override_settings(**FLAGS)
class PollAndSweepTests(_Base, TestCase):
    def test_the_run_poll_applies_a_terminal_first_profile_run(self):
        self.set_state(runId="fp-run-1")
        run = self.make_run()
        with patch(f"{VIEWS}._call_content_factory_run_status", return_value={}), \
                patch(f"{VIEWS}.start_article_research", return_value=Response({"runId": "t"}, status=202)):
            response = self.client.get(f"/api/v1/vibe-marketing/runs/{run.run_id}/",
                                       {"company_id": str(self.company.pk), "view": "status"})
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(self.state()["appliedRunId"], run.run_id)

    def test_the_sweep_syncs_a_running_run_and_applies_it(self):
        run = self.make_run(status="running", result={})
        self.set_state(runId=run.run_id, startedAt=timezone.now().isoformat())
        remote = {"run_id": run.run_id, "workflow": "startup_autofill", "status": "completed",
                  "result": {"status": "completed", "autofill": AUTOFILL}}
        with patch(f"{VIEWS}._call_content_factory_run_status", return_value=remote), \
                patch(f"{VIEWS}.start_article_research", return_value=Response({"runId": "t"}, status=202)):
            summary = first_profile.sweep()
        self.assertEqual(summary["applied"], 1, summary)
        self.assertEqual(StartupProfile.objects.get(organization=self.org).target_audience,
                         "Melbourne homeowners who want a native garden")

    def test_the_sweep_finishes_a_stuck_run_with_the_fallback(self):
        long_ago = timezone.now() - timedelta(minutes=20)
        run = self.make_run(status="running", result={}, created_at=long_ago)
        self.set_state(runId=run.run_id, startedAt=long_ago.isoformat(),
                       homepageMeta={"title": "Banksia Studio", "description": "Native gardens.",
                                     "ogSiteName": None, "ogImage": None, "icon": None})
        with patch(f"{VIEWS}._call_content_factory_run_status", return_value={}), \
                patch(f"{VIEWS}.start_article_research", return_value=Response({"runId": "t"}, status=202)):
            summary = first_profile.sweep()
        self.assertEqual(summary["applied"], 1, summary)
        self.assertEqual(StartupProfile.objects.get(organization=self.org).short_description, "Native gardens.")

    def test_the_sweep_never_reads_configs_outside_its_window(self):
        run = self.make_run(status="completed")
        self.set_state(runId=run.run_id, startedAt=timezone.now().isoformat())
        OrganizationContentConfig.objects.filter(organization=self.org).update(
            updated_at=timezone.now() - timedelta(hours=25))
        with patch.object(first_profile, "read_state", wraps=first_profile.read_state) as read:
            summary = first_profile.sweep()
        self.assertEqual(summary["checked"], 0, summary)
        read.assert_not_called()
        self.assertIsNone(self.state().get("appliedRunId"))

    def test_the_sweep_scan_is_bounded(self):
        self.set_state(appliedRunId="done", runId="done")
        with patch.object(first_profile, "SWEEP_SCAN_LIMIT", 0), \
                patch.object(first_profile, "read_state", wraps=first_profile.read_state) as read:
            first_profile.sweep()
        read.assert_not_called()

    def test_the_founder_journey_runner_finds_the_sweep(self):
        from content_factory.founder_journey_sweeps import run_founder_journey_sweeps
        result = run_founder_journey_sweeps(sweeps=("content_factory.startup_first_profile:sweep",))
        self.assertEqual(result["sweeps"]["content_factory.startup_first_profile:sweep"], "completed")


class GateTests(_Base, TestCase):
    def test_disabled_without_the_flag_and_skipped_for_legacy_orgs(self):
        self.set_state(runId="fp-run-1")
        run = self.make_run()
        with override_settings(**WITHOUT_ONE_FIELD):
            self.assertEqual(first_profile.apply_first_profile(run)["status"], "disabled")
            self.assertEqual(first_profile.sweep(), {"status": "disabled"})
        with override_settings(**WITHOUT_V2):
            self.assertEqual(first_profile.apply_first_profile(run)["status"], "skipped")
        legacy = make_named_legacy_org("vyavos")
        legacy_run = ContentFactoryRun.objects.create(
            run_id="legacy-fp", workflow="startup_autofill", domain=legacy.organization.domain,
            organization=legacy.organization, status="completed",
            run_request={"company_id": str(legacy.company.pk), "research_depth": "first_profile"},
            result={"autofill": AUTOFILL})
        with override_settings(**FLAGS):
            self.assertEqual(first_profile.apply_first_profile(legacy_run)["status"], "skipped")
        legacy.organization.refresh_from_db()
        self.assertNotEqual(legacy.organization.seed_keywords, ["native garden design", "drought tolerant plants"])
        config = OrganizationContentConfig.objects.get(organization=legacy.organization)
        self.assertNotIn(FIRST_PROFILE_KEY, config.pillar_strategy)
        self.assertEqual(self.state().get("appliedRunId"), None)


class PureHelperTests(TestCase):
    def test_parse_homepage_meta(self):
        meta = first_profile.parse_homepage_meta(HOMEPAGE, "banksia.studio")
        self.assertEqual(meta["ogImage"], "https://banksia.studio/images/banksia-og.png")
        self.assertIsNone(first_profile.parse_homepage_meta(b"<html><body>none</body></html>", "x.test"))
        unsafe = first_profile.parse_homepage_meta(
            b'<meta property="og:image" content="javascript:alert(1)"><title>T</title>', "x.test")
        self.assertIsNone(unsafe["ogImage"])

    def test_site_profile_is_bounded(self):
        profile = first_profile.site_profile_from_autofill({"siteProfile": {
            "platform": "MySpace", "existingPosts": [{"url": f"https://x.test/{i}", "title": "t"} for i in range(80)],
            "primaryCallToAction": {"label": "Go", "url": "http://x.test"}}})
        self.assertEqual(profile["platform"], "unknown")
        self.assertEqual(len(profile["existingPosts"]), 50)
        self.assertIsNone(profile["primaryCallToAction"])
        self.assertIsNone(first_profile.site_profile_from_autofill({}))

    def test_fallback_seed_keywords(self):
        seeds = first_profile.fallback_seed_keywords(name="Ledger Mates", description="Bookkeeping for tradies")
        self.assertEqual(seeds[0], "ledger mates")
        self.assertIn("bookkeeping tradies", seeds)
        self.assertEqual(first_profile.fallback_seed_keywords(domain="banksia.studio"), ["banksia"])

    def test_homepage_meta_gives_up_after_the_budget(self):
        import time

        def slow(*_args, **_kwargs):
            time.sleep(0.3)
            return HOMEPAGE, {}
        with patch(FETCH, side_effect=slow), patch.object(first_profile, "HOMEPAGE_FETCH_SECONDS", 0.05):
            self.assertIsNone(first_profile.homepage_meta("banksia.studio"))
        self.assertIsNone(first_profile.homepage_meta("startup-1.invalid"))
