"""P1.10: copy receipt, posted link and the article zip (IF-P1-13).

All three sit behind ARTICLES_DRAFT_TOOLS_ENABLED and the new-journey cohort,
are scoped to the selected company's runs, are for the startup owner only, and
never approve or charge a run. Legacy orgs get 404 with every P1 flag on.
"""
import io
import json
import zipfile
from copy import deepcopy
from unittest import mock
from uuid import uuid4

from django.test import SimpleTestCase, TestCase, override_settings
from django.urls import resolve
from rest_framework.test import APIClient

from core.models import User
from founder_tools.models import VibeRaisingCompany, VibeRaisingProfile
from roo.models import Ledger
from workflow_runs.models import ContentFactoryRun

from . import founder_draft_views as tools
from .founder_journey_testing import ALL_P1_FLAGS_ON, NAMED_LEGACY_ORGS, make_named_legacy_org, make_new_journey_org
from .models import OrganizationContentConfig
from .tests_founder_draft_export import snapshot
from .vibe_marketing_views import VibeMarketingRunControlView
from .website_contract import WebsiteAuthorityError

FLAGS = ALL_P1_FLAGS_ON
TOOLS_OFF = {**ALL_P1_FLAGS_ON, "ARTICLES_DRAFT_TOOLS_ENABLED": False}
V2_OFF = {**ALL_P1_FLAGS_ON, "MY_STARTUP_FOUNDER_JOURNEY_V2_ENABLED": False}
PREFIXES = ("/api/v1/vibe-marketing/", "/api/v1/my-startup/vibe-marketing/")
IMAGE = "https://firebasestorage.googleapis.com/v0/b/mlai/o/{}.png?alt=media&token=t"
ARTICLE_SENTENCES = (
    "Starting a small business blog takes less time than most founders expect each week. "
    "The first step is choosing one question your customers ask you over and over again. "
    "Answer that question in plain words and add one example from your own work with them. "
    "Publish it where your customers already look, then share the link with three of them."
)


class RoutingTests(SimpleTestCase):
    def test_new_routes_resolve_to_the_draft_tools_on_both_prefixes(self):
        expected = {"copied": tools.FounderDraftCopiedView, "posted-link": tools.FounderDraftPostedLinkView,
                    "article/download": tools.FounderDraftDownloadView}
        for prefix in PREFIXES:
            for route, view in expected.items():
                for slash in ("", "/"):
                    with self.subTest(prefix=prefix, route=route, slash=slash):
                        match = resolve(f"{prefix}runs/run-abc/{route}{slash}")
                        self.assertTrue(issubclass(match.func.view_class, view))
                        self.assertFalse(issubclass(match.func.view_class, VibeMarketingRunControlView))
                        self.assertEqual(match.kwargs, {"run_id": "run-abc"})

    def test_existing_run_actions_still_resolve_as_before(self):
        for action in ("approve", "cancel", "publish-pr"):
            for slash in ("", "/"):
                match = resolve(f"/api/v1/vibe-marketing/runs/run-abc/{action}{slash}")
                self.assertIs(match.func.view_class, VibeMarketingRunControlView)
                self.assertEqual(match.kwargs, {"run_id": "run-abc", "action": action})
                facade = resolve(f"/api/v1/my-startup/vibe-marketing/runs/run-abc/{action}{slash}").func.view_class
                self.assertFalse(issubclass(facade, (tools.FounderDraftCopiedView, tools.FounderDraftPostedLinkView,
                                                      tools.FounderDraftDownloadView)))
        self.assertTrue(resolve("/api/v1/vibe-marketing/runs/run-abc/article-review/").func.view_class.__name__.endswith(
            "ArticleReviewView"))


class PostedUrlTests(SimpleTestCase):
    def test_only_https_links_to_public_hosts_are_accepted(self):
        self.assertEqual(tools.clean_posted_url(" https://medium.com/@me/post "), ("https://medium.com/@me/post", "medium.com"))
        for value in ("http://example.com/post", "ftp://example.com/x", "https://localhost/post", "https://127.0.0.1/post",
                      "https://10.0.0.8/post", "https://[::1]/post", "https://169.254.169.254/latest", "https://intranet/post",
                      "https://blog.corp/post", "https://user:pass@example.com/post", "https://example.com:8443/post",
                      "https://example.com/" + "a" * 2048, "https://exa mple.com/post", "", None, 42):
            with self.subTest(value=value):
                self.assertIsNone(tools.clean_posted_url(value))
        self.assertIsNotNone(tools.clean_posted_url("https://example.com/" + "a" * (2048 - len("https://example.com/"))))


def _run(org, *, status="completed", delivery_mode="content_only", workflow="article_generation"):
    return ContentFactoryRun.objects.create(
        run_id=f"run-{uuid4().hex[:12]}", workflow=workflow, domain=org.domain, organization=org, status=status,
        run_request={"delivery_mode": delivery_mode, "delivery_mode_confirmed": True},
        result={"delivery_mode": delivery_mode})


def _review(run_id, *, revision="rev-1", media=None, html=None):
    review = snapshot(run_id, revision=revision)
    if media is not None:
        review["articleExport"]["media"] = media
    if html is not None:
        review["articleExport"]["html"] = html
    return review


def _posted(org):
    config = OrganizationContentConfig.objects.get(organization=org)
    return (config.pillar_strategy or {}).get("founder_posted_articles") or {}


@mock.patch("content_factory.founder_draft_views.views._latest_review_ready_component_revision", return_value=None)
class DraftToolTests(TestCase):
    def setUp(self):
        # A public-looking domain: links on a reserved .test host are refused.
        self.built = make_new_journey_org(domain=f"site-{uuid4().hex[:8]}.example.com")
        self.org = self.built.organization
        self.run = _run(self.org)

    def client_for(self, user):
        client = APIClient()
        client.force_authenticate(user)
        return client

    def call(self, method, route, *, run=None, user=None, body=None, company=None, review=None, prefix=PREFIXES[0]):
        run = run or self.run
        company = company or self.built.company
        url = f"{prefix}runs/{run.run_id}/{route}/?company_id={company.pk}"
        client = self.client_for(user or self.built.owner)
        remote = mock.patch("content_factory.article_review_views.remote_review",
                            return_value=deepcopy(review if review is not None else _review(run.run_id)))
        with remote as patched:
            response = client.post(url, body or {}, format="json") if method == "post" else client.get(url)
        response.remote_calls = patched.call_count
        return response

    # -- scoping -----------------------------------------------------------

    @override_settings(**FLAGS)
    def test_other_founders_and_other_companies_runs_are_not_found(self, _latest):
        stranger = make_new_journey_org()
        foreign = _run(stranger.organization)
        second_profile = VibeRaisingProfile.objects.get(user=self.built.owner)
        other_org = make_new_journey_org().organization
        own_other_company = VibeRaisingCompany.objects.create(profile=second_profile, organization=other_org,
                                                              name="Second", domain=other_org.domain)
        for route, method in (("copied", "post"), ("posted-link", "post"), ("article/download", "get")):
            with self.subTest(route=route):
                self.assertEqual(self.call(method, route, run=foreign, body={"revision": "rev-1"}).status_code, 404)
                self.assertEqual(self.call(method, route, company=own_other_company, body={"revision": "rev-1"}).status_code, 404)

    @override_settings(**FLAGS)
    def test_a_member_who_is_not_the_owner_is_refused(self, _latest):
        member = User.objects.create_user(email=f"member-{uuid4().hex[:8]}@synthetic.example.test")
        profile = VibeRaisingProfile.objects.create(user=member, role=VibeRaisingProfile.ROLE_FOUNDER)
        company = VibeRaisingCompany.objects.create(profile=profile, organization=self.org, name="Member", domain=self.org.domain)
        for route, method in (("copied", "post"), ("posted-link", "post"), ("article/download", "get")):
            with self.subTest(route=route):
                response = self.call(method, route, user=member, company=company,
                                     body={"revision": "rev-1", "url": "https://medium.com/@me/x"})
                self.assertEqual(response.status_code, 403)
                self.assertEqual(response.json(), {"detail": "Only the startup owner can do this.", "reason": "not_owner"})
        self.assertEqual(_posted(self.org), {})

    # -- copy receipt -------------------------------------------------------

    @override_settings(**FLAGS)
    def test_copy_receipt_is_stored_with_its_revision_and_repeats_return_it(self, _latest):
        before = (self.run.status, self.run.approval_state, deepcopy(self.run.result), Ledger.objects.count())
        with mock.patch("content_factory.vibe_marketing_views.control_article_run") as approve, \
                self.assertLogs("content_factory.founder_draft_views", "INFO") as logs:
            first = self.call("post", "copied", body={"revision": "rev-1"})
            repeat = self.call("post", "copied", body={"revision": "rev-1"}, prefix=PREFIXES[1])
        approve.assert_not_called()
        self.assertEqual(first.status_code, 200)
        self.assertEqual(first.json()["copied"]["revision"], "rev-1")
        self.assertEqual(repeat.status_code, 200)
        self.assertEqual(repeat.json(), first.json())
        self.assertEqual(repeat.remote_calls, 0)
        self.assertEqual(_posted(self.org)[self.run.run_id]["copiedRevision"], "rev-1")
        self.assertEqual(sum("founder_draft_copied" in line for line in logs.output), 1)
        self.run.refresh_from_db()
        self.assertEqual((self.run.status, self.run.approval_state, self.run.result, Ledger.objects.count()), before)

    @override_settings(**FLAGS)
    def test_stale_revision_is_refused_and_a_new_revision_replaces_the_receipt(self, _latest):
        stale = self.call("post", "copied", body={"revision": "rev-0"})
        self.assertEqual(stale.status_code, 409)
        self.assertEqual(stale.json(), {"detail": "Your draft just changed. Copy it again.", "reason": "revision_changed"})
        self.assertEqual(self.call("post", "copied", body={}).json()["reason"], "revision_changed")
        self.assertEqual(self.call("post", "copied", body={"revision": "rev-1"}).status_code, 200)
        moved = self.call("post", "copied", body={"revision": "rev-2"}, review=_review(self.run.run_id, revision="rev-2"))
        self.assertEqual(moved.json()["copied"]["revision"], "rev-2")

    @override_settings(**FLAGS)
    def test_only_a_delivered_content_only_run_can_be_copied(self, _latest):
        not_ready = {"detail": "Your article isn't ready to copy yet.", "reason": "not_ready"}
        for run in (_run(self.org, status="running"), _run(self.org, delivery_mode="review_draft"),
                    _run(self.org, workflow="article_system_setup"), _run(self.org, status="cancelled")):
            with self.subTest(status=run.status, workflow=run.workflow):
                response = self.call("post", "copied", run=run, body={"revision": "rev-1"})
                self.assertEqual((response.status_code, response.json()), (409, not_ready))
        unready = _review(self.run.run_id)
        unready["articleExport"]["status"] = "unavailable"
        unready["articleExport"]["reasonCode"] = "preview_pending"
        response = self.call("post", "copied", body={"revision": "rev-1"}, review=unready)
        self.assertEqual((response.status_code, response.json()), (409, not_ready))
        self.assertEqual(_posted(self.org), {})

    @override_settings(**{**FLAGS, "ARTICLES_COPY_WITHOUT_PREVIEW_ENABLED": False})
    def test_copy_waits_for_the_preview_when_copy_without_preview_is_off(self, _latest):
        self.assertEqual(self.call("post", "copied", body={"revision": "rev-1"}).json()["reason"], "not_ready")

    # -- posted link --------------------------------------------------------

    @override_settings(**FLAGS)
    def test_other_hosts_are_stored_as_skipped_without_fetching_and_keep_the_receipt(self, _latest):
        self.call("post", "copied", body={"revision": "rev-1"})
        with mock.patch("content_factory.website_live_fetch.fetch_live_route") as fetch:
            response = self.call("post", "posted-link", body={"url": "https://www.linkedin.com/pulse/how-to-start"})
        fetch.assert_not_called()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"postedLink": {"url": "https://www.linkedin.com/pulse/how-to-start",
                                                          "check": "skipped", "checkedAt": None}})
        record = _posted(self.org)[self.run.run_id]
        self.assertEqual(record["copiedRevision"], "rev-1")
        self.assertTrue(record["postedAt"])

    @override_settings(**FLAGS)
    def test_invalid_and_private_links_are_refused(self, _latest):
        for url in ("http://example.com/post", "https://127.0.0.1/post", "https://localhost/post",
                    "https://example.com/" + "a" * 2050, "not a link"):
            with self.subTest(url=url[:40]):
                response = self.call("post", "posted-link", body={"url": url})
                self.assertEqual(response.status_code, 400)
                self.assertEqual(response.json(), {"detail": "That doesn't look like a web address.", "reason": "invalid_url"})
        self.assertEqual(_posted(self.org), {})

    def _own_page(self, body_html):
        canonical = f"https://{self.org.domain}/blog/how-to-start"
        return (f'<html><head><link rel="canonical" href="{canonical}"></head><body><nav>Home</nav>'
                f"{body_html}</body></html>").encode()

    @override_settings(**FLAGS)
    def test_own_site_links_are_checked_over_the_pinned_fetcher(self, _latest):
        html = "<p>" + ARTICLE_SENTENCES + "</p>"
        review = _review(self.run.run_id, html=html)
        exact = self._own_page(f"<article><h1>How to start</h1>{html}</article>")
        pasted = self._own_page(f"<main><h1>How to start</h1><p>By Sam, 3 min read</p>{html}<p>Share this</p></main>")
        unrelated = self._own_page("<article><h1>Our pricing</h1><p>" + "Plans start small and grow with you. " * 10 + "</p></article>")
        url = f"https://{self.org.domain}/blog/how-to-start?utm_source=chat"
        cases = (
            (mock.Mock(return_value=(exact, {})), "matched"),
            (mock.Mock(return_value=(pasted, {})), "matched"),
            (mock.Mock(return_value=(unrelated, {})), "not_matched"),
            (mock.Mock(side_effect=WebsiteAuthorityError("deployment_route_transient", "Try later.")), "pending"),
            (mock.Mock(side_effect=WebsiteAuthorityError("deployment_origin_invalid", "No.")), "not_matched"),
            (mock.Mock(side_effect=OSError("reset")), "pending"),
        )
        for fetch, expected in cases:
            with self.subTest(expected=expected), mock.patch("content_factory.website_live_fetch.fetch_live_route", fetch):
                response = self.call("post", "posted-link", body={"url": url}, review=review)
                self.assertEqual(response.status_code, 200)
                link = response.json()["postedLink"]
                self.assertEqual((link["url"], link["check"]), (url, expected))
                self.assertEqual(link["checkedAt"] is not None, expected in {"matched", "not_matched"})
                target, host = fetch.call_args.args
                self.assertEqual((target, host), (f"https://{self.org.domain}/blog/how-to-start", self.org.domain))

    @override_settings(**FLAGS)
    def test_a_subdomain_counts_as_the_own_site(self, _latest):
        fetch = mock.Mock(side_effect=WebsiteAuthorityError("deployment_route_transient", "Later."))
        with mock.patch("content_factory.website_live_fetch.fetch_live_route", fetch):
            link = self.call("post", "posted-link", body={"url": f"https://blog.{self.org.domain}/post"}).json()["postedLink"]
        self.assertEqual(link["check"], "pending")
        self.assertEqual(fetch.call_args.args, (f"https://blog.{self.org.domain}/post", f"blog.{self.org.domain}"))

    @override_settings(**FLAGS)
    def test_records_are_newest_first_and_capped_at_fifty(self, _latest):
        config = OrganizationContentConfig.objects.get(organization=self.org)
        config.pillar_strategy = {**config.pillar_strategy, "founder_posted_articles": {
            f"old-{index}": {"url": f"https://medium.com/p/{index}", "check": "skipped"} for index in range(55)}}
        config.save(update_fields=["pillar_strategy", "updated_at"])
        self.call("post", "posted-link", body={"url": "https://medium.com/@me/new"})
        posted = _posted(self.org)
        self.assertEqual(len(posted), 50)
        self.assertEqual(list(posted)[:2], [self.run.run_id, "old-0"])
        self.assertIn("founder_journey", OrganizationContentConfig.objects.get(organization=self.org).pillar_strategy)

    # -- zip ----------------------------------------------------------------

    @override_settings(**FLAGS)
    def test_zip_holds_the_article_and_allowed_images_and_lists_the_rest(self, _latest):
        media = [{"url": IMAGE.format(index), "alt": f"Image {index}", "caption": ""} for index in range(11)]
        media.insert(1, {"url": "https://cdn.example.com/photo.png", "alt": "External", "caption": ""})
        media.insert(2, {"url": IMAGE.format("big"), "alt": "Big", "caption": ""})
        fetched = []

        def fake_fetch(url, *, limit, deadline):
            fetched.append(url)
            if "big" in url:
                raise tools._ImageSkipped("larger than 8 MB")
            return b"\x89PNG" + url.encode(), "png"

        with mock.patch.object(tools, "fetch_image", side_effect=fake_fetch):
            response = self.call("get", "article/download", review=_review(self.run.run_id, media=media))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Type"], "application/zip")
        self.assertEqual(response["Content-Disposition"], 'attachment; filename="how-to-start.zip"')
        archive = zipfile.ZipFile(io.BytesIO(response.content))
        names = archive.namelist()
        self.assertEqual(names[:2], ["article.md", "article.html"])
        self.assertTrue(archive.read("article.md").decode().startswith("# How to start"))
        page = archive.read("article.html").decode()
        self.assertIn("<h2>Why</h2>", page)
        self.assertIn("<title>How to start</title>", page)
        self.assertEqual([name for name in names if name.endswith(".png")], [f"images/{index}.png" for index in range(1, 11)])
        readme = archive.read("images/README.txt").decode()
        self.assertIn("https://cdn.example.com/photo.png (not stored by MLAI)", readme)
        self.assertIn(f"{IMAGE.format('big')} (larger than 8 MB)", readme)
        self.assertIn(f"{IMAGE.format(10)} (more than 10 images)", readme)
        self.assertIn("1.png: Image 0", readme)
        self.assertNotIn("https://cdn.example.com/photo.png", fetched)
        self.assertEqual(len(fetched), 11)
        self.assertNotIn("\u2014", readme)
        self.assertNotIn("\u2013", readme)

    @override_settings(**FLAGS)
    def test_zip_is_refused_until_the_export_is_ready(self, _latest):
        unready = _review(self.run.run_id)
        unready["articleExport"]["status"] = "unavailable"
        response = self.call("get", "article/download", review=unready)
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json(), {"detail": "Your article isn't ready to download yet.", "reason": "not_ready"})

    # -- flags and the legacy cohort ---------------------------------------

    def test_every_endpoint_is_not_found_with_the_flag_off_or_v2_off(self, _latest):
        for flags in (TOOLS_OFF, V2_OFF):
            with override_settings(**flags):
                for route, method in (("copied", "post"), ("posted-link", "post"), ("article/download", "get")):
                    for prefix in PREFIXES:
                        with self.subTest(route=route, prefix=prefix, flags=flags is TOOLS_OFF):
                            response = self.call(method, route, prefix=prefix,
                                                 body={"revision": "rev-1", "url": "https://medium.com/@me/x"})
                            self.assertEqual(response.status_code, 404)
                            self.assertEqual(response.remote_calls, 0)
        self.assertEqual(_posted(self.org), {})

    @override_settings(**FLAGS)
    def test_legacy_orgs_get_not_found_with_every_flag_on(self, _latest):
        for name in NAMED_LEGACY_ORGS:
            for marker in (True, False):
                with self.subTest(org=name, marker=marker):
                    built = make_named_legacy_org(name, marker=marker, domain=f"{name}-{uuid4().hex[:6]}.example.test",
                                                  owner_email=f"{uuid4().hex[:8]}@synthetic.example.test")
                    run = _run(built.organization)
                    for route, method in (("copied", "post"), ("posted-link", "post"), ("article/download", "get")):
                        response = self.call(method, route, run=run, user=built.owner, company=built.company,
                                             body={"revision": "rev-1", "url": "https://medium.com/@me/x"})
                        self.assertEqual(response.status_code, 404)
                        self.assertEqual(response.json(), {"detail": "Not found."})
                        self.assertEqual(response.remote_calls, 0)
                    self.assertNotIn("founder_posted_articles",
                                     OrganizationContentConfig.objects.get(organization=built.organization).pillar_strategy)


class MarkdownFallbackTests(SimpleTestCase):
    def test_markdown_fallback_escapes_text_and_keeps_structure(self):
        html = tools.markdown_html("# Title\n\nSome **bold** and [a link](https://example.com/x) <script>.\n\n"
                                   "- one\n- two\n\n![Alt](https://example.com/a.png)")
        self.assertIn("<h1>Title</h1>", html)
        self.assertIn("<strong>bold</strong>", html)
        self.assertIn('<a href="https://example.com/x">a link</a>', html)
        self.assertIn("&lt;script&gt;", html)
        self.assertIn("<ul><li>one</li><li>two</li></ul>", html)
        self.assertIn('<img src="https://example.com/a.png" alt="Alt">', html)
        self.assertEqual(json.loads(json.dumps(tools.expected_article_html({"markdown": "Plain text.",
                                                                          "metadata": {"title": "T"}}))),
                         "<article><h1>T</h1><p>Plain text.</p></article>")
