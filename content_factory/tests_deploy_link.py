"""Plan C14: the deploy link proven at setup, and a post-merge live check per article.

* A "merge does not deploy" fixture (the host never serves the merged setup)
  raises ``deploy_site`` with the host class's copy, past the bound only.
* A merged article whose public page returns 404 alerts staff once and is not
  counted as published.
* A draft is never blocked by the live check: a review, an open pull request
  and a new article all keep their own step.

Everything is local: GitHub, the founder's site and DNS are recorded or
simulated responses; nothing leaves the process.
"""
from datetime import timedelta
from unittest import mock
from uuid import uuid4

from django.core.cache import cache
from django.test import SimpleTestCase, TestCase, override_settings
from django.utils import timezone

from organizations.models import Organization
from workflow_runs.models import ContentFactoryRun

from . import founder_journey as journeys
from . import website_deploy_link as deploy_link
from . import website_live_status as live_status
from .article_publish_status import article_bucket, refresh_publish_statuses
from .founder_journey_alerts import _receipt_index
from .models import ArticlePublishStatus, OrganizationContentConfig, WrittenArticle
from .tests_founder_journey_unit import JourneyFixture, make_run
from .tests_website_live_status import MergedLegacySite

ON = {"ARTICLES_LIVE_URL_VERIFICATION_ENABLED": True}
OFF = {"ARTICLES_LIVE_URL_VERIFICATION_ENABLED": False}
MERGE_SHA = "c" * 40


class DeployAskCopyTests(SimpleTestCase):
    def test_each_host_class_has_its_own_named_step_and_unknown_is_honest(self):
        self.assertEqual(deploy_link.deploy_ask("lovable")["headline"], "Press Publish in Lovable")
        self.assertEqual(deploy_link.deploy_ask("netlify")["headline"], "Link this repository in Netlify")
        self.assertEqual(deploy_link.deploy_ask("vercel")["headline"], "Link this repository in Vercel")
        self.assertEqual(deploy_link.deploy_ask("something-else")["hostClass"], "unknown")
        for host in deploy_link.HOST_CLASSES:
            ask = deploy_link.deploy_ask(host, staff_alerted=True)
            self.assertEqual(ask["id"], "deploy_site")
            self.assertTrue(ask["body"].endswith(deploy_link.STAFF_TOLD))
            for text in (ask["headline"], ask["body"], ask["label"]):
                self.assertNotIn("—", text)
                self.assertNotIn("–", text)

    def test_a_lovable_domain_names_the_builder(self):
        organization = mock.Mock(domain="studio.lovable.app")
        connection = mock.Mock(organization=organization, verified_sha="")
        self.assertEqual(deploy_link.host_class(connection, provider="cloudflare"), "lovable")


class MergeDoesNotDeployFixtureTests(MergedLegacySite, TestCase):
    """The setup merged; the founder's host never serves it (a builder not republished)."""

    def watch_after(self, hours, *, provider="netlify"):
        """Watch from now, then check again ``hours`` later; still not deployed. Returns (summary, then)."""
        start = timezone.now()
        self.then = start + timedelta(hours=hours)
        with self.not_deployed(), mock.patch("content_factory.website_live_status._probe_host", return_value=provider):
            self.assertEqual(live_status.observe(self.config, now=start), "waiting")
            self.assertEqual(live_status.observe(self.config, now=self.then), "waiting")
        return live_status.summary(live_status.watch_for(self.connection))

    @override_settings(**ON)
    def test_merge_that_never_deploys_raises_deploy_site_with_the_hosts_copy(self):
        watch = self.watch_after(2)
        self.assertEqual((watch["hostClass"], watch["deployLink"]), ("netlify", "not_live"))
        projected = live_status.project(deployed=False, ready=True, watch=watch, now=self.then)
        self.assertEqual(projected["action"], {"id": "deploy_site", "label": "How to link it",
                                               "url": "https://app.netlify.com/"})
        self.assertEqual(projected["headline"], "Link this repository in Netlify")
        self.assertTrue(projected["message"].startswith("Your change is merged on GitHub"))
        # Still a status, never a gate: the site stays Ready and publishable.
        from .activation import durable_integration_evidence
        self.assertTrue(durable_integration_evidence(self.config, self.connection)["publishingVerified"])

    @override_settings(**ON)
    def test_a_lovable_site_is_told_to_press_publish(self):
        self.built.organization.domain = "lavie.lovable.app"
        self.built.organization.save(update_fields=["domain"])
        self.connection.organization.refresh_from_db()
        with mock.patch("content_factory.website_deploy_link.builder_hint", return_value="lovable"):
            watch = self.watch_after(2, provider="cloudflare")
        projected = live_status.project(deployed=False, ready=True, watch=watch, now=self.then)
        self.assertEqual((projected["headline"], projected["hostClass"]), ("Press Publish in Lovable", "lovable"))

    @override_settings(**ON)
    def test_before_the_bound_there_is_no_ask(self):
        start = timezone.now()
        with self.not_deployed(), mock.patch("content_factory.website_live_status._probe_host", return_value="netlify"):
            live_status.observe(self.config, now=start)
        projected = live_status.project(deployed=False, ready=True,
            watch=live_status.summary(live_status.watch_for(self.connection)), now=start + timedelta(minutes=40))
        self.assertEqual(projected["status"], "waiting")
        self.assertNotIn("action", projected)

    @override_settings(**OFF)
    def test_with_the_flag_off_the_ask_stays_retired(self):
        watch = self.watch_after(2)
        projected = live_status.project(deployed=False, ready=True, watch=watch, now=self.then)
        self.assertNotIn("action", projected)
        self.assertIn("deploy_site", journeys.retired_actions())

    @override_settings(**ON)
    def test_a_host_that_deploys_records_the_deploy_link_as_proven(self):
        with self.live():
            self.assertEqual(live_status.observe(self.config), "live")
        watch = live_status.watch_for(self.connection)
        self.assertEqual(watch.receipt["deploy_link"], "proven")
        self.assertEqual(live_status.summary(watch)["deployLink"], "proven")

    @override_settings(**ON)
    def test_a_new_watch_proves_the_deploy_link_afresh(self):
        with self.live():
            self.assertEqual(live_status.observe(self.config), "live")
        watch = live_status.watch_for(self.connection)
        # The marker changed (a new setup merge) and the site no longer serves it.
        watch.next_attempt_at = timezone.now() - timedelta(minutes=1)
        watch.save(update_fields=["next_attempt_at"])
        with self.not_deployed(), mock.patch("content_factory.website_live_status._probe_host", return_value="netlify"), \
             mock.patch("content_factory.activation.live_deployment_verified", return_value=False):
            live_status.observe(self.config, now=timezone.now() + timedelta(hours=7))
        watch = live_status.watch_for(self.connection)
        self.assertEqual(watch.state, "pending")
        self.assertNotIn("deploy_link_proven_at", watch.receipt)
        self.assertIsNone(live_status.summary(watch)["deployLink"])


def _package_run(organization):
    body = "<article><h1>Merged article</h1><p>" + "A merged article that has not reached the site yet. " * 6 + "</p></article>"
    return ContentFactoryRun.objects.create(run_id=f"src-{uuid4().hex[:10]}", workflow="direct_generate",
        domain=organization.domain, organization=organization, result={"content_package": {"article_html": body}})


class MergedArticle404Tests(TestCase):
    """A merged article whose public page 404s alerts staff and is not counted as published."""

    def setUp(self):
        cache.clear()
        self.organization = Organization.objects.create(domain="mlai.au", name="MLAI")
        self.config = OrganizationContentConfig.objects.create(organization=self.organization,
            github_repo="MLAI-AUS-Inc/mlai-au", github_installation_id="12345")
        self.source = _package_run(self.organization)
        self.url = "https://mlai.au/articles/featured/merged-article"
        merged = timezone.now() - timedelta(hours=3)
        self.article = WrittenArticle.objects.create(organization=self.organization, title="Merged", slug="merged-article",
            category="featured", primary_keyword="merged", source_run_id=self.source.run_id,
            pr_url="https://github.com/MLAI-AUS-Inc/mlai-au/pull/991", publish_status=ArticlePublishStatus.MERGED,
            pr_merged_at=merged, on_main_verified_at=merged, on_main_commit_sha=MERGE_SHA, merge_commit_sha=MERGE_SHA,
            canonical_url=self.url)

    def refresh(self, status_code=404):
        response = mock.Mock(status_code=status_code)
        response.iter_content.return_value = [b"<html>Not found</html>"]
        pr = {"status": ArticlePublishStatus.MERGED, "number": 991, "merge_commit_sha": MERGE_SHA, "base_ref": "main"}
        # The sitemap does not list it yet: the saved canonical URL is checked instead.
        with mock.patch("content_factory.article_publish_status._site_article_urls", return_value=[]), \
             mock.patch("content_factory.article_publish_status._github_pr_state", return_value=pr), \
             mock.patch("content_factory.article_live_evidence.public_article_url", return_value=self.url) as pinned, \
             mock.patch("content_factory.article_publish_status.http_requests.get", return_value=response) as get:
            refresh_publish_statuses(self.organization, self.config, force=True)
        return pinned, get

    def alerts(self):
        receipts = dict(_receipt_index([self.organization.pk])).get(self.organization.pk, {})
        return {key: value for key, value in receipts.items()
                if (value.get("details") or {}).get("kind") == deploy_link.ARTICLE_ALERT_KIND}

    @override_settings(**ON)
    def test_a_merged_article_that_404s_alerts_staff_once_and_is_not_published(self):
        pinned, get = self.refresh()
        # The exact-origin, public-host check ran before the one fetch.
        pinned.assert_called_once_with(self.url, "mlai.au")
        self.assertEqual(get.call_args.kwargs.get("allow_redirects"), False)
        self.article.refresh_from_db()
        self.assertNotEqual(self.article.publish_status, ArticlePublishStatus.LIVE)
        self.assertEqual(article_bucket(self.article), "publishing")
        state = deploy_link.article_live_state(self.article, {"state": "unverified", "reason": "Live page returned HTTP 404"})
        self.assertEqual((state["state"], state["reason"]), ("not_live", "not_found"))
        alerts = self.alerts()
        self.assertEqual(len(alerts), 1)
        details = next(iter(alerts.values()))["details"]
        self.assertEqual((details["step"], details["code"], details["live_check"]),
                         ("article-live", "live_route_unavailable", "not_found"))
        # Re-checking the same merge never pages again.
        self.refresh()
        self.assertEqual(len(self.alerts()), 1)

    @override_settings(**ON)
    def test_the_dashboard_shows_the_article_live_state(self):
        self.refresh()
        from .vibe_marketing_views import _serialize_written_article
        self.article.refresh_from_db()
        payload = _serialize_written_article(self.article)
        self.assertEqual(payload["bucket"], "publishing")
        self.assertEqual(payload["articleLive"]["state"], "not_live")
        self.assertEqual(payload["articleLive"]["reason"], "not_found")

    @override_settings(ARTICLES_FOUNDER_STUCK_ALERTS_ENABLED=False, **ON)
    def test_the_dashboard_says_staff_were_told_only_when_they_were(self):
        self.refresh()
        from .vibe_marketing_views import _serialize_written_article
        self.article.refresh_from_db()
        self.assertEqual(self.alerts(), {})
        self.assertIs(_serialize_written_article(self.article)["articleLive"]["staffAlerted"], False)
        with override_settings(ARTICLES_FOUNDER_STUCK_ALERTS_ENABLED=True):
            self.refresh()
        self.article.refresh_from_db()
        self.assertIs(_serialize_written_article(self.article)["articleLive"]["staffAlerted"], True)

    @override_settings(**ON)
    def test_the_sweep_checks_the_least_recently_checked_websites_first(self):
        # Many websites whose articles never deploy, all checked moments ago and
        # with lower ids: they must not starve a website never checked yet.
        merged = timezone.now() - timedelta(hours=3)
        for index in range(deploy_link.SWEEP_ORGANIZATIONS + 2):
            other = Organization.objects.create(domain=f"other-{index}.example", name=f"Other {index}")
            OrganizationContentConfig.objects.create(organization=other, github_repo=f"acme/site-{index}")
            WrittenArticle.objects.create(organization=other, title="Other", slug=f"other-{index}", category="featured",
                primary_keyword="other", publish_status=ArticlePublishStatus.MERGED, pr_merged_at=merged,
                on_main_verified_at=merged, live_checked_at=timezone.now() - timedelta(minutes=11 + index))
        # The never-checked website has the highest id, so id order alone would skip it.
        latest = Organization.objects.create(domain="latest.example", name="Latest")
        OrganizationContentConfig.objects.create(organization=latest, github_repo="acme/latest")
        WrittenArticle.objects.filter(pk=self.article.pk).update(live_checked_at=None, organization=latest)
        seen = []
        with mock.patch("content_factory.article_publish_status.refresh_publish_statuses",
                        side_effect=lambda organization, *args, **kwargs: seen.append(organization.pk) or []):
            result = deploy_link.run_article_live_sweep()
        self.assertEqual(result["organizations"], deploy_link.SWEEP_ORGANIZATIONS)
        self.assertEqual(seen[0], latest.pk)
        # A website checked within the refresh interval is not due and is skipped.
        cache.clear()
        WrittenArticle.objects.exclude(pk=self.article.pk).update(live_checked_at=timezone.now())
        WrittenArticle.objects.filter(pk=self.article.pk).update(live_checked_at=timezone.now())
        with mock.patch("content_factory.article_publish_status.refresh_publish_statuses") as refresh:
            self.assertEqual(deploy_link.run_article_live_sweep()["organizations"], 0)
        refresh.assert_not_called()

    @override_settings(**ON)
    def test_within_the_bound_it_is_checking_and_nobody_is_paged(self):
        merged = timezone.now() - timedelta(minutes=10)
        WrittenArticle.objects.filter(pk=self.article.pk).update(on_main_verified_at=merged, pr_merged_at=merged)
        self.refresh()
        self.article.refresh_from_db()
        self.assertEqual(deploy_link.article_live_state(self.article)["state"], "checking")
        self.assertEqual(self.alerts(), {})

    @override_settings(**OFF)
    def test_with_the_flag_off_an_article_on_main_still_counts_and_nobody_is_paged(self):
        self.refresh()
        self.article.refresh_from_db()
        self.assertEqual(article_bucket(self.article), "published")
        self.assertEqual(self.alerts(), {})

    @override_settings(**ON)
    def test_the_sweep_rechecks_merged_articles_and_alerts(self):
        response = mock.Mock(status_code=404)
        response.iter_content.return_value = []
        pr = {"status": ArticlePublishStatus.MERGED, "number": 991, "merge_commit_sha": MERGE_SHA, "base_ref": "main"}
        with mock.patch("content_factory.article_publish_status._site_article_urls", return_value=[]), \
             mock.patch("content_factory.article_publish_status._github_pr_state", return_value=pr), \
             mock.patch("content_factory.article_live_evidence.public_article_url", return_value=self.url), \
             mock.patch("content_factory.article_publish_status.http_requests.get", return_value=response):
            result = deploy_link.run_article_live_sweep()
            self.assertEqual(deploy_link.run_article_live_sweep(), {"status": "throttled"})
        self.assertEqual((result["status"], result["checked"], result["alerted"]), ("completed", 1, 1))

    @override_settings(**OFF)
    def test_the_sweep_is_off_with_the_flag(self):
        self.assertEqual(deploy_link.run_article_live_sweep(), {"status": "disabled"})

    @override_settings(**ON)
    def test_an_article_served_after_merge_is_published(self):
        body = self.source.result["content_package"]["article_html"]
        response = mock.Mock(status_code=200)
        response.iter_content.return_value = [('<link rel="canonical" href="' + self.url + '">' + body).encode()]
        pr = {"status": ArticlePublishStatus.MERGED, "number": 991, "merge_commit_sha": MERGE_SHA, "base_ref": "main"}
        with mock.patch("content_factory.article_publish_status._site_article_urls", return_value=[]), \
             mock.patch("content_factory.article_publish_status._github_pr_state", return_value=pr), \
             mock.patch("content_factory.article_live_evidence.public_article_url", return_value=self.url), \
             mock.patch("content_factory.article_publish_status.http_requests.get", return_value=response):
            refresh_publish_statuses(self.organization, self.config, force=True)
        self.article.refresh_from_db()
        self.assertEqual(self.article.publish_status, ArticlePublishStatus.LIVE)
        self.assertEqual(article_bucket(self.article), "published")
        self.assertEqual(deploy_link.article_live_state(self.article)["state"], "live")
        self.assertEqual(self.alerts(), {})


class JourneyDeployAskTests(JourneyFixture, TestCase):
    """Home: the deploy ask after a merged article stays unserved; never in front of a draft."""

    def approved_run(self):
        return make_run(self.org, status="completed", step="deliver", approval_state="approved",
                        request={"delivery_mode": "review_draft"}, result={"delivery_mode": "review_draft"})

    def merged_article(self, run, *, hours=3):
        merged = timezone.now() - timedelta(hours=hours)
        return WrittenArticle.objects.create(organization=self.org, title="Merged", slug=f"merged-{uuid4().hex[:6]}",
            category="guides", primary_keyword="merged", source_run_id=run.run_id,
            publish_status=ArticlePublishStatus.MERGED, pr_merged_at=merged, on_main_verified_at=merged)

    def test_a_merged_article_not_live_past_the_bound_asks_to_deploy(self):
        article = self.merged_article(self.approved_run())
        with override_settings(**ON):
            body = self.journey()
        self.assertStep(body, "publishing", "your_turn", ask="deploy_site")
        copy = deploy_link.DEPLOY_ASK_COPY["unknown"]
        self.assertEqual((body["headline"], body["action"]["label"], body["action"]["url"]), (copy[0], copy[2], None))
        # Nobody was told yet: the founder copy never claims it.
        self.assertEqual(body["body"], copy[1])
        from .founder_stuck_alerts import queue_article_not_live_alert
        self.assertEqual(queue_article_not_live_alert(article), 1)
        with override_settings(**ON):
            body = self.journey()
        self.assertTrue(body["body"].endswith(deploy_link.STAFF_TOLD))

    @override_settings(ARTICLES_FOUNDER_STUCK_ALERTS_ENABLED=False, **ON)
    def test_with_staff_alerts_off_the_founder_is_not_told_staff_know(self):
        article = self.merged_article(self.approved_run())
        from .founder_stuck_alerts import queue_article_not_live_alert
        self.assertEqual(queue_article_not_live_alert(article), 0)
        body = self.journey()
        self.assertStep(body, "publishing", "your_turn", ask="deploy_site")
        self.assertNotIn(deploy_link.STAFF_TOLD.strip(), body["body"])

    def test_a_lovable_founder_is_told_to_press_publish(self):
        self.org.domain = f"site-{uuid4().hex[:6]}.lovable.app"
        self.org.save(update_fields=["domain"])
        run = self.approved_run()
        self.merged_article(run)
        self.connection()
        with override_settings(**ON):
            body = self.journey()
        self.assertStep(body, "publishing", "your_turn", ask="deploy_site")
        self.assertEqual(body["headline"], "Press Publish in Lovable")
        self.assertEqual(body["action"]["url"], "https://lovable.dev/projects")

    def test_within_the_bound_it_is_still_publishing(self):
        self.merged_article(self.approved_run(), hours=0)
        with override_settings(**ON):
            body = self.journey()
        self.assertStep(body, "publishing", "working")

    def test_a_run_stopped_on_deploy_site_gets_the_hosts_ask(self):
        make_run(self.org, status="blocked",
                 result={"failure_class": "needs_user", "failure": {"action": {"id": "deploy_site"}}})
        with override_settings(**ON):
            body = self.journey()
        self.assertStep(body, "publishing", "your_turn", ask="deploy_site")
        self.assertEqual(body["headline"], deploy_link.DEPLOY_ASK_COPY["unknown"][0])
        with override_settings(**OFF):
            body = self.journey()
        self.assertStep(body, "writing", "working", next_kind="view_progress")

    def test_with_the_flag_off_deploy_site_stays_retired(self):
        self.merged_article(self.approved_run())
        with override_settings(**OFF):
            body = self.journey()
        self.assertStep(body, "publishing", "working")

    def test_a_draft_waiting_for_review_is_never_blocked_by_the_live_check(self):
        self.merged_article(self.approved_run())
        draft = make_run(self.org, status="completed", step="deliver", result={"delivery_mode": "content_only"})
        with override_settings(**ON):
            body = self.journey()
        self.assertStep(body, "review", "your_turn", ask="review_draft")
        self.assertEqual(body["runId"], draft.run_id)

    def test_a_new_article_is_never_blocked_by_the_live_check(self):
        self.merged_article(self.approved_run())
        writing = make_run(self.org, status="running", step="research_topic")
        with override_settings(**ON):
            body = self.journey()
        self.assertEqual((body["stage"], body["state"], body["runId"]), ("writing", "working", writing.run_id))
        self.assertIsNone(body["action"])

    def test_a_live_article_is_done(self):
        run = self.approved_run()
        WrittenArticle.objects.create(organization=self.org, title="Live", slug="live", category="guides",
            primary_keyword="live", publish_status=ArticlePublishStatus.LIVE, live_url=f"https://{self.org.domain}/blog/live",
            published_at=timezone.now(), live_verified_at=timezone.now(), source_run_id=run.run_id)
        with override_settings(**ON):
            body = self.journey()
        self.assertStep(body, "done", "done", next_kind="view_article")
