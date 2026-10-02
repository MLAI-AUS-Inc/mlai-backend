"""Database-free regressions for explicit public sharing and anonymous reading."""
from contextlib import ExitStack
from datetime import date
from types import SimpleNamespace as Obj
from unittest.mock import MagicMock, patch

from django.http import Http404
from django.test import SimpleTestCase, override_settings
from rest_framework.test import APIRequestFactory
from rest_framework.request import Request

from community_chat.startups import publication, public_views, views
from startup_updates import revisions, services
from vibe_raising.audience_visibility import normalize_audience_visibility
from vibe_raising.serializers import VibeRaisingMonthlyUpdateUpsertSerializer


class AudienceTests(SimpleTestCase):
    def test_all_three_audiences_are_distinct_and_public_is_exclusive(self):
        for audience in ("public", "community", "just_me"):
            self.assertEqual(normalize_audience_visibility(audience), [audience])
            serializer = VibeRaisingMonthlyUpdateUpsertSerializer(data={
                "month": "September", "year": 2026, "audienceVisibility": audience,
            })
            self.assertTrue(serializer.is_valid(), serializer.errors)
            self.assertEqual(serializer.validated_data["audienceVisibility"], [audience])
        for audience in (["public", "community"], ["public", "just_me"], ["public", "investors"]):
            with self.assertRaises(ValueError):
                normalize_audience_visibility(audience)

    def test_public_revision_approval_requires_its_own_exact_disclosure(self):
        revision = Obj(pk=5, content_hash="reviewed", structured_memo={"_audience_visibility": ["public"]}, validation={"groundedness_status": "passed"}, snapshot=Obj(payload={}))
        draft = Obj(pk=1, month=date(2026, 9, 1), current_revision=revision, published_revision_id=None,
            published_at=None, first_published_at=None, save=MagicMock())
        with patch.object(revisions.MonthlyUpdateDraft, "objects") as drafts, patch.object(revisions.MonthlyUpdateApproval, "objects") as approvals:
            drafts.select_for_update.return_value.get.return_value = draft
            approvals.get_or_create.return_value = (Obj(), True)
            for audience in (["community"], ["just_me"]):
                with self.assertRaises(revisions.RevisionConflict):
                    revisions.approve_and_publish.__wrapped__(draft, actor=Obj(pk=9),
                        revision_id=5, revision_hash="reviewed", audience_visibility=audience)
            approvals.get_or_create.assert_not_called()
            result = revisions.approve_and_publish.__wrapped__(draft, actor=Obj(pk=9),
                revision_id=5, revision_hash="reviewed", audience_visibility=["public"])
        self.assertIs(result.published_revision, revision)
        self.assertEqual(result.audience_visibility, ["public"])

    def test_public_draft_discloses_only_selected_metrics_and_never_publishes_on_save(self):
        snapshot = Obj(pk=2, organization_id=1, month=date(2026, 9, 1), content_hash="snapshot", payload={
            "metrics": [
                {"key": "revenue", "label": "Revenue", "display_value": "AUD 100"},
                {"key": "cashBalance", "label": "Cash balance", "display_value": "AUD 999"},
            ], "metric_history": {"revenue": [100], "cashBalance": [999]},
            "charts": {"private": "chart"}, "events": [], "source_coverage": {},
        })
        draft = Obj(pk=1, organization_id=1, month=snapshot.month, current_revision=None,
            published_at=None, published_revision_id=None, structured_memo={}, update_date=date(2026, 9, 30),
            revisions=MagicMock(), save=MagicMock())
        draft.revisions.aggregate.return_value = {"n": 0}
        memo = {"summary": "- We shipped.", "kpi_snapshot": [{"metric_key": "revenue"}],
            "display_config": {"full_metric_keys": ["revenue"]}}
        with patch.object(revisions.MonthlyUpdateDraft, "objects") as drafts, patch.object(revisions.MonthlyUpdateRevision, "objects") as rows, patch("startup_updates.covers.inherit_cover", side_effect=lambda value, *_: value), patch("vibe_raising.progress.charts_for_revision", return_value=None), patch.object(revisions, "render_metric_claims", side_effect=lambda value, _: value), patch("startup_updates.services.render_monthly_update_markdown", return_value="- We shipped."):
            drafts.select_for_update.return_value.get.return_value = draft
            revisions.save_revision.__wrapped__(draft, memo, snapshot=snapshot, audience="public")
        saved = rows.create.call_args.kwargs
        self.assertEqual(saved["audience"], "public")
        self.assertEqual(saved["structured_memo"]["_audience_visibility"], ["public"])
        self.assertEqual([metric["metric_key"] for metric in saved["structured_memo"]["kpi_snapshot"]], ["revenue"])
        self.assertEqual(saved["structured_memo"]["metric_history"], {"revenue": [100]})
        self.assertIsNone(saved["structured_memo"]["financial_snapshot"])
        self.assertIsNone(draft.published_revision_id)

    def test_generation_pins_the_selected_audience_without_publishing(self):
        binding = Obj(user=Obj(), user_id=9, organization=Obj(), google_connection=None, id=3)
        organization = Obj(id=1, domain="startup.invalid", startup_profile=None)
        with ExitStack() as stack:
            stack.enter_context(patch("integrations.services.external_connectors.google_connection_for_org", return_value=None))
            stack.enter_context(patch.object(services, "get_open_startup_update_run", return_value=None))
            stack.enter_context(patch.object(services, "supersede_conflicting_startup_update_runs"))
            stack.enter_context(patch.object(services, "build_external_context_for_sources", return_value={}))
            stack.enter_context(patch.object(services, "reconcile_startup_update_run_source_steps"))
            stack.enter_context(patch.object(services.transaction, "atomic"))
            rows = stack.enter_context(patch.object(services.ContentFactoryRun, "objects"))
            services.create_startup_update_run(organization=organization, binding=binding,
                input_sources=["manual_documents"], manual_summary="We shipped.",
                audience_visibility=["public"], target_month=date(2026, 9, 1))
        payload = rows.create.call_args.kwargs["run_request"]
        self.assertEqual(payload["audience_visibility"], ["public"])
        self.assertNotIn("published_at", payload)


@override_settings(COMMUNITY_CHAT_STARTUP_UPDATES_ENABLED=True)
class PublicReaderTests(SimpleTestCase):
    def setUp(self):
        self.factory = APIRequestFactory()
        self.view = public_views.PublicUpdateView.as_view(throttle_classes=())

    def test_query_requires_current_public_approval_and_matching_hash(self):
        with patch.object(publication.MonthlyUpdateDraft, "objects") as manager:
            publication.approved_updates("public")
        clause = manager.filter.call_args.args[0]
        self.assertIn(("published_revision__audience", "public"), clause.children[-1].children)
        self.assertIn(("published_revision__approval__audience_visibility", ["public"]), clause.children[-1].children)
        filters = manager.filter.call_args.kwargs
        self.assertEqual(filters["published_revision__approval__content_hash"].name, "published_revision__content_hash")
        self.assertIs(filters["published_at__isnull"], False)
        with self.assertRaises(ValueError):
            publication.approved_updates("private")

    def test_owner_link_only_exists_for_the_approved_public_version(self):
        draft = Obj(pk=7, month=date(2026, 9, 1), published_revision_id=3)
        owner_view = views.UpdateView()
        owner_view.company = Obj(organization=Obj())
        for published, publicly_approved in ((True, True), (True, False), (False, True)):
            request = Request(self.factory.get("/", {"version": "published"} if published else {}))
            with patch.object(views.MonthlyUpdateDraft, "objects"), patch.object(views, "get_object_or_404", return_value=draft), patch.object(views, "update_payload", return_value={"id": 7}), patch.object(views, "approved_updates") as lookup:
                lookup.return_value.filter.return_value.exists.return_value = publicly_approved
                response = owner_view.get(request, update_id=7)
            if published and publicly_approved:
                self.assertEqual(response.data["update"]["publicUrl"], "http://testserver/api/v1/community-chat/startups/public/7/")
            else:
                self.assertNotIn("publicUrl", response.data["update"])

    def test_anonymous_reader_uses_published_projection_and_escapes_narrative(self):
        draft = Obj()
        update = {"startup": {"name": "Example"}, "month": "September 2026", "summary": "- <script>private attack</script>\n- Shipped a release.", "metrics": {"revenue": "AUD 100"}, "metricEvidence": {}}
        with patch.object(public_views, "approved_updates") as lookup, patch.object(public_views, "get_object_or_404", return_value=draft), patch.object(public_views, "update_payload", return_value=update) as project:
            response = self.view(self.factory.get("/", HTTP_ACCEPT="text/html"), update_id=1)
        self.assertEqual(response.status_code, 200)
        lookup.assert_called_once_with("public")
        project.assert_called_once_with(draft, published=True, community=True)
        html = response.content.decode()
        self.assertIn("&lt;script&gt;private attack&lt;/script&gt;", html)
        self.assertNotIn("<script>", html)
        self.assertIn("<li>Shipped a release.</li>", html)
        self.assertEqual(response["Cache-Control"], "no-store")

    def test_multiline_and_legacy_points_match_the_editor(self):
        sections = public_views.public_sections({"summary": "- First point\n  continuation line\n+ Second point\n\nLegacy third point"})
        self.assertEqual(sections, [{"label": "Summary", "points": ["First point\ncontinuation line", "Second point", "Legacy third point"]}])

    def test_private_missing_and_revoked_publications_have_no_anonymous_fallback(self):
        with patch.object(public_views, "approved_updates") as lookup, patch.object(public_views, "get_object_or_404", side_effect=Http404), patch.object(public_views, "update_payload") as project:
            response = self.view(self.factory.get("/"), update_id=1)
        self.assertEqual(response.status_code, 404)
        lookup.assert_called_once_with("public")
        project.assert_not_called()

    def test_anonymous_route_is_read_only(self):
        for method in ("post", "put", "patch", "delete"):
            response = self.view(getattr(self.factory, method)("/", {}, format="json"), update_id=1)
            self.assertEqual(response.status_code, 405)

    @override_settings(COMMUNITY_CHAT_STARTUP_UPDATES_ENABLED=False)
    def test_feature_flag_also_closes_the_public_reader(self):
        with patch.object(public_views, "approved_updates") as lookup:
            response = self.view(self.factory.get("/"), update_id=1)
        self.assertEqual(response.status_code, 404)
        lookup.assert_not_called()
