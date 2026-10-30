"""P1.1 and P1.2: the server-owned founder journey projection (IF-P1-03).

29 Oct 2026 funnel: 0 of 80 new users reached an article, and the dashboard
asked them up to 9 different things at once. The journey returns one stage,
one of 3 states and one next button. These are the 14 contract fixtures and
the extra cases, each asserting exactly one ask or none and the copy of
contract section 11.2.
"""
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import patch
from uuid import uuid4

from django.test import TestCase, override_settings
from django.utils import timezone

from roo.models import Ledger
from startup_updates.models import StartupProfile
from workflow_runs.models import ContentFactoryRun

from . import first_article_entitlement as first_article
from . import founder_journey as journeys
from . import vibe_marketing_views as views
from .founder_journey_testing import ALL_P1_FLAGS_ON, make_named_legacy_org, make_new_journey_org
from .models import ArticlePublishStatus, WrittenArticle
from .website_models import WebsiteConnection, WebsiteConnectionTarget

FREE = {"ARTICLES_FREE_FIRST_ARTICLE_ENABLED": True, "ARTICLES_FIRST_ARTICLE_GOAL_ENABLED": False,
        "ARTICLES_FREE_FIRST_ARTICLE_DAILY_CAP": 0, "ARTICLES_FREE_FIRST_ARTICLE_SPIKE_THRESHOLD": 0}
FLAGS = {**ALL_P1_FLAGS_ON, **FREE}
NO_WEBSITE_FACTS = {"operation": None, "setupPullRequest": None, "canGenerateArticle": False}


def make_run(org, *, status="running", step="research_topic", request=None, result=None, workflow="article_generation",
             created_at=None, approval_state=None):
    run = ContentFactoryRun.objects.create(
        run_id=f"run-{uuid4().hex[:12]}", workflow=workflow, domain=org.domain, organization=org, status=status,
        current_step=step, run_request=request if request is not None else {"delivery_mode": "content_only"},
        result=result if result is not None else {}, **({"approval_state": approval_state} if approval_state else {}))
    if created_at is not None:
        ContentFactoryRun.objects.filter(pk=run.pk).update(created_at=created_at)
        run.refresh_from_db()
    return run


def entitled(org, user, *, attempt=1, company=None):
    """The request a reserved free first-article attempt carries."""
    row = first_article._write(organization_id=org.pk, suffix=f"attempt:{attempt}", user=user,
                               reference_id=first_article._reference(f"first-article:{company}:{attempt}"))
    return {"delivery_mode": "content_only", "client_request_id": f"first-article:{company}:{attempt}",
            "roo_points_billing_status": "entitled", "roo_points_entitlement": first_article.ENTITLEMENT,
            "roo_points_ledger_id": str(row.pk), "roo_points_cost": 0}


class JourneyFixture:
    """A new-journey startup with a one-line description; CF, prices and website reads mocked."""

    domain = None
    one_liner = "Booking software for physiotherapy clinics"

    def setUp(self):
        super().setUp()
        self.enterContext(override_settings(**FLAGS))
        self.balance = 0
        self.website_facts = dict(NO_WEBSITE_FACTS)
        self.enterContext(patch.object(views, "get_content_factory_article_cost_points", return_value=12))
        self.enterContext(patch.object(views, "_roo_points_balance_for_user", side_effect=lambda user: self.balance))
        self.pool = self.enterContext(patch.object(views, "_topic_selection_candidate_pool", return_value=[]))
        self.enterContext(patch.object(journeys, "_website_journey_facts", side_effect=lambda **kw: self.website_facts))
        self.enterContext(patch.object(first_article, "_queue_alert"))
        self.built = make_new_journey_org(self.domain)
        self.org, self.owner, self.company = self.built.organization, self.built.owner, self.built.company
        if self.one_liner:
            self.describe(self.one_liner)

    def describe(self, one_liner, *, target_audience=""):
        StartupProfile.objects.update_or_create(organization=self.org, defaults={
            "short_description": one_liner, "target_audience": target_audience})

    def journey(self, **kwargs):
        return journeys.project_founder_journey(self.org, user=self.owner, company=self.company, **kwargs)

    def assertStep(self, body, stage, state, *, ask=None, next_kind=None):
        """Exactly one ask or none: ``action`` and ``next`` are never both set."""
        self.assertEqual((body["stage"], body["state"]), (stage, state), body)
        self.assertEqual((body["action"] or {}).get("id"), ask, body)
        self.assertEqual((body["next"] or {}).get("kind"), next_kind, body)
        self.assertFalse(body["action"] and body["next"], body)
        if body["action"]:
            self.assertIn(body["action"]["id"], journeys.FOUNDER_ASKS)
        if body["next"]:
            self.assertIn(body["next"]["kind"], journeys.NEXT_KINDS)
        for text in (body["headline"], body["body"], (body["action"] or body["next"] or {}).get("label")):
            if text:
                self.assertNotIn("—", text)
                self.assertNotIn("–", text)
                self.assertNotIn("Roo", text)

    def setup_run(self, status="running", result=None):
        return make_run(self.org, workflow="article_system_setup", status=status, step="setup",
                        request={"github_repo": "example/site"}, result=result or {})

    def connection(self, state=WebsiteConnection.State.CONNECTED, verified=True):
        connection = WebsiteConnection.objects.create(
            organization=self.org, github_repo="example/site", installation_id="1000001", branch="main",
            site_url=f"https://{self.org.domain}", state=state, verified_sha="a" * 40 if verified else "",
            last_verified_at=timezone.now() if verified else None)
        WebsiteConnectionTarget.objects.create(
            connection=connection, target_key="articles", generation=connection.generation, adapter="next_app_router",
            source_sha="a" * 40, contract={"route": "/blog/{slug}"}, verified_at=timezone.now() if verified else None)
        if state == WebsiteConnection.State.CONNECTED and verified:
            self.built.config.website_connection = connection
            self.built.config.save(update_fields=["website_connection", "updated_at"])
        return connection


class ContractFixtureTests(JourneyFixture, TestCase):
    """The 14 fixtures of IF-P1-03."""

    def test_01_no_website_with_a_description_gets_the_free_first_article(self):
        self.org.domain = f"banksia-{uuid4().hex[:6]}.invalid"
        self.org.save(update_fields=["domain"])
        body = self.journey()
        self.assertStep(body, "first_article", None, next_kind="write_first_article")
        self.assertEqual(body["headline"], "Your first article is on us")
        self.assertTrue(body["body"].startswith("Here's your first article: "))
        self.assertEqual(body["next"]["label"], "Write my first article · free")
        self.assertEqual(body["website"], {"hasWebsite": False, "domain": None, "platform": None, "blogUrl": None,
                                           "publishAutomatically": "unavailable"})
        self.assertEqual(body["pollAfterMs"], 30000)

    def test_02_website_without_a_repo(self):
        body = self.journey()
        self.assertStep(body, "first_article", None, next_kind="write_first_article")
        self.assertEqual(body["website"]["publishAutomatically"], "available")
        self.assertEqual(body["website"]["domain"], self.org.domain)

    def test_03_repo_saved_but_unbound(self):
        self.built.config.github_repo = "example/site"
        self.built.config.save(update_fields=["github_repo", "updated_at"])
        self.assertStep(self.journey(), "first_article", None, next_kind="write_first_article")

    def test_04_setup_working_never_blocks_the_first_article(self):
        self.connection(state=WebsiteConnection.State.DISCONNECTED, verified=False)
        self.setup_run("running")
        body = self.journey()
        self.assertStep(body, "first_article", None, next_kind="write_first_article")
        self.assertEqual(body["website"]["publishAutomatically"], "in_progress")

    def test_05_setup_needing_the_owner_click(self):
        run = self.setup_run("awaiting_approval")
        stub = SimpleNamespace(requires_owner_click=lambda org: True)
        with patch.object(journeys, "_owner_click_module", return_value=stub):
            body = self.journey()
        self.assertStep(body, "setup", "your_turn", ask="approve_setup")
        self.assertEqual((body["headline"], body["body"], body["action"]["label"], body["runId"]),
                         ("Your blog page is ready", "Take a look, then add it to your website.", "Take a look",
                          run.run_id))

    def test_06_unsupported_runtime(self):
        self.connection(state=WebsiteConnection.State.DISCONNECTED, verified=False)
        self.website_facts = {**NO_WEBSITE_FACTS, "operation": {"state": "failed", "failureClass": "unsupported",
                                                                "actionId": None, "url": None}}
        self.assertStep(self.journey(), "first_article", None, next_kind="write_first_article")

    def test_07_verified_site_without_an_article_keeps_the_free_first_article(self):
        self.connection()
        body = self.journey()
        self.assertStep(body, "first_article", None, next_kind="write_first_article")
        self.assertEqual(body["next"]["label"], "Write my first article · free")
        self.assertEqual((body["firstArticle"]["status"], body["firstArticle"]["limit"]), ("available", None))
        # Without the card flag the verified site still counts as established (P0's rule).
        with override_settings(ARTICLES_FIRST_ARTICLE_CARD_ENABLED=False):
            self.assertEqual(first_article.entitlement_state(self.org, self.owner)["limit"], "established")
        # A written article still makes the startup established.
        WrittenArticle.objects.create(organization=self.org, title="Older post", slug="older-post", category="guides",
                                      primary_keyword="older post")
        self.assertEqual(first_article.entitlement_state(self.org, self.owner)["limit"], "established")

    def test_08_queued(self):
        run = make_run(self.org, status="queued", step="queued")
        with patch("content_factory.run_queue_position.queue_position_payload",
                   return_value={"queuePosition": 3, "queueEtaSeconds": 610}):
            body = self.journey()
        self.assertStep(body, "writing", "working", next_kind="view_progress")
        self.assertEqual((body["queuePosition"], body["etaSeconds"], body["runId"]), (3, 660, run.run_id))
        self.assertEqual(body["body"], "You're 3rd in line · about 11 minutes.")
        self.assertEqual(body["pollAfterMs"], 5000)

    def test_09_draft_writing(self):
        run = make_run(self.org, status="running", step="write_article")
        body = self.journey()
        self.assertStep(body, "writing", "working", next_kind="view_progress")
        self.assertEqual((body["headline"], body["body"], body["next"]["label"], body["next"]["runId"]),
                         ("Working on it", "Writing your article.", "See progress", run.run_id))
        self.assertIsNone(body["holdingReason"])

    def test_10_draft_ready_content_only_not_copied(self):
        run = make_run(self.org, status="completed", step="deliver", result={"delivery_mode": "content_only"})
        body = self.journey()
        self.assertStep(body, "review", "your_turn", ask="review_draft")
        self.assertEqual((body["headline"], body["body"], body["action"]["label"], body["runId"]),
                         ("Your draft is ready", "Tap anything to suggest a change, then copy it to your website.",
                          "Review your draft", run.run_id))

    def test_11_publishing(self):
        make_run(self.org, status="completed", step="deliver", approval_state="approved",
                 request={"delivery_mode": "review_draft"}, result={"delivery_mode": "review_draft"})
        body = self.journey()
        self.assertStep(body, "publishing", "working")
        self.assertEqual((body["holdingReason"], body["headline"], body["body"], body["pollAfterMs"]),
                         ("website_update", "Publishing…", "Waiting for your website to update.", 15000))

    def test_12_live(self):
        run = make_run(self.org, status="completed", step="deliver", approval_state="approved",
                       request={"delivery_mode": "review_draft"}, result={"delivery_mode": "review_draft"})
        WrittenArticle.objects.create(
            organization=self.org, title="Live", slug="live", category="guides", primary_keyword="live",
            publish_status=ArticlePublishStatus.LIVE, live_url=f"https://{self.org.domain}/blog/live",
            published_at=timezone.now(), live_verified_at=timezone.now(), source_run_id=run.run_id)
        body = self.journey()
        self.assertStep(body, "done", "done", next_kind="view_article")
        self.assertEqual((body["headline"], body["next"]["label"], body["next"]["url"], body["pollAfterMs"]),
                         ("Your article is live!", "View article", f"https://{self.org.domain}/blog/live", 60000))

    def test_13_legacy_github_publisher_short_circuits(self):
        legacy = make_named_legacy_org("talathrive")
        body = journeys.project_founder_journey(legacy.organization, user=legacy.owner, company=legacy.company)
        self.assertEqual(body, journeys.legacy_payload(legacy.company.pk))
        self.assertEqual((body["stage"], body["state"], body["action"], body["next"], body["pollAfterMs"]),
                         ("legacy", None, None, None, 60000))

    def test_14_paid_article_failed_terminally_and_refunded(self):
        key = f"paid-{uuid4().hex[:8]}"
        make_run(self.org, status="failed", step="write_article",
                 request={"delivery_mode": "content_only", "client_request_id": key,
                          "roo_points_billing_status": "charged"},
                 result={"failure_class": "permanent", "failure_fingerprint": "fp-terminal-1"})
        Ledger.objects.create(user=self.owner, delta=-12, kind="SPEND", source="CONTENT_FACTORY",
                              idempotency_key=f"content_factory:charge:{key}")
        Ledger.objects.create(user=self.owner, delta=12, kind="REFUND", source="CONTENT_FACTORY",
                              idempotency_key=f"content_factory:refund:{key}")
        self.balance = 40
        body = self.journey()
        self.assertStep(body, "first_article", None, next_kind="write_article")
        self.assertEqual((body["body"], body["next"]["label"], body["supportCode"]),
                         ("We couldn't write this one. Your points are back.", "Write an article · 12 points",
                          "fp-terminal-1"))


class NoDescriptionTests(JourneyFixture, TestCase):
    one_liner = ""

    def test_no_domain_and_no_description_asks_for_a_website(self):
        self.org.domain = f"nothing-{uuid4().hex[:6]}.invalid"
        self.org.save(update_fields=["domain"])
        body = self.journey()
        self.assertStep(body, "website", None, next_kind="add_website")
        self.assertEqual((body["headline"], body["body"], body["next"]["label"]),
                         ("What's your startup's website?", "We'll read it and fill in the rest.", "Add your website"))

    def test_describe_customer_without_a_description_or_audience(self):
        body = self.journey()
        self.assertStep(body, "first_article", "your_turn", ask="describe_customer")
        self.assertEqual((body["headline"], body["body"], body["action"]["label"]),
                         ("Who is your typical customer?", "We'll write your first article for them.",
                          "Describe your customer"))

    def test_a_target_audience_is_enough(self):
        self.describe("", target_audience="Clinic owners in regional towns")
        self.assertStep(self.journey(), "first_article", None, next_kind="write_first_article")


class ExtraCaseTests(JourneyFixture, TestCase):
    def test_an_invalid_org_with_a_description_never_asks_for_the_customer(self):
        self.org.domain = f"banksia-{uuid4().hex[:6]}.invalid"
        self.org.save(update_fields=["domain"])
        self.assertNotEqual((self.journey()["action"] or {}).get("id"), "describe_customer")

    def test_buy_points_when_the_free_article_is_used_and_the_balance_is_short(self):
        with patch.object(first_article, "entitlement_state", return_value={
                "version": 1, "status": "used", "free": False, "attempts_used": 1, "attempts_allowed": 2,
                "topic_batch_free": False, "run_id": None, "limit": None}):
            self.balance = 5
            body = self.journey()
        self.assertStep(body, "first_article", "your_turn", ask="buy_points")
        self.assertEqual((body["headline"], body["body"], body["action"]["label"]),
                         ("This article uses 12 points. You have 5.", None, "Get 7 more points"))

    def test_a_run_waiting_on_points_asks_for_points(self):
        make_run(self.org, status="blocked", step="charge",
                 result={"failure_class": "needs_user", "failure": {"action": {"id": "buy_points"}}})
        self.balance = 2
        body = self.journey()
        self.assertStep(body, "writing", "your_turn", ask="buy_points")
        self.assertEqual(body["action"]["label"], "Get 10 more points")

    def test_authorize_github(self):
        self.setup_run("blocked", result={"failure_class": "needs_user",
                                          "failure": {"action": {"id": "review_github_permissions"}}})
        body = self.journey()
        self.assertStep(body, "setup", "your_turn", ask="authorize_github")
        self.assertEqual((body["headline"], body["action"]["label"]),
                         ("We need permission to read your website on GitHub", "Give access on GitHub"))

    def test_approve_on_github_carries_the_pull_request_url(self):
        url = "https://github.com/example/site/pull/7"
        self.setup_run("blocked", result={"failure_class": "needs_user", "pr_html_url": url,
                                          "failure": {"action": {"id": "merge_pr"}}})
        body = self.journey()
        self.assertStep(body, "setup", "your_turn", ask="approve_on_github")
        self.assertEqual((body["action"]["url"], body["action"]["label"], body["body"]),
                         (url, "Open on GitHub", "Approve it on GitHub when you're ready."))

    def test_manual_setup_approval_shows_the_open_pull_request(self):
        url = "https://github.com/example/site/pull/9"
        run = self.setup_run("awaiting_approval", result={"pr_html_url": url})
        strategy = dict(self.built.config.pillar_strategy)
        strategy["founder_journey"] = {**strategy["founder_journey"],
                                       "setupApproval": {"runId": run.run_id, "merge": "manual"}}
        self.built.config.pillar_strategy = strategy
        self.built.config.save(update_fields=["pillar_strategy", "updated_at"])
        body = self.journey()
        self.assertStep(body, "setup", "your_turn", ask="approve_on_github")
        self.assertEqual(body["action"]["url"], url)

    def test_daily_cap_is_busy_today(self):
        with override_settings(ARTICLES_FREE_FIRST_ARTICLE_DAILY_CAP=1):
            other = make_new_journey_org()
            first_article._write(organization_id=other.organization.pk, suffix="attempt:1", user=other.owner,
                                 reference_id="other")
            body = self.journey()
        self.assertStep(body, "first_article", None)
        self.assertEqual((body["holdingReason"], body["body"], body["next"]),
                         ("busy_today", "Free first articles are busy today. Please try again tomorrow.", None))

    def test_a_failed_free_article_whose_entitlement_is_back_offers_it_again(self):
        request = entitled(self.org, self.owner, company=self.company.pk)
        run = make_run(self.org, status="failed", step="write_article", request=request,
                       result={"failure_class": "permanent", "failure_occurrence_id": "occ-1"})
        first_article.release_entitlement(organization=self.org, run=run, reason="failed")
        body = self.journey()
        self.assertStep(body, "first_article", None, next_kind="write_first_article")
        self.assertEqual((body["body"], body["supportCode"]),
                         ("We couldn't write this one. Your free article is still waiting for you.", "occ-1"))

    def test_a_charged_run_whose_refund_is_pending(self):
        key = f"paid-{uuid4().hex[:8]}"
        make_run(self.org, status="failed", request={"client_request_id": key}, result={"failure_class": "transient"})
        Ledger.objects.create(user=self.owner, delta=-12, kind="SPEND", source="CONTENT_FACTORY",
                              idempotency_key=f"content_factory:charge:{key}")
        body = self.journey()
        self.assertEqual(body["body"], "We couldn't write this one. We're returning your points.")
        self.assertEqual(body["next"]["kind"], "write_article")

    def test_a_free_run_that_failed_with_attempts_exhausted_has_no_free_line(self):
        for attempt in (1, 2):
            request = entitled(self.org, self.owner, attempt=attempt, company=self.company.pk)
            run = make_run(self.org, status="failed", request=request, result={"failure_class": "permanent"},
                           created_at=timezone.now() - timedelta(minutes=10 - attempt))
            first_article.release_entitlement(organization=self.org, run=run, reason="failed")
        body = self.journey()
        self.assertEqual(body["body"], "We couldn't write this one.")
        self.assertNotIn("free", body["body"])
        self.assertEqual(body["next"]["kind"], "write_article")

    def test_a_retrying_failure_is_working_and_slow_with_no_email_clause(self):
        make_run(self.org, status="failed", result={"failure_class": "transient", "recovery": {"state": "pending"}})
        with patch.object(journeys, "draft_ready_notifications", return_value=True):
            body = self.journey()
        self.assertStep(body, "writing", "working", next_kind="view_progress")
        self.assertEqual((body["holdingReason"], body["body"]), ("slow", "Taking a little longer than usual."))
        self.assertNotIn(journeys.EMAIL_CLAUSE, body["body"])

    def test_a_superseded_run_is_skipped(self):
        make_run(self.org, status="running", step="write_article", created_at=timezone.now() - timedelta(minutes=5))
        make_run(self.org, status="cancelled", result={"cancellation_reason": "superseded", "cancelled_by": "system"})
        body = self.journey()
        self.assertEqual(body["stage"], "writing")

    def test_a_copied_content_only_draft_is_done(self):
        run = make_run(self.org, status="completed", step="deliver", result={"delivery_mode": "content_only"})
        strategy = dict(self.built.config.pillar_strategy)
        strategy["founder_posted_articles"] = {run.run_id: {"copiedAt": timezone.now().isoformat()}}
        self.built.config.pillar_strategy = strategy
        self.built.config.save(update_fields=["pillar_strategy", "updated_at"])
        body = self.journey()
        self.assertStep(body, "done", "done", next_kind="view_draft")
        self.assertEqual((body["headline"], body["next"]["label"], body["next"]["runId"]),
                         ("Your article is ready to post", "Open your article", run.run_id))

    def test_a_posted_link_is_done_too(self):
        run = make_run(self.org, status="completed", step="deliver", result={"delivery_mode": "content_only"})
        strategy = dict(self.built.config.pillar_strategy)
        strategy["founder_posted_articles"] = {run.run_id: {"url": f"https://{self.org.domain}/blog/x"}}
        self.built.config.pillar_strategy = strategy
        self.built.config.save(update_fields=["pillar_strategy", "updated_at"])
        self.assertStep(self.journey(), "done", "done", next_kind="view_draft")

    def test_slow_is_held_and_offers_a_new_topic_on_the_free_run(self):
        request = entitled(self.org, self.owner, company=self.company.pk)
        make_run(self.org, status="running", step="write_article", request=request,
                 created_at=timezone.now() - timedelta(minutes=40))
        body = self.journey()
        self.assertStep(body, "writing", "working", next_kind="view_progress")
        self.assertEqual((body["holdingReason"], body["body"], body["switchTopicAvailable"]),
                         ("slow", "Taking a little longer than usual.", True))

    def test_slow_after_is_twice_the_median(self):
        make_run(self.org, status="running", step="write_article", result={"queue_name": "articles"},
                 created_at=timezone.now() - timedelta(minutes=12))
        with patch("content_factory.run_queue_position._cached_median_run_seconds", return_value=300.0):
            body = self.journey()
        self.assertEqual((body["holdingReason"], body["etaSeconds"]), ("slow", 60))
        with patch("content_factory.run_queue_position._cached_median_run_seconds", return_value=1200.0):
            body = self.journey()
        self.assertIsNone(body["holdingReason"])
        self.assertEqual((body["body"], body["etaSeconds"]), ("Usually about 8 minutes.", 480))

    def test_a_free_run_started_outside_the_card_never_offers_a_new_topic(self):
        request = {**entitled(self.org, self.owner, company=self.company.pk), "client_request_id": "articles-page-1"}
        Ledger.objects.filter(reference_type=first_article.REFERENCE_TYPE).update(
            reference_id=first_article._reference("articles-page-1"))
        make_run(self.org, status="running", step="write_article", request=request,
                 created_at=timezone.now() - timedelta(minutes=40))
        body = self.journey()
        self.assertEqual(body["holdingReason"], "slow")
        self.assertFalse(body["switchTopicAvailable"])

    def test_no_new_topic_when_the_daily_cap_would_refuse_attempt_two(self):
        request = entitled(self.org, self.owner, company=self.company.pk)
        make_run(self.org, status="running", step="write_article", request=request,
                 created_at=timezone.now() - timedelta(minutes=40))
        self.assertTrue(self.journey()["switchTopicAvailable"])
        # This founder's own attempt fills a cap of 1; a released attempt still counts.
        with override_settings(ARTICLES_FREE_FIRST_ARTICLE_DAILY_CAP=1):
            self.assertFalse(self.journey()["switchTopicAvailable"])
        with override_settings(ARTICLES_FREE_FIRST_ARTICLE_DAILY_CAP=2):
            self.assertTrue(self.journey()["switchTopicAvailable"])

    def test_a_paid_slow_run_never_offers_a_new_topic(self):
        make_run(self.org, status="running", step="write_article", created_at=timezone.now() - timedelta(minutes=40))
        body = self.journey()
        self.assertEqual(body["holdingReason"], "slow")
        self.assertFalse(body["switchTopicAvailable"])

    def test_every_retired_registry_action_shows_working(self):
        for action in sorted(journeys.RETIRED_ACTIONS):
            with self.subTest(action=action):
                ContentFactoryRun.objects.filter(organization=self.org).delete()
                make_run(self.org, status="blocked",
                         result={"failure_class": "needs_user", "failure": {"action": {"id": action}}})
                with self.assertLogs("content_factory.founder_journey", level="INFO") as logs:
                    body = self.journey()
                self.assertStep(body, "writing", "working", next_kind="view_progress")
                self.assertEqual((body["holdingReason"], body["body"]), ("slow", "Still working."))
                self.assertTrue(any(f"founder_journey_retired_action org={self.org.pk} action={action}" in line
                                    for line in logs.output))

    def test_github_return_lasts_fifteen_minutes(self):
        at = timezone.now() - timedelta(minutes=5)
        strategy = dict(self.built.config.pillar_strategy)
        strategy["founder_journey"] = {**strategy["founder_journey"],
                                       "githubReturn": {"reason": "installed", "at": at.isoformat()}}
        self.built.config.pillar_strategy = strategy
        self.built.config.save(update_fields=["pillar_strategy", "updated_at"])
        self.assertEqual(self.journey()["githubReturn"], {"reason": "installed", "at": at.isoformat()})
        self.assertIsNone(self.journey(now=timezone.now() + timedelta(minutes=11))["githubReturn"])

    def test_platform_and_blog_url_come_from_the_site_profile(self):
        strategy = dict(self.built.config.pillar_strategy)
        strategy["founder_first_profile"] = {"siteProfile": {"platform": "wix", "blogUrl": "https://x.example/blog",
                                                             "existingPosts": []}}
        self.built.config.pillar_strategy = strategy
        self.built.config.save(update_fields=["pillar_strategy", "updated_at"])
        website = self.journey()["website"]
        self.assertEqual((website["platform"], website["blogUrl"]), ("wix", "https://x.example/blog"))

    def test_publish_automatically_is_on_with_the_marker_and_a_connected_site(self):
        self.connection()
        strategy = dict(self.built.config.pillar_strategy)
        strategy["founder_journey"] = {**strategy["founder_journey"],
                                       "publishAutomatically": {"enabledAt": timezone.now().isoformat()}}
        self.built.config.pillar_strategy = strategy
        self.built.config.save(update_fields=["pillar_strategy", "updated_at"])
        self.assertEqual(self.journey()["website"]["publishAutomatically"], "on")

    def test_publish_automatically_is_in_progress_only_while_something_is_underway(self):
        website = lambda: self.journey()["website"]["publishAutomatically"]  # noqa: E731
        connection = self.connection(verified=False)
        self.assertEqual(website(), "in_progress")  # connecting, not verified yet
        setup = self.setup_run("failed")
        self.assertEqual(website(), "available")
        ContentFactoryRun.objects.filter(pk=setup.pk).update(status="awaiting_approval")
        self.assertEqual(website(), "in_progress")
        ContentFactoryRun.objects.filter(pk=setup.pk).delete()
        for state in (WebsiteConnection.State.DISCONNECTED, WebsiteConnection.State.REVOKED,
                      WebsiteConnection.State.PAUSED):
            with self.subTest(state=state):
                WebsiteConnection.objects.filter(pk=connection.pk).update(state=state)
                self.assertEqual(website(), "available")

    def test_a_verified_site_never_approved_for_publishing_is_available(self):
        self.connection()
        self.assertEqual(self.journey()["website"]["publishAutomatically"], "available")

    @override_settings(COMMUNITY_CHAT_MEMBERSHIP_POLICY_VERSION="2026-10-30")
    def test_out_of_date_terms_ask_the_member_to_agree_again(self):
        from community_chat.models import CommunityMemberProfile
        # A member who never accepted any version is not asked here.
        self.assertNotEqual((self.journey()["action"] or {}).get("id"), "agree_terms")
        profile = CommunityMemberProfile.objects.create(user=self.owner, status="approved", first_name="Jo",
                                                        adult_confirmed_at=timezone.now(), policy_version="2026-10-30")
        self.assertNotEqual((self.journey()["action"] or {}).get("id"), "agree_terms")
        profile.policy_version = "2026-01-01"
        profile.save(update_fields=["policy_version"])
        make_run(self.org, status="running", step="write_article")
        body = self.journey()
        self.assertStep(body, "first_article", "your_turn", ask="agree_terms")
        self.assertEqual((body["headline"], body["body"], body["action"]["label"]),
                         ("Our terms were just updated", "Please tick to agree again.", "Review the terms"))

    def test_the_body_never_carries_a_registry_key(self):
        make_run(self.org, status="failed", result={"failure_class": "permanent", "failure_fingerprint": "fp"})
        body = self.journey()
        scanned = {"code", "error_code", "errorCode", "blocking_code", "blockingCode", "reasonCode", "reason_code",
                   "failure_code", "model_adapter_blocking_code"}

        def keys(value):
            if isinstance(value, dict):
                for key, item in value.items():
                    yield key
                    yield from keys(item)
            elif isinstance(value, list):
                for item in value:
                    yield from keys(item)
        self.assertFalse(scanned & set(keys(body)))


class EmailClauseTests(JourneyFixture, TestCase):
    """The email clause appears only when draft-ready email can be sent, never on a failure or retry."""

    def test_running_copy_gains_the_clause_only_when_email_can_be_sent(self):
        make_run(self.org, status="running", step="write_article")
        self.assertEqual(self.journey()["body"], "Writing your article.")
        with patch.object(journeys, "draft_ready_notifications", return_value=True):
            body = self.journey()
        self.assertEqual(body["body"], f"Writing your article. {journeys.EMAIL_CLAUSE}")
        self.assertTrue(body["draftReadyNotifications"])

    def test_failure_lines_never_carry_the_clause(self):
        make_run(self.org, status="failed", result={"failure_class": "permanent"})
        with patch.object(journeys, "draft_ready_notifications", return_value=True):
            body = self.journey()
        self.assertNotIn(journeys.EMAIL_CLAUSE, body["body"])

    def test_a_missing_outbox_module_means_no_email(self):
        with patch.object(journeys.importlib, "import_module", side_effect=ImportError):
            self.assertFalse(journeys.draft_ready_notifications(self.org, self.owner))

    def test_a_present_outbox_module_is_asked(self):
        module = SimpleNamespace(can_notify=lambda org, user: True)
        with patch.object(journeys.importlib, "import_module", return_value=module):
            self.assertTrue(journeys.draft_ready_notifications(self.org, self.owner))

    def test_a_missing_owner_click_module_means_only_the_registry_mapping(self):
        self.setup_run("awaiting_approval")
        with patch.object(journeys.importlib, "import_module", side_effect=ImportError):
            body = self.journey()
        self.assertStep(body, "first_article", None, next_kind="write_first_article")


@override_settings(**FLAGS)
class HelperTests(TestCase):
    def test_ordinals(self):
        self.assertEqual([journeys.ordinal(n) for n in (1, 2, 3, 4, 11, 12, 13, 21, 22, 101)],
                         ["1st", "2nd", "3rd", "4th", "11th", "12th", "13th", "21st", "22nd", "101st"])

    def test_if_none_match(self):
        self.assertTrue(journeys.matches_if_none_match('"abc"', "abc"))
        self.assertTrue(journeys.matches_if_none_match('W/"abc"', "abc"))
        self.assertTrue(journeys.matches_if_none_match('"x", W/"abc"', "abc"))
        self.assertFalse(journeys.matches_if_none_match('"abd"', "abc"))
        self.assertFalse(journeys.matches_if_none_match("", "abc"))

    def test_registry_mapping_covers_only_the_eight_asks(self):
        self.assertEqual(len(journeys.FOUNDER_ASKS), 8)
        self.assertTrue(set(journeys.REGISTRY_ASKS.values()) <= set(journeys.FOUNDER_ASKS))
        self.assertIsNone(journeys.founder_ask("choose_articles_page"))

    def test_copy_has_no_dashes_and_no_roo(self):
        for case, texts in journeys.COPY.items():
            for text in texts:
                if text:
                    with self.subTest(case=case):
                        self.assertNotIn("—", text)
                        self.assertNotIn("–", text)
                        self.assertNotIn("Roo", text)

    @override_settings(MY_STARTUP_FOUNDER_JOURNEY_V2_ENABLED=False)
    def test_with_v2_off_a_new_org_is_legacy(self):
        built = make_new_journey_org()
        body = journeys.project_founder_journey(built.organization, user=built.owner, company=built.company)
        self.assertEqual(body["stage"], "legacy")
