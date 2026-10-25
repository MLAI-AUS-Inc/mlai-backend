"""MLAI-hosted article directories publish without a repository change.

PawShare (24 Oct 2026): its merged repo setup could not publish, so its blog is
served by MLAI on pawshareclub.com/blog/* and published from these records.
"""
from unittest.mock import Mock, patch

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from rest_framework.test import APIRequestFactory, force_authenticate

from founder_tools.models import VibeRaisingCompany, VibeRaisingProfile
from organizations.models import Organization
from workflow_runs.models import ContentFactoryRun

from . import hosted_articles as hosted
from .activation import founder_context_for_domain
from .hosted_article_models import HostedArticlePublication, HostedArticleSite
from .hosted_article_views import (HostedArticlePublicationServiceView, HostedArticlePublishView, HostedArticleSiteServiceView,
                                   HostedArticleSiteSourceServiceView)
from .models import ArticlePublishStatus, WrittenArticle

DIGEST = "d" * 64
KEY = "synthetic-test-key"


def article(**changes):
    values = {"slug": "dog-friendly-cafes", "title": "Dog-friendly cafés", "excerpt": "Where to go.", "published_at": "2026-10-24",
              "author_name": "Pawshare Club", "body_html": "<h2>Before you go</h2><p>Call ahead.</p>", "categories": ["Pet Care Tips"],
              "tags": [], "cover": None, "structured_data": {"schema_version": 1, "json_ld": []}, "unexpected": "dropped"}
    values.update(changes)
    return values


@override_settings(ROO_API_KEY=KEY, INTERNAL_API_KEY=KEY, ARTICLES_HOSTED_SITES_ENABLED=True)
class HostedArticleTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(email="hosted@example.test")
        self.profile = VibeRaisingProfile.objects.create(user=self.user, role="founder")
        self.org = Organization.objects.create(domain="pawshare.example.test", name="Synthetic hosted")
        VibeRaisingCompany.objects.create(profile=self.profile, organization=self.org, domain=self.org.domain, name="Synthetic")
        self.context = founder_context_for_domain(self.user, self.org.domain)
        self.site = HostedArticleSite.objects.create(organization=self.org, domain=self.org.domain, state="active",
                                                     profile={"base_path": "/blog/"})
        self.run = ContentFactoryRun.objects.create(run_id="hosted-run", domain=self.org.domain, organization=self.org,
                                                    workflow="direct_generate", status="completed", github_repo="", result={},
                                                    run_request={"delivery_mode": "content_only", "delivery_mode_confirmed": True})
        self.written = WrittenArticle.objects.create(organization=self.org, title="Dog-friendly cafés", slug="dog-friendly-cafes",
                                                     category="Pet Care Tips", primary_keyword="dog friendly cafe", source_run_id=self.run.run_id)
        self.factory = APIRequestFactory()

    def publish(self, method="post", run_id="hosted-run", dispatch_status=202, reviewed="rev-1", review=None):
        body = {"reviewedRevision": reviewed, "waivedComments": []} if reviewed else {}
        request = getattr(self.factory, method)("/synthetic", body, format="json")
        force_authenticate(request, user=self.user)
        with patch("content_factory.vibe_marketing_views._resolve_context_or_response", return_value=(self.context, None)), \
             patch("content_factory.vibe_marketing_views._latest_review_ready_component_revision", return_value=None), \
             patch("content_factory.article_review_views.remote_review", return_value=review or {"revision": "rev-1", "previewPending": False}), \
             patch("content_factory.vibe_marketing_views._content_factory_remote_config", return_value={"enabled": True, "base_url": "https://cf.test"}), \
             patch("content_factory.hosted_article_views.http_client.post", return_value=Mock(status_code=dispatch_status)) as post:
            response = HostedArticlePublishView.as_view(authentication_classes=[])(request, run_id=run_id)
        return response, post

    def progress(self, **data):
        return hosted.record_progress(domain=self.site.domain, run_id=self.run.run_id, data=data)

    def test_the_owner_queues_a_finished_article_and_content_factory_is_asked_to_publish_it(self):
        response, post = self.publish()
        self.assertEqual(response.status_code, 202, response.data)
        publication = HostedArticlePublication.objects.get(site=self.site, run_id=self.run.run_id)
        self.assertEqual(publication.status, "pending")
        self.assertEqual(publication.requested_by, self.user)
        self.assertEqual(post.call_args.args[0], "https://cf.test/api/runs/hosted-run/hosted-publish")
        self.assertEqual(post.call_args.kwargs["json"], {"domain": self.site.domain, "base_path": "/blog/", "publication_id": str(publication.pk),
                                                          "reviewed_revision": "rev-1"})
        self.run.refresh_from_db()
        self.assertEqual(self.run.result["article_review_approval"]["revision"], "rev-1")

    def test_publishing_needs_the_current_reviewed_revision_and_a_repo_free_article(self):
        for reviewed, review in (("", None), ("rev-0", None), ("rev-1", {"revision": "rev-1", "previewPending": True})):
            response, post = self.publish(reviewed=reviewed, review=review)
            self.assertEqual((response.status_code, response.data["reason"]), (409, "review_changed"))
            post.assert_not_called()
        self.run.run_request = {"delivery_mode": "review_draft"}
        self.run.save(update_fields=["run_request"])
        response, post = self.publish()
        self.assertEqual((response.status_code, response.data["reason"]), (400, "not_a_hosted_article"))
        self.assertFalse(HostedArticlePublication.objects.exists())

    def test_a_content_factory_outage_is_reported_and_recorded(self):
        response, _ = self.publish(dispatch_status=500)
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.data["status"], "failed")
        self.assertEqual(response.data["failureReason"], "hosted_dispatch_failed")

    def test_unfinished_articles_and_sites_without_hosting_cannot_publish(self):
        self.run.status = "running"
        self.run.save(update_fields=["status"])
        response, post = self.publish()
        self.assertEqual((response.status_code, response.data["reason"]), (409, "article_not_ready"))
        post.assert_not_called()
        self.site.state = "paused"
        self.site.save(update_fields=["state"])
        response, post = self.publish()
        self.assertEqual((response.status_code, response.data["reason"]), (409, "hosted_site_not_active"))
        post.assert_not_called()

    def test_render_upload_and_live_check_make_the_article_live(self):
        hosted.request_publication(site=self.site, run=self.run, user=self.user)
        self.progress(status="rendering", article=article(), revision_digest=DIGEST)
        self.progress(status="deployed", deployment={"worker_name": "mlai-blog-x", "url": "https://mlai-blog-x.mlai.workers.dev", "manifest_digest": "m" * 64})
        publication = self.progress(status="live", verified_digest=DIGEST)
        self.assertEqual(publication.status, "live")
        self.assertNotIn("unexpected", publication.article)
        self.written.refresh_from_db()
        self.site.refresh_from_db()
        self.assertEqual(self.written.publish_status, ArticlePublishStatus.LIVE)
        self.assertEqual(self.written.live_url, "https://pawshare.example.test/blog/dog-friendly-cafes/")
        self.assertEqual(self.written.canonical_path, "/blog/dog-friendly-cafes/")
        self.assertIsNotNone(self.site.proxy_verified_at)
        self.assertEqual(self.site.worker_url, "https://mlai-blog-x.mlai.workers.dev")
        state = hosted.site_render_state(self.site)
        self.assertEqual([row["slug"] for row in state["publications"]], ["dog-friendly-cafes"])

    def test_a_live_page_without_this_revision_and_out_of_order_reports_are_refused(self):
        hosted.request_publication(site=self.site, run=self.run, user=self.user)
        with self.assertRaises(hosted.HostedPublishError) as stale:
            self.progress(status="live", verified_digest=DIGEST)
        self.assertEqual(stale.exception.reason, "hosted_status_stale")
        self.progress(status="rendering", article=article(), revision_digest=DIGEST)
        with self.assertRaises(hosted.HostedPublishError) as unverified:
            self.progress(status="live", verified_digest="e" * 64)
        self.assertEqual(unverified.exception.reason, "hosted_live_unverified")
        self.written.refresh_from_db()
        self.assertNotEqual(self.written.publish_status, ArticlePublishStatus.LIVE)

    def test_two_articles_cannot_share_one_address(self):
        other = ContentFactoryRun.objects.create(run_id="other-run", domain=self.org.domain, organization=self.org,
                                                 workflow="direct_generate", status="completed", github_repo="", result={})
        for run in (self.run, other):
            hosted.request_publication(site=self.site, run=run, user=self.user)
        self.progress(status="rendering", article=article(), revision_digest=DIGEST)
        with self.assertRaises(hosted.HostedPublishError) as taken:
            hosted.record_progress(domain=self.site.domain, run_id=other.run_id,
                                   data={"status": "rendering", "article": article(), "revision_digest": DIGEST})
        self.assertEqual(taken.exception.reason, "hosted_slug_taken")

    def test_a_live_article_keeps_its_last_revision_while_a_republish_is_queued_or_fails(self):
        hosted.request_publication(site=self.site, run=self.run, user=self.user)
        self.progress(status="rendering", article=article(), revision_digest=DIGEST)
        self.progress(status="live", verified_digest=DIGEST)
        hosted.request_publication(site=self.site, run=self.run, user=self.user)
        self.assertEqual([row["status"] for row in hosted.site_render_state(self.site)["publications"]], ["pending"])
        self.progress(status="rendering")
        self.progress(status="failed", failure_reason="cloudflare_refused")
        self.assertEqual(len(hosted.site_render_state(self.site)["publications"]), 1)

    def test_service_endpoints_need_the_service_key(self):
        request = self.factory.get("/synthetic", {"domain": "https://www.pawshare.example.test/blog/"})
        self.assertEqual(HostedArticleSiteServiceView.as_view()(request).status_code, 403)
        request = self.factory.get("/synthetic", {"domain": "https://www.pawshare.example.test/blog/"}, HTTP_X_API_KEY=KEY)
        response = HostedArticleSiteServiceView.as_view()(request)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["basePath"], "/blog/")
        hosted.request_publication(site=self.site, run=self.run, user=self.user)
        request = self.factory.put("/synthetic", {"domain": self.site.domain, "status": "rendering", "article": article(),
                                                  "revision_digest": DIGEST}, format="json", HTTP_X_API_KEY=KEY)
        response = HostedArticlePublicationServiceView.as_view()(request, run_id=self.run.run_id)
        self.assertEqual((response.status_code, response.data["status"], response.data["slug"]), (200, "rendering", "dog-friendly-cafes"))


@override_settings(ROO_API_KEY=KEY, INTERNAL_API_KEY=KEY, ARTICLES_HOSTED_SITES_ENABLED=True)
class HostedArticleSiteLifecycleTests(TestCase):
    def setUp(self):
        self.org = Organization.objects.create(domain="lifecycle.example.test", name="Synthetic lifecycle")
        self.factory = APIRequestFactory()

    def test_a_site_is_registered_as_draft_deployed_then_activated(self):
        import json
        import tempfile
        from io import StringIO

        from django.core.management import call_command
        from django.core.management.base import CommandError

        build = {"registry_path": "src/articles/articles.json", "body_dir": "src/articles/content",
                 "build_command": ["npm", "run", "build"], "output_dir": "dist", "publish_prefix": "blog/"}
        profile = {"domain": self.org.domain, "base_path": "/blog/", "build": {**build, "build_command": "npm run build"}}
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as fh:
            json.dump(profile, fh)
        with self.assertRaises(CommandError):
            call_command("hosted_article_site", "register", self.org.domain, profile=fh.name, stdout=StringIO())
        with open(fh.name, "w") as rewrite:
            json.dump({**profile, "build": build}, rewrite)
        call_command("hosted_article_site", "register", "https://www." + self.org.domain, profile=fh.name, stdout=StringIO())
        site = HostedArticleSite.objects.get(domain=self.org.domain)
        self.assertEqual((site.state, site.organization, site.profile["build"]), ("draft", self.org, build))
        with self.assertRaises(CommandError):
            call_command("hosted_article_site", "activate", self.org.domain, stdout=StringIO())
        request = self.factory.put("/synthetic", {"domain": self.org.domain, "proxy_verified": True,
            "deployment": {"worker_name": "mlai-blog-y", "url": "https://mlai-blog-y.mlai.workers.dev", "manifest_digest": "m" * 64}},
            format="json", HTTP_X_API_KEY=KEY)
        self.assertEqual(HostedArticleSiteServiceView.as_view()(request).status_code, 200)
        site.refresh_from_db()
        self.assertEqual(site.worker_url, "https://mlai-blog-y.mlai.workers.dev")
        self.assertIsNotNone(site.proxy_verified_at)
        call_command("hosted_article_site", "activate", self.org.domain, stdout=StringIO())
        site.refresh_from_db()
        self.assertEqual(site.state, "active")


@override_settings(ROO_API_KEY=KEY, INTERNAL_API_KEY=KEY, ARTICLES_HOSTED_SITES_ENABLED=True)
class HostedArticleSourceTests(TestCase):
    """Content Factory builds a hosted site from the exact commit MLAI reads from its connected repository."""

    SHA = "c" * 40

    def setUp(self):
        from .website_models import WebsiteConnection

        self.org = Organization.objects.create(domain="source.example.test", name="Synthetic source")
        self.site = HostedArticleSite.objects.create(organization=self.org, domain=self.org.domain)
        self.connection = WebsiteConnection.objects.create(
            organization=self.org, github_repo="Owner/site", repository_id=42, installation_id="7",
            site_url="https://www.source.example.test", branch="")
        self.factory = APIRequestFactory()

    def read(self, repository=None, commit=None):
        def get(url, **_kwargs):
            payload = commit if url.endswith("/commits/main") else repository
            return Mock(status_code=200, json=Mock(return_value=payload), raise_for_status=Mock())

        repository = repository or {"id": 42, "full_name": "owner/site", "default_branch": "main"}
        commit = commit or {"sha": self.SHA}
        request = self.factory.get("/synthetic", {"domain": self.site.domain}, HTTP_X_API_KEY=KEY)
        with patch("integrations.services.github_app.create_installation_access_token", return_value=Mock(token="t")) as mint, \
             patch("integrations.http_client.get", side_effect=get), patch("integrations.http_client.delete") as revoke:
            response = HostedArticleSiteSourceServiceView.as_view()(request)
        return response, mint, revoke

    def test_the_site_is_built_from_its_connected_repositorys_current_commit_with_a_read_token(self):
        response, mint, revoke = self.read()
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data, {"githubRepo": "Owner/site", "repositoryId": 42, "branch": "main", "sha": self.SHA})
        self.assertEqual(mint.call_args.kwargs["permission_mode"], "read")
        self.assertEqual(mint.call_args.kwargs["repository_id"], 42)
        revoke.assert_called_once()

    def test_a_different_repository_or_an_unreadable_commit_is_refused(self):
        response, _, revoke = self.read(repository={"id": 43, "full_name": "owner/site", "default_branch": "main"})
        self.assertEqual((response.status_code, response.data["reason"]), (409, "hosted_source_mismatch"))
        revoke.assert_called_once()
        response, _, _ = self.read(commit={"sha": "main"})
        self.assertEqual((response.status_code, response.data["reason"]), (503, "hosted_source_unavailable"))

    def test_the_source_must_be_one_connected_repository(self):
        from .website_models import WebsiteConnection

        WebsiteConnection.objects.create(organization=self.org, github_repo="owner/other", installation_id="7",
                                         site_url="https://other.example.test")
        self.assertEqual(self.read()[0].status_code, 200)
        copy = WebsiteConnection.objects.create(organization=self.org, github_repo="owner/copy", installation_id="7",
                                                site_url="https://source.example.test")
        self.assertEqual(self.read()[0].data["reason"], "hosted_source_ambiguous")
        # The website the organisation selected in MLAI is the source.
        from .models import OrganizationContentConfig
        config = OrganizationContentConfig.objects.create(organization=self.org, website_connection=self.connection)
        self.assertEqual(self.read()[0].data["githubRepo"], "Owner/site")
        config.website_connection = copy
        config.save(update_fields=["website_connection"])
        self.assertEqual(self.read()[1].call_args.kwargs["repository"], "owner/copy")
        config.delete()
        WebsiteConnection.objects.filter(organization=self.org).update(state="disconnected")
        self.assertEqual(self.read()[0].data["reason"], "hosted_source_not_connected")
        request = self.factory.get("/synthetic", {"domain": "missing.example.test"}, HTTP_X_API_KEY=KEY)
        self.assertEqual(HostedArticleSiteSourceServiceView.as_view()(request).status_code, 404)
        request = self.factory.get("/synthetic", {"domain": self.site.domain})
        self.assertEqual(HostedArticleSiteSourceServiceView.as_view()(request).status_code, 403)


@override_settings(ROO_API_KEY=KEY, INTERNAL_API_KEY=KEY, ARTICLES_HOSTED_SITES_ENABLED=True)
class HostedArticleGateTests(TestCase):
    """A hosted site writes repo-free drafts and publishes them through MLAI, not a pull request."""

    def setUp(self):
        from .models import OrganizationContentConfig
        from .website_models import WebsiteConnection

        self.user = get_user_model().objects.create_user(email="gates@example.test")
        self.org = Organization.objects.create(domain="gates.example.test", name="Synthetic gates")
        website = WebsiteConnection.objects.create(organization=self.org, authorized_by=self.user, github_repo="example/site",
                                                   repository_id=1, installation_id="1", branch="main")
        self.config = OrganizationContentConfig.objects.create(organization=self.org, github_repo="example/site",
                                                               website_connection=website, article_delivery_mode="review_draft")
        self.site = HostedArticleSite.objects.create(organization=self.org, domain=self.org.domain, state="draft", worker_url="https://w.test")

    def test_only_an_active_hosted_site_turns_a_connected_website_into_repo_free_publishing(self):
        from .activation import article_capabilities
        from .portable_drafts import has_connected_website, website_preview_delivery_mode
        from .vibe_marketing_views import _effective_article_delivery_mode

        self.assertTrue(has_connected_website(self.config))
        self.assertEqual(website_preview_delivery_mode(self.config, "content_only"), "review_draft")
        self.assertFalse(hosted.hosted_capabilities(self.config)["canPublishHostedArticle"])
        self.site.state = "active"
        self.site.save(update_fields=["state"])
        self.assertFalse(has_connected_website(self.config))
        self.assertEqual(website_preview_delivery_mode(self.config, "content_only"), "content_only")
        self.assertEqual(_effective_article_delivery_mode(self.config), "content_only")
        caps = hosted.hosted_capabilities(self.config)
        self.assertEqual(caps, {"canPublishHostedArticle": True, "hostedSite": {"state": "active", "baseUrl": "https://gates.example.test/blog/", "proxyVerified": False}})
        with patch("content_factory.activation.get_content_factory_content_island_topic_cost_points", return_value=1), \
             patch("content_factory.activation.get_content_factory_article_cost_points", return_value=1):
            projected = article_capabilities(self.config, domain=self.org.domain)
        self.assertTrue(projected["canPublishHostedArticle"])
        self.assertFalse(projected["canPublishArticle"])

    def test_the_publish_step_follows_the_hosted_publication(self):
        self.site.state = "active"
        self.site.save(update_fields=["state"])
        run = ContentFactoryRun.objects.create(run_id="gate-run", domain=self.org.domain, organization=self.org, workflow="direct_generate",
                                               status="completed", github_repo="", result={},
                                               run_request={"delivery_mode": "content_only", "delivery_mode_confirmed": True})
        step = hosted.hosted_publish_step(run, self.config)
        self.assertEqual((step["status"], step["intent"], step["label"]), ("ready", "hosted-publish", "Publish article"))
        publication = HostedArticlePublication.objects.create(site=self.site, run_id=run.run_id, slug="a-story", status="deployed")
        self.assertEqual(hosted.hosted_publish_step(run, self.config)["status"], "running")
        publication.status = "live"
        publication.save(update_fields=["status"])
        live = hosted.hosted_publish_step(run, self.config)
        self.assertEqual((live["status"], live["href"]), ("complete", "https://gates.example.test/blog/a-story/"))
        repo_run = ContentFactoryRun.objects.create(run_id="repo-run", domain=self.org.domain, organization=self.org, workflow="direct_generate",
                                                    status="completed", github_repo="example/site", result={}, run_request={"delivery_mode": "review_draft"})
        self.assertIsNone(hosted.hosted_publish_step(repo_run, self.config))


class HostedArticleRolloutTests(TestCase):
    def test_with_the_rollout_off_an_active_site_changes_nothing(self):
        org = Organization.objects.create(domain="rollout.example.test", name="Synthetic rollout")
        HostedArticleSite.objects.create(organization=org, domain=org.domain, state="active")
        config = type("Config", (), {"organization": org})()
        self.assertIsNone(hosted.active_site(org))
        self.assertFalse(hosted.hosted_site_active(config))
        self.assertEqual(hosted.hosted_capabilities(config), {})
