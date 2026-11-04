"""Owner rule (17 Oct 2026): a connected website always delivers a website preview.

Database-free: run with scripts/test_without_database.py. ORM, provider and
billing seams are fakes, so these cases prove delivery-mode policy and control
flow, not persistence or real website authority.
"""
from contextlib import ExitStack, contextmanager, nullcontext
import importlib
from types import SimpleNamespace as Obj
from unittest.mock import Mock, patch
import uuid

from django.test import SimpleTestCase, override_settings
from rest_framework.response import Response

from content_factory import portable_drafts
from content_factory import vibe_marketing_views as views
from content_factory.website_connections import owner_operation_contract

CONNECTION_ID = str(uuid.UUID(int=551))
READY = {"canGenerateArticle": True, "reasonCode": "", "nextStep": "articles"}
UNREADY = {"canGenerateArticle": False, "reasonCode": "integration_required", "nextStep": "articles"}
migration = importlib.import_module("content_factory.migrations.0046_connected_website_article_delivery_preview")


def config(*, state="connected", mode="content_only", **extra):
    connection = Obj(state=state) if state else None
    return Obj(website_connection_id=CONNECTION_ID if state else None, website_connection=connection,
               article_delivery_mode=mode, github_repo="founder/site" if state else "", **extra)


class ConnectedWebsitePolicyTests(SimpleTestCase):
    def test_only_a_live_or_paused_website_requires_preview(self):
        self.assertTrue(portable_drafts.has_connected_website(config(state="connected")))
        self.assertTrue(portable_drafts.has_connected_website(config(state="paused")))
        for state in ("disconnected", "revoked", None):
            with self.subTest(state=state):
                self.assertFalse(portable_drafts.has_connected_website(config(state=state)))
        self.assertFalse(portable_drafts.has_connected_website(None))

    def test_content_only_is_stored_as_review_draft_only_for_a_connected_website(self):
        self.assertEqual(portable_drafts.website_preview_delivery_mode(config(), "content_only"), "review_draft")
        self.assertEqual(portable_drafts.website_preview_delivery_mode(config(), "publish_code"), "publish_code")
        self.assertEqual(portable_drafts.website_preview_delivery_mode(config(state=None), "content_only"), "content_only")
        self.assertEqual(portable_drafts.website_preview_delivery_mode(config(state="disconnected"), "content_only"), "content_only")

    def test_every_explicit_or_confirmed_content_only_spelling_is_recognised(self):
        for payload in ({"deliveryMode": "content_only", "deliveryModeExplicit": "true"},
                        {"delivery_mode": "content_only", "delivery_mode_explicit": True},
                        {"deliveryMode": "content_only", "deliveryModeExplicit": False, "deliveryModeConfirmed": True}):
            with self.subTest(payload=payload):
                self.assertTrue(portable_drafts.requests_content_only(payload))
        for payload in ({"deliveryMode": "content_only"}, {"deliveryMode": "review_draft", "deliveryModeExplicit": True}, {}):
            with self.subTest(payload=payload):
                self.assertFalse(portable_drafts.requests_content_only(payload))


@override_settings(CONTENT_FACTORY_DEFAULT_ARTICLE_DELIVERY_MODE="")
class ReportedDeliveryModeTests(SimpleTestCase):
    """Rules 1 and 3: stored/effective/bootstrap modes never say content_only."""

    def effective(self, cfg, **kwargs):
        with patch.object(views, "_article_repo_is_review_capable", return_value=False):
            return views._effective_article_delivery_mode(cfg, **kwargs)

    def test_stored_content_only_reads_as_review_draft_for_a_connected_website(self):
        self.assertEqual(views._stored_article_delivery_mode(config()), "review_draft")
        self.assertEqual(views._stored_article_delivery_mode(config(state=None)), "content_only")

    def test_unready_connected_website_reports_review_draft_not_a_portable_downgrade(self):
        for mode in ("content_only", None, ""):
            with self.subTest(mode=mode):
                self.assertEqual(self.effective(config(mode=mode)), "review_draft")
                self.assertEqual(views._reported_article_delivery_mode(config(mode=mode)), "review_draft")
        self.assertEqual(self.effective(config(mode="publish_code")), "publish_code")

    def test_company_without_a_connected_website_keeps_content_only(self):
        for state in (None, "disconnected"):
            with self.subTest(state=state):
                self.assertEqual(self.effective(config(state=state)), "content_only")
                self.assertEqual(views._reported_article_delivery_mode(config(state=state, mode=None)), "content_only")

    def test_bootstrap_settings_report_review_draft_with_existing_readiness_flags(self):
        organization = Obj(id=1, name="Example", domain="example.test", company_linkedin_url="", competitors=[], seed_keywords=[])
        company = Obj(id=2, name="Example", domain="example.test", location="", abn="", avatar_url="", organization_id=None)
        context = Obj(organization=organization, company=company, profile=Obj(user=Obj(id=3, pk=3)))
        cfg = config(brand_name="", company_context="", daily_discovery_enabled=False, daily_discovery_priority=0,
                     default_timezone="UTC", github_connection_state="connected", authors=[], default_author_id=None)
        stubs = {
            "_get_config": cfg, "_latest_runs_for_org": [], "_recent_discovery_topic_runs_for_org": [],
            "list_topic_feedback": [], "build_topic_coverage_memory": {}, "_written_topic_memory": {},
            "_topic_candidates_from_runs": [], "_canonicalize_topic_candidate_ids": [], "_topic_pillars_for_bootstrap": [],
            "_canonicalize_topic_pillars": [], "_island_graph_for_bootstrap": None, "_latest_baseline_snapshot": None,
            "_profile_checks": {"scaffold": {"generationReady": False}}, "_article_capabilities_for_context": UNREADY,
            "_article_setup_state_for_config": {"generationReady": False}, "_guided_steps": ([], None),
            "_latest_run_matching": None, "google_baseline_connection_status": {}, "_google_baseline_connect_url": "",
            "_has_completed_article_flow": False, "_bootstrap_topic_picker_ready": False, "_serialize_startup_profile": {},
            "_serialize_baseline_snapshot": {}, "_pending_article_system_setup_from_config": None, "normalize_authors": [],
            "_recent_article_drafts": [], "_recent_written_topics": [], "_publish_evidence_from_run": {},
            "_workflow_progress": {}, "_article_repo_is_review_capable": False,
        }
        with ExitStack() as stack:
            for name, value in stubs.items():
                stack.enter_context(patch.object(views, name, Mock(return_value=value)))
            payload = views._compute_bootstrap_payload(context, view="summary")
        self.assertEqual(payload["settings"]["articleDeliveryMode"], "review_draft")
        self.assertEqual(payload["settings"]["articleDeliveryModeEffective"], "review_draft")
        self.assertFalse(payload["articleCapabilities"]["canGenerateArticle"])
        self.assertFalse(payload["articleSetupState"]["generationReady"])


class SettingsWriterTests(SimpleTestCase):
    """Rule 2: every settings writer stores review_draft for a connected website."""

    def test_vibe_marketing_settings_store_review_draft(self):
        cfg = config(mode="review_draft", brand_name="", company_context="", save=Mock())
        organization = Obj(name="Example", save=Mock())
        company = Obj(name="Example", domain="example.test", save=Mock(), id="company-1")
        request = Obj(user=Obj(is_staff=False, is_superuser=False),
                      data={"domain": "example.test", "articleDeliveryMode": "content_only"})
        with patch.object(views, "_resolve_profile_company_or_response", return_value=(Obj(), company, None)), \
             patch.object(views, "apply_company_domain_change"), \
             patch.object(views, "ensure_company_organization", return_value=organization), \
             patch.object(views, "_get_config", return_value=cfg), \
             patch.object(views, "_assign_config_actor", return_value=[]), \
             patch.object(views, "bind_website") as bind, \
             patch.object(views, "apply_shared_startup_details") as shared, \
             patch.object(views, "get_founder_company_context", return_value="context"):
            result = views.VibeMarketingSettingsView._save_settings.__wrapped__(views.VibeMarketingSettingsView(), request)
        self.assertEqual(result, "context")
        bind.assert_not_called()
        self.assertEqual(cfg.article_delivery_mode, "review_draft")
        self.assertIn("article_delivery_mode", cfg.save.call_args.kwargs["update_fields"])
        shared.assert_called_once()

    def test_vibe_marketing_settings_keep_content_only_without_a_website(self):
        cfg = config(state=None, mode="review_draft", brand_name="", company_context="", save=Mock())
        request = Obj(user=Obj(is_staff=False, is_superuser=False),
                      data={"domain": "example.test", "articleDeliveryMode": "content_only"})
        with patch.object(views, "_resolve_profile_company_or_response",
                          return_value=(Obj(), Obj(name="Example", domain="example.test", save=Mock(), id="c"), None)), \
             patch.object(views, "apply_company_domain_change"), \
             patch.object(views, "ensure_company_organization", return_value=Obj(name="Example", save=Mock())), \
             patch.object(views, "_get_config", return_value=cfg), \
             patch.object(views, "_assign_config_actor", return_value=[]), \
             patch.object(views, "apply_shared_startup_details"), \
             patch.object(views, "get_founder_company_context", return_value="context"):
            views.VibeMarketingSettingsView._save_settings.__wrapped__(views.VibeMarketingSettingsView(), request)
        self.assertEqual(cfg.article_delivery_mode, "content_only")

    def test_shared_startup_details_store_review_draft(self):
        from founder_tools import services
        organization = Obj(name="Example", domain="example.test", competitors=[], seed_keywords=[],
                           company_linkedin_url="", save=Mock())
        profile = Mock(company_aliases=["Example"], domain_aliases=["example.test"])
        # A legacy stored content_only is healed; a stored review_draft is not rewritten.
        for stored, saved in (("content_only", True), ("review_draft", False)):
            cfg = config(mode=stored, connected_slack_user_id="mlai_user:1", brand_name="", company_context="",
                         default_timezone="", daily_discovery_enabled=False, save=Mock())
            with self.subTest(stored=stored), \
                 patch("founder_tools.profile_fields.validate_profile_fields", side_effect=lambda data: data), \
                 patch("founder_tools.profile_fields.save_profile_details"), \
                 patch.object(services, "ensure_company_organization", return_value=organization), \
                 patch.object(services, "founder_actor_id_for_user", return_value="mlai_user:1"), \
                 patch.object(services, "actor_ids_for_user", return_value={"mlai_user:1"}), \
                 patch("content_factory.models.OrganizationContentConfig.objects.get_or_create", return_value=(cfg, False)), \
                 patch("startup_updates.services.resolve_or_create_profile", return_value=(None, profile)), \
                 patch("startup_updates.services.bind_user_to_startup"):
                services.apply_shared_startup_details.__wrapped__(user=Obj(), company=Obj(name="Example"),
                                                                  data={"articleDeliveryMode": "content_only"})
                self.assertEqual(cfg.article_delivery_mode, "review_draft")
                self.assertEqual(cfg.save.called, saved)
                if saved:
                    self.assertIn("article_delivery_mode", cfg.save.call_args.kwargs["update_fields"])

    def test_content_factory_app_settings_store_review_draft(self):
        from integrations import api_views_content_factory_app as app_views
        cfg = config(mode="content_only", default_timezone="UTC", brand_name="", company_context="", save=Mock())
        organization = Obj(domain="example.test")
        request = Obj(user=Obj(), data={"domain": "example.test", "article_delivery_mode": "content_only"})
        with patch.object(app_views, "_ensure_actor_id", return_value="mlai_user:1"), \
             patch.object(app_views, "actor_ids_for_user", return_value={"mlai_user:1"}), \
             patch.object(app_views, "_assert_domain_access", return_value=(None, "example.test")), \
             patch.object(app_views, "_get_config_for_domain", return_value=cfg), \
             patch.object(app_views.transaction, "atomic", return_value=nullcontext()), \
             patch.object(app_views.Organization.objects, "update_or_create", return_value=(organization, False)), \
             patch.object(app_views.OrganizationContentConfig.objects, "get_or_create", return_value=(cfg, False)), \
             patch.object(app_views, "_serialize_config", return_value={}), \
             patch.object(app_views, "_serialize_github_status", return_value={}):
            response = app_views.ContentFactoryAppSettingsView().put(request)
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(cfg.article_delivery_mode, "review_draft")
        cfg.save.assert_called_once()


class ArticleStartTests(SimpleTestCase):
    """Rules 4 and 5: an explicit content_only start never yields a portable draft."""

    def setUp(self):
        self.user = Obj(pk=1)
        self.context = Obj(organization=Obj(pk=7, domain="example.test"), company=Obj(pk="company-1"),
                           profile=Obj(user=self.user))
        self.config = config()
        # Exactly what older desktops post when their own website check is unverified.
        self.legacy_data = {"topic": "Delivery", "deliveryMode": "content_only", "deliveryModeExplicit": "true",
                            "website_connection_id": CONNECTION_ID, "connection_generation": "3"}
        self.start = views.start_article_generation

    def run_start(self, data, *, capabilities=READY, cfg=None, **kwargs):
        captured = {}

        def inner(**call):
            captured.update(call, owner_scope=owner_operation_contract())
            return Response({"runId": "run-1"}, status=202)

        self.guard_scopes = []

        @contextmanager
        def guard(scope, *, action):
            self.guard_scopes.append((scope, action))
            yield

        with patch.object(views, "_get_config", return_value=cfg or self.config), \
             patch.object(views, "_article_capabilities_for_context", return_value=capabilities) as self.capabilities, \
             patch.object(views, "_setup_blocked_response_for_generation", return_value=Response({"code": "setup"}, status=409)) as self.setup, \
             patch.object(views, "_charge_roo_points_for_article") as self.charge, \
             patch.object(views, "authority_guard", side_effect=guard), \
             patch.object(views, "start_article_generation", side_effect=inner) as self.inner:
            response = self.start(user=self.user, context=self.context, data=data, **kwargs)
        return response, captured

    def test_unready_website_refuses_a_portable_draft_before_billing(self):
        response, _ = self.run_start(self.legacy_data, capabilities=UNREADY)
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.data["code"], "website_preview_required")
        self.assertEqual(response.data["detail"], "Finish website setup to generate an article you can preview on your site.")
        self.assertEqual(response.data["reasonCode"], "integration_required")
        self.assertEqual(response.data["field"], "deliveryMode")
        self.capabilities.assert_called_once_with(self.context, self.config, force=True)
        self.inner.assert_not_called()
        self.setup.assert_not_called()
        self.charge.assert_not_called()

    def test_ready_website_runs_review_draft_under_the_reviewed_connection(self):
        response, call = self.run_start(self.legacy_data)
        self.assertEqual(response.status_code, 202)
        expected = {"website_connection_id": CONNECTION_ID, "connection_generation": 3}
        self.assertEqual(call["data"]["delivery_mode"], "review_draft")
        self.assertIs(call["data"]["delivery_mode_explicit"], True)
        self.assertNotIn("deliveryMode", call["data"])
        self.assertNotIn("deliveryModeExplicit", call["data"])
        self.assertEqual(call["data"]["topic"], "Delivery")
        self.assertEqual(call["website_contract"], expected)
        self.assertEqual(call["owner_scope"], {**expected, "domain": "example.test", "github_repo": "founder/site"})
        self.assertEqual(self.guard_scopes, [({**expected, "domain": "example.test", "github_repo": "founder/site"}, "read")])
        self.setup.assert_not_called()

    def test_ready_website_without_a_reviewed_connection_is_refused(self):
        data = {key: value for key, value in self.legacy_data.items() if key not in {"website_connection_id", "connection_generation"}}
        response, _ = self.run_start(data)
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.data["code"], "website_connection_required")
        self.inner.assert_not_called()

    def test_reviewed_callers_keep_their_contract_without_a_second_guard(self):
        contract = {"website_connection_id": CONNECTION_ID, "connection_generation": 3}
        _, call = self.run_start({"topic": "Delivery", "delivery_mode": "content_only", "delivery_mode_explicit": True},
                                 website_contract=contract)
        self.assertEqual(call["website_contract"], contract)
        self.assertEqual(call["data"]["delivery_mode"], "review_draft")
        self.assertEqual(self.guard_scopes, [])

    def test_company_without_a_connected_website_keeps_portable_drafts(self):
        for state in (None, "disconnected"):
            with self.subTest(state=state):
                response, _ = self.run_start(self.legacy_data, cfg=config(state=state))
                # The unchanged admission path ran (stubbed to stop before billing).
                self.assertEqual(response.data, {"code": "setup"})
                self.setup.assert_called_once()
                self.capabilities.assert_not_called()
                self.inner.assert_not_called()

    def test_implicit_or_preview_requests_are_unchanged(self):
        for data in ({"topic": "Delivery", "deliveryMode": "content_only"},
                     {"topic": "Delivery", "deliveryMode": "review_draft", "deliveryModeExplicit": True}):
            with self.subTest(data=data):
                response, _ = self.run_start(data)
                self.assertEqual(response.data, {"code": "setup"})
                self.capabilities.assert_not_called()
                self.inner.assert_not_called()


class MigrationTests(SimpleTestCase):
    """Rule 6: stored content_only becomes review_draft only for connected websites."""

    def test_rewrites_only_connected_content_only_configs(self):
        queryset = Mock()
        model = Obj(objects=Mock(filter=Mock(return_value=queryset)))
        apps = Mock(get_model=Mock(return_value=model))
        migration.preview_connected_website_articles(apps, schema_editor=None)
        apps.get_model.assert_called_once_with("content_factory", "OrganizationContentConfig")
        model.objects.filter.assert_called_once_with(
            article_delivery_mode="content_only", website_connection__state__in=("connected", "paused"))
        self.assertEqual(queryset.update.call_args.kwargs["article_delivery_mode"], "review_draft")
        self.assertEqual(set(migration.WEBSITE_PREVIEW_CONNECTION_STATES), set(portable_drafts.WEBSITE_PREVIEW_CONNECTION_STATES))

    def test_reverse_is_a_noop_and_follows_the_current_leaf(self):
        operation = migration.Migration.operations[0]
        self.assertIs(operation.reverse_code, operation.noop)
        self.assertEqual(migration.Migration.dependencies, [("content_factory", "0045_content_dispatch_outbox")])
