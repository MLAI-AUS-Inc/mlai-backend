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
from .hosted_article_views import HostedArticlePublicationServiceView, HostedArticlePublishView, HostedArticleSiteServiceView
from .models import ArticlePublishStatus, WrittenArticle

DIGEST = "d" * 64
KEY = "synthetic-test-key"


def article(**changes):
    values = {"slug": "dog-friendly-cafes", "title": "Dog-friendly cafés", "excerpt": "Where to go.", "published_at": "2026-10-24",
              "author_name": "Pawshare Club", "body_html": "<h2>Before you go</h2><p>Call ahead.</p>", "categories": ["Pet Care Tips"],
              "tags": [], "cover": None, "structured_data": {"schema_version": 1, "json_ld": []}, "unexpected": "dropped"}
    values.update(changes)
    return values


@override_settings(ROO_API_KEY=KEY, INTERNAL_API_KEY=KEY)
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
                                                    workflow="direct_generate", status="completed", github_repo="", result={})
        self.written = WrittenArticle.objects.create(organization=self.org, title="Dog-friendly cafés", slug="dog-friendly-cafes",
                                                     category="Pet Care Tips", primary_keyword="dog friendly cafe", source_run_id=self.run.run_id)
        self.factory = APIRequestFactory()

    def publish(self, method="post", run_id="hosted-run", dispatch_status=202):
        request = getattr(self.factory, method)("/synthetic", {}, format="json")
        force_authenticate(request, user=self.user)
        with patch("content_factory.vibe_marketing_views._resolve_context_or_response", return_value=(self.context, None)), \
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
        self.assertEqual(post.call_args.kwargs["json"], {"domain": self.site.domain, "base_path": "/blog/", "publication_id": str(publication.pk)})

    def test_a_content_factory_outage_is_reported_and_recorded(self):
        response, _ = self.publish(dispatch_status=500)
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.data["status"], "failed")
        self.assertEqual(response.data["errorCode"], "hosted_dispatch_failed")

    def test_unfinished_articles_and_sites_without_hosting_cannot_publish(self):
        self.run.status = "running"
        self.run.save(update_fields=["status"])
        response, post = self.publish()
        self.assertEqual((response.status_code, response.data["code"]), (409, "article_not_ready"))
        post.assert_not_called()
        self.site.state = "paused"
        self.site.save(update_fields=["state"])
        response, post = self.publish()
        self.assertEqual((response.status_code, response.data["code"]), (409, "hosted_site_not_active"))
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
        self.assertEqual(stale.exception.code, "hosted_status_stale")
        self.progress(status="rendering", article=article(), revision_digest=DIGEST)
        with self.assertRaises(hosted.HostedPublishError) as unverified:
            self.progress(status="live", verified_digest="e" * 64)
        self.assertEqual(unverified.exception.code, "hosted_live_unverified")
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
        self.assertEqual(taken.exception.code, "hosted_slug_taken")

    def test_a_live_article_keeps_its_last_revision_while_a_republish_is_queued_or_fails(self):
        hosted.request_publication(site=self.site, run=self.run, user=self.user)
        self.progress(status="rendering", article=article(), revision_digest=DIGEST)
        self.progress(status="live", verified_digest=DIGEST)
        hosted.request_publication(site=self.site, run=self.run, user=self.user)
        self.assertEqual([row["status"] for row in hosted.site_render_state(self.site)["publications"]], ["pending"])
        self.progress(status="rendering")
        self.progress(status="failed", error_code="cloudflare_refused")
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
