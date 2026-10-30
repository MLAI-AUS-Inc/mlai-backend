"""P1.10: copy-ready export while the preview rebuilds, and ``founderDraft`` (IF-P1-12).

A content_only draft of a new-journey org can be copied while its preview is
still rebuilding. Superseded and mismatched revisions are still refused, and a
legacy org with the same review keeps today's fail-closed ``preview_pending``
and never sees ``founderDraft``, even with every P1 flag on.
"""
from copy import deepcopy
from unittest import mock
from uuid import uuid4

from django.test import SimpleTestCase, TestCase, override_settings
from rest_framework.test import APIClient

from workflow_runs.models import ContentFactoryRun

from .article_export import article_export
from .founder_journey_testing import ALL_P1_FLAGS_ON, NAMED_LEGACY_ORGS, make_named_legacy_org, make_new_journey_org
from .models import OrganizationContentConfig

FLAGS = ALL_P1_FLAGS_ON
V2_OFF = {**ALL_P1_FLAGS_ON, "MY_STARTUP_FOUNDER_JOURNEY_V2_ENABLED": False}
COPY_OFF = {**ALL_P1_FLAGS_ON, "ARTICLES_COPY_WITHOUT_PREVIEW_ENABLED": False}


def snapshot(run_id, *, pending=True, revision="rev-1", saved_revision=None, saved_status="ready", html=True):
    """A Content Factory review snapshot whose saved export is ready (CF's copy-ready export)."""
    export = {
        "version": 1, "status": saved_status, "runId": run_id, "revision": saved_revision or revision,
        "format": "markdown", "markdown": "# How to start\n\nA first paragraph about the topic.",
        "metadata": {"title": "How to start", "slug": "how-to-start", "description": "A short guide.",
                     "canonical_url": "https://example.test/blog/how-to-start"},
        "media": [{"url": "https://firebasestorage.googleapis.com/v0/b/x/o/a.png?alt=media&token=t", "alt": "A", "caption": ""}],
    }
    if saved_status != "ready":
        export["reasonCode"] = "preview_pending"
    if html:
        export.update(html='<h2>Why</h2><p><a href="https://example.test/blog/other">More</a></p>', metaTitle="How to start (guide)")
    if pending:
        export["previewPending"] = True
    return {"revision": revision, "previewPending": pending,
            "reviewFindings": [{"fieldId": "sections.0.body", "kind": "style", "message": "Shorter sentences read better."}],
            "articleExport": export}


class ArticleExportRelaxationTests(SimpleTestCase):
    def test_default_export_is_unchanged_and_fails_closed_while_pending(self):
        result = article_export(snapshot("run-1"), run_id="run-1")
        self.assertEqual(result["status"], "unavailable")
        self.assertEqual(result["reasonCode"], "preview_pending")
        ready = article_export(snapshot("run-1", pending=False), run_id="run-1")
        self.assertEqual(ready["status"], "ready")
        self.assertNotIn("html", ready)
        self.assertNotIn("metaTitle", ready)
        self.assertNotIn("previewPending", ready)

    def test_allowed_pending_export_is_ready_with_copy_formats(self):
        result = article_export(snapshot("run-1"), run_id="run-1", allow_preview_pending=True, copy_formats=True)
        self.assertEqual(result["status"], "ready")
        self.assertIs(result["previewPending"], True)
        self.assertIn("<h2>Why</h2>", result["html"])
        self.assertEqual(result["metaTitle"], "How to start (guide)")
        self.assertTrue(result["markdown"].startswith("# How to start"))

    def test_superseded_mismatched_and_unready_saved_exports_are_still_refused(self):
        refused = article_export(snapshot("run-1"), run_id="run-1", latest_run_id="run-2", allow_preview_pending=True)
        self.assertEqual(refused["reasonCode"], "revision_superseded")
        mismatch = article_export(snapshot("run-1", saved_revision="rev-0"), run_id="run-1", allow_preview_pending=True)
        self.assertEqual(mismatch["reasonCode"], "revision_mismatch")
        foreign = article_export(snapshot("run-9"), run_id="run-1", allow_preview_pending=True)
        self.assertEqual(foreign["reasonCode"], "revision_mismatch")
        # CF without its own copy-ready flag still marks the saved export pending.
        unready = article_export(snapshot("run-1", saved_status="unavailable"), run_id="run-1", allow_preview_pending=True)
        self.assertEqual((unready["status"], unready["reasonCode"]), ("unavailable", "preview_pending"))

    def test_only_a_literal_true_relaxes(self):
        for value in (1, "true", None):
            result = article_export(snapshot("run-1"), run_id="run-1", allow_preview_pending=value)
            self.assertEqual(result["reasonCode"], "preview_pending")
        unknown = snapshot("run-1")
        unknown["previewPending"] = None
        self.assertEqual(article_export(unknown, run_id="run-1", allow_preview_pending=True)["reasonCode"], "preview_pending")


def _content_only_run(org, *, status="completed", delivery_mode="content_only"):
    return ContentFactoryRun.objects.create(
        run_id=f"run-{uuid4().hex[:12]}", workflow="article_generation", domain=org.domain, organization=org,
        status=status, run_request={"delivery_mode": delivery_mode, "delivery_mode_confirmed": True},
        result={"delivery_mode": delivery_mode})


@mock.patch("content_factory.article_review_views.views._component_feedback_from_run", return_value={})
@mock.patch("content_factory.article_review_billing.image_regeneration_cost", return_value=0)
@mock.patch("content_factory.article_review_billing.reconcile_image_charges")
@mock.patch("content_factory.article_review_views.views._latest_review_ready_component_revision", return_value=None)
class ReviewViewFounderDraftTests(TestCase):
    def _get(self, built, run, review):
        client = APIClient()
        client.force_authenticate(built.owner)
        with mock.patch("content_factory.article_review_views.remote_review", return_value=deepcopy(review)):
            return client.get(f"/api/v1/vibe-marketing/runs/{run.run_id}/article-review/?company_id={built.company.pk}")

    def _set_profile(self, built, site_profile):
        config = OrganizationContentConfig.objects.get(organization=built.organization)
        strategy = dict(config.pillar_strategy or {})
        strategy["founder_first_profile"] = {"version": 1, "siteProfile": site_profile}
        config.pillar_strategy = strategy
        config.save(update_fields=["pillar_strategy", "updated_at"])

    @override_settings(**FLAGS)
    def test_new_journey_content_only_draft_exports_while_preview_pending(self, *_mocks):
        built = make_new_journey_org()
        self._set_profile(built, {"platform": "wix", "blogUrl": "https://example.test/blog"})
        run = _content_only_run(built.organization)
        response = self._get(built, run, snapshot(run.run_id))
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertIs(data["previewPending"], True)
        self.assertEqual(data["reviewFindings"][0]["kind"], "style")
        self.assertEqual(data["articleExport"]["status"], "ready")
        self.assertIn("<h2>Why</h2>", data["articleExport"]["html"])
        self.assertEqual(data["articleExport"]["metaTitle"], "How to start (guide)")
        self.assertEqual(data["founderDraft"], {
            "version": 1, "copyWhilePreviewPending": True, "tools": True, "postedLink": None, "copied": None,
            "platform": "wix", "blogUrl": "https://example.test/blog"})

    @override_settings(**FLAGS)
    def test_founder_draft_reads_receipt_and_link_and_ignores_unsafe_profile_values(self, *_mocks):
        built = make_new_journey_org()
        self._set_profile(built, {"platform": "<script>", "blogUrl": "http://example.test/blog"})
        run = _content_only_run(built.organization)
        config = OrganizationContentConfig.objects.get(organization=built.organization)
        config.pillar_strategy = {**config.pillar_strategy, "founder_posted_articles": {run.run_id: {
            "copiedAt": "2026-10-30T01:00:00+00:00", "copiedRevision": "rev-1",
            "url": "https://medium.com/@me/post", "check": "skipped", "checkedAt": None}}}
        config.save(update_fields=["pillar_strategy", "updated_at"])
        draft = self._get(built, run, snapshot(run.run_id)).json()["founderDraft"]
        self.assertEqual(draft["copied"], {"at": "2026-10-30T01:00:00+00:00", "revision": "rev-1"})
        self.assertEqual(draft["postedLink"], {"url": "https://medium.com/@me/post", "check": "skipped", "checkedAt": None})
        self.assertIsNone(draft["platform"])
        self.assertIsNone(draft["blogUrl"])

    @override_settings(**FLAGS)
    def test_review_draft_run_keeps_waiting_for_its_preview(self, *_mocks):
        built = make_new_journey_org()
        run = _content_only_run(built.organization, delivery_mode="review_draft")
        data = self._get(built, run, snapshot(run.run_id)).json()
        self.assertEqual(data["articleExport"]["reasonCode"], "preview_pending")
        self.assertIs(data["founderDraft"]["copyWhilePreviewPending"], False)

    @override_settings(**COPY_OFF)
    def test_copy_flag_off_keeps_fail_closed_export_for_new_journey(self, *_mocks):
        built = make_new_journey_org()
        run = _content_only_run(built.organization)
        data = self._get(built, run, snapshot(run.run_id)).json()
        self.assertEqual(data["articleExport"]["reasonCode"], "preview_pending")
        self.assertIs(data["founderDraft"]["copyWhilePreviewPending"], False)
        ready = self._get(built, run, snapshot(run.run_id, pending=False)).json()["articleExport"]
        self.assertNotIn("html", ready)

    def test_legacy_orgs_fail_closed_without_founder_draft_with_every_flag_on(self, *_mocks):
        for name in NAMED_LEGACY_ORGS:
            for marker in (True, False):
                for flags in (FLAGS, V2_OFF):
                    with self.subTest(org=name, marker=marker, v2=flags["MY_STARTUP_FOUNDER_JOURNEY_V2_ENABLED"]), \
                            override_settings(**flags):
                        built = make_named_legacy_org(name, marker=marker, domain=f"{name}-{uuid4().hex[:6]}.example.test",
                                                      owner_email=f"{uuid4().hex[:8]}@synthetic.example.test")
                        run = _content_only_run(built.organization)
                        data = self._get(built, run, snapshot(run.run_id)).json()
                        self.assertEqual(data["articleExport"]["status"], "unavailable")
                        self.assertEqual(data["articleExport"]["reasonCode"], "preview_pending")
                        self.assertNotIn("founderDraft", data)
                        ready = self._get(built, run, snapshot(run.run_id, pending=False)).json()
                        self.assertEqual(ready["articleExport"]["status"], "ready")
                        self.assertNotIn("html", ready["articleExport"])
                        self.assertNotIn("metaTitle", ready["articleExport"])
                        self.assertNotIn("previewPending", ready["articleExport"])
                        self.assertNotIn("founderDraft", ready)

    @override_settings(**V2_OFF)
    def test_new_journey_org_with_v2_off_gets_todays_review(self, *_mocks):
        built = make_new_journey_org()
        run = _content_only_run(built.organization)
        data = self._get(built, run, snapshot(run.run_id)).json()
        self.assertEqual(data["articleExport"]["reasonCode"], "preview_pending")
        self.assertNotIn("founderDraft", data)
