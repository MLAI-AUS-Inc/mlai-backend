"""Exercise the Chat facade at HTTP dispatch without database/network access."""
from types import SimpleNamespace as Obj
from unittest.mock import MagicMock, patch

from django.http import Http404
from django.test import SimpleTestCase
from django.urls import resolve, Resolver404
from rest_framework.response import Response
from rest_framework.test import APIRequestFactory, force_authenticate

from community_chat import my_startup_views as views
from content_factory.website_contract import WebsiteAuthorityError
from founder_tools.my_startup.api import MyStartupAuthentication
from community_chat.throttles import StartupScopedThrottle

COMPANY = "12345678-1234-1234-1234-123456789abc"
OTHER = "abcdefab-1234-1234-1234-123456789abc"


class MyStartupFacadeTests(SimpleTestCase):
    def setUp(self):
        self.factory = APIRequestFactory()
        self.user = Obj(pk=7, id=7, is_authenticated=True)
        self.company = Obj(pk=COMPANY, organization_id=2, organization=Obj(pk=2, domain="owned.test"))
        for target, value in (("get_object_or_404", self.company), ("user_may_use_organization", True)):
            patcher = patch.object(views, target, return_value=value)
            setattr(self, target, patcher.start())
            self.addCleanup(patcher.stop)
        patcher = patch.object(views.VibeRaisingCompany.objects, "select_related")
        patcher.start()
        self.addCleanup(patcher.stop)
        patcher = patch.object(StartupScopedThrottle, "allow_request", return_value=True)
        patcher.start()
        self.addCleanup(patcher.stop)

    def call(self, view, *, method="get", data=None, query=None, authenticated=True, **kwargs):
        url = "/?company_id=" + (query or COMPANY)
        request = getattr(self.factory, method)(url, data=data or {}, format="json")
        if authenticated:
            force_authenticate(request, user=self.user, token=Obj(pk="chat-session"))
        return view.as_view()(request, **kwargs)

    def test_authenticated_bootstrap_uses_explicit_company_and_private_cache(self):
        with patch.object(views.marketing.VibeMarketingBootstrapView, "get", return_value=Response({"ok": True})):
            response = self.call(views.BootstrapView)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Cache-Control"], "private, no-store")
        self.assertEqual(self.get_object_or_404.call_args.kwargs, {"pk": COMPANY, "profile__user": self.user})
        self.assertEqual(views.BootstrapView.authentication_classes, (MyStartupAuthentication,))

    def test_missing_auth_or_company_and_conflicting_body_scope_fail_closed(self):
        with patch.object(views.marketing.VibeMarketingSettingsView, "put") as save:
            self.assertEqual(self.call(views.SettingsView, method="put", authenticated=False).status_code, 401)
            self.assertEqual(self.call(views.SettingsView, method="put", data={"companyId": OTHER}).status_code, 400)
            request = self.factory.get("/")
            force_authenticate(request, self.user)
            self.assertEqual(views.BootstrapView.as_view()(request).status_code, 400)
        save.assert_not_called()

    def test_unowned_company_or_unowned_organization_never_reaches_mutation(self):
        with patch.object(views.founder.FounderToolsCompanyView, "post") as save:
            self.get_object_or_404.side_effect = Http404
            self.assertEqual(self.call(views.CompaniesView, method="post", data={"companyId": COMPANY, "name": "Changed"}).status_code, 404)
            self.get_object_or_404.side_effect = None
            self.user_may_use_organization.return_value = False
            self.assertEqual(self.call(views.CompaniesView, method="post", data={"name": "Changed"}).status_code, 404)
        save.assert_not_called()

    def test_creation_has_no_selected_company_and_requires_draft_research(self):
        with patch.object(views.marketing.VibeMarketingAutofillView, "post", return_value=Response({"runId": "r"})) as research:
            response = self.call(views.ResearchView, method="post", data={"createNew": True, "draftOnly": True})
            self.assertEqual(response.status_code, 400)
            self.assertEqual(self.call(views.ResearchView, method="post").status_code, 400)
            request = self.factory.post("/", {"createNew": True, "draftOnly": True}, format="json")
            force_authenticate(request, self.user)
            self.assertEqual(views.ResearchView.as_view()(request).status_code, 200)
            research.assert_called_once()

    def test_profile_and_preferences_cannot_bypass_repository_selection(self):
        with patch.object(views.founder.FounderToolsCompanyView, "post") as save:
            self.assertEqual(self.call(views.CompaniesView, method="post", data={"name": "Acme", "githubRepo": "x/y"}).status_code, 400)
            save.assert_not_called()
        with patch.object(views.marketing.VibeMarketingSettingsView, "put") as save:
            self.assertEqual(self.call(views.SettingsView, method="put", data={"githubRepo": "x/y"}).status_code, 400)
            save.assert_not_called()

    def test_cancel_only_delegates_cancellation_and_keeps_shared_scope_checks(self):
        with patch.object(views.marketing.VibeMarketingRunControlView, "post", return_value=Response({"status": "cancelling"})) as control:
            response = self.call(views.CancelRunView, method="post", run_id="run-1")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(control.call_args.args[1:], ("run-1", "cancel"))

    def test_github_reuses_account_and_replaces_raw_oauth_with_chat_handoff(self):
        with patch.object(views.marketing.VibeMarketingGitHubConnectView, "post", return_value=Response({"status": "auth_required", "auth_url": "raw-oauth"})), \
             patch("community_chat.startups.connections.ConnectView.post", return_value=Response({"authorizationUrl": "signed-handoff"})):
            response = self.call(views.GitHubConnectView, method="post")
        self.assertEqual(response.data["auth_url"], "signed-handoff")
        self.assertEqual(response.data["authorizationUrl"], "signed-handoff")

    def test_narrow_url_allowlist_excludes_admin_worker_and_arbitrary_actions(self):
        for suffix in ("vibe-marketing/company/avatar/", "vibe-marketing/github/repository/",
                       "founder-tools/profile/", "vibe-marketing/runs/run-1/cancel/"):
            match = resolve("/api/v1/my-startup/" + suffix)
            self.assertTrue(issubclass(match.func.view_class, views.MyStartupAccess))
        for suffix in ("points/admin/award/", "vibe-marketing/admin/usage/", "vibe-marketing/runs/run-1/arbitrary-action/"):
            with self.assertRaises(Resolver404):
                resolve("/" + suffix, urlconf="community_chat.my_startup_urls")

    def test_article_preferences_and_sources_routes_resolve_to_scoped_facades(self):
        routes = {
            "vibe-marketing/notifications/channels": views.NotificationChannelsView,
            "vibe-marketing/notifications/channels/delivery": views.NotificationDeliveryView,
            f"vibe-marketing/notifications/channels/{OTHER}": views.NotificationChannelView,
            f"vibe-marketing/notifications/channels/{OTHER}/verify": views.VerifyNotificationChannelView,
            f"vibe-marketing/notifications/channels/{OTHER}/resend": views.ResendNotificationChannelView,
            "vibe-marketing/notifications/automation": views.AutomationStatusView,
            "vibe-marketing/learned-rules": views.LearnedRulesView,
            "vibe-marketing/learned-rules/42": views.LearnedRuleView,
            "integrations/sources/status": views.SourcesStatusView,
        }
        for route, expected in routes.items():
            for slash in ("", "/"):
                with self.subTest(route=route, slash=slash):
                    actual = resolve("/api/v1/my-startup/" + route + slash).func.view_class
                    self.assertIs(actual, expected)
                    self.assertEqual(actual.authentication_classes, (MyStartupAuthentication,))
                    self.assertTrue(actual.requires_company)

    def test_website_lifecycle_routes_resolve_to_chat_company_facades(self):
        routes = {"vibe-marketing/website-connection": views.WebsiteConnectionView}
        routes.update({f"vibe-marketing/website-connection/{action}": views.WebsiteConnectionActionView
                       for action in ("pause", "disconnect", "reconnect", "reset", "cleanup")})
        for route, expected in routes.items():
            for slash in ("", "/"):
                with self.subTest(route=route, slash=slash):
                    actual = resolve("/api/v1/my-startup/" + route + slash).func.view_class
                    self.assertIs(actual, expected)
                    self.assertEqual(actual.authentication_classes, (MyStartupAuthentication,))
                    self.assertIn(StartupScopedThrottle, [type(item) for item in actual().get_throttles()])
                    self.assertTrue(actual.requires_company)
        self.assertIs(resolve("/api/v1/vibe-marketing/website-connection/reset").func.view_class,
                      views.websites.WebsiteConnectionActionView)

    def test_website_actions_require_auth_and_unambiguous_owned_company_before_transition(self):
        with patch.object(views.websites, "transition_connection") as transition:
            self.assertEqual(self.call(views.WebsiteConnectionActionView, method="post", action="reset", authenticated=False).status_code, 401)
            self.assertEqual(self.call(views.WebsiteConnectionActionView, method="post", action="reset", data={"companyId": OTHER}).status_code, 400)
            request = self.factory.post("/", {}, format="json")
            force_authenticate(request, self.user)
            self.assertEqual(views.WebsiteConnectionActionView.as_view()(request, action="reset").status_code, 400)
            self.get_object_or_404.side_effect = Http404
            self.assertEqual(self.call(views.WebsiteConnectionActionView, method="post", action="reset").status_code, 404)
            self.get_object_or_404.side_effect = None
            self.user_may_use_organization.return_value = False
            self.assertEqual(self.call(views.WebsiteConnectionActionView, method="post", action="disconnect").status_code, 404)
        transition.assert_not_called()

    def test_website_reset_dispatches_exact_reviewed_tuple_and_idempotency_key(self):
        binding = {"website_connection_id": OTHER, "connection_generation": 1, "repository_id": 123}
        operation = Obj(pk="reset-receipt", state="pending", receipt={"repository_modified": False,
                        "retained": ["company_details", "editorial_policy", "history"]})
        config = Obj(refresh_from_db=MagicMock())
        request = self.factory.post(f"/?company_id={COMPANY}", binding, format="json",
                                    HTTP_IDEMPOTENCY_KEY="reviewed-reset")
        force_authenticate(request, self.user)
        with patch.object(views.websites, "_context", return_value=(Obj(company=self.company), config, None)), \
             patch.object(views.websites, "transition_connection", return_value=operation) as transition, \
             patch.object(views.websites, "summary_for", return_value={"connectionGeneration": 2}) as summary:
            match = resolve("/api/v1/my-startup/vibe-marketing/website-connection/reset")
            response = match.func(request, **match.kwargs)
        self.assertEqual(response.status_code, 200)
        transition.assert_called_once_with(config, action="reset", expected=binding, idempotency_key="reviewed-reset")
        config.refresh_from_db.assert_called_once_with()
        summary.assert_called_once_with(config, company_id=COMPANY)
        self.assertEqual(response.data["websiteConnection"]["connectionGeneration"], 2)
        self.assertEqual(response.data["operation"]["receipt"], operation.receipt)
        self.assertEqual(response["Cache-Control"], "private, no-store")

    def test_website_reset_preserves_stale_tuple_conflict_without_remote_calls(self):
        binding = {"website_connection_id": OTHER, "connection_generation": 1}
        config = Obj(refresh_from_db=MagicMock())
        with patch.object(views.websites, "_context", return_value=(Obj(company=self.company), config, None)), \
             patch.object(views.websites, "transition_connection", side_effect=WebsiteAuthorityError(
                 "website_connection_changed", "Refresh and retry.")) as transition, \
             patch.object(views.websites, "bind_website") as bind, \
             patch("content_factory.website_reconciliation.approve_cleanup_proposal") as cleanup:
            response = self.call(views.WebsiteConnectionActionView, method="post", action="reset", data=binding)
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.data["code"], "website_connection_changed")
        self.assertEqual(transition.call_args.kwargs["expected"], binding)
        config.refresh_from_db.assert_not_called()
        bind.assert_not_called()
        cleanup.assert_not_called()

    def test_website_cleanup_still_requires_explicit_reviewed_approval(self):
        config = Obj(refresh_from_db=MagicMock())
        operation = Obj(pk="cleanup-receipt", state="pending", receipt={"requires_review": True})
        binding = {"website_connection_id": OTHER, "connection_generation": 2}
        reviewed = {**binding, "approve_cleanup": True, "operation_id": "proposal-1",
                    "source_sha": "a" * 40, "proposal_digest": "b" * 64}
        with patch.object(views.websites, "_context", return_value=(Obj(company=self.company), config, None)), \
             patch.object(views.websites, "summary_for", return_value={}), \
             patch.object(views.websites, "transition_connection", return_value=operation) as transition, \
             patch("content_factory.website_reconciliation.approve_cleanup_proposal", return_value=operation) as cleanup:
            self.assertEqual(self.call(views.WebsiteConnectionActionView, method="post", action="cleanup", data=binding).status_code, 200)
            cleanup.assert_not_called()
            self.assertEqual(self.call(views.WebsiteConnectionActionView, method="post", action="cleanup", data=reviewed).status_code, 200)
        transition.assert_called_once()
        cleanup.assert_called_once_with(config, user=self.user, data=reviewed)

    def test_website_action_route_cannot_invoke_worker_authorization(self):
        with patch.object(views.websites, "_context") as context:
            response = self.call(views.WebsiteConnectionActionView, method="post", action="authorize")
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.data["code"], "invalid_connection_action")
        context.assert_not_called()

    def test_foreign_notification_channel_cannot_be_verified_removed_or_toggled(self):
        context = Obj(organization=self.company.organization)
        with patch.object(views.notifications, "_resolve_context_or_response", return_value=(context, None)), \
             patch.object(views.notifications, "_channel_or_none", return_value=None) as channel, \
             patch.object(views.notifications, "verify_whatsapp_otp") as verify, \
             patch.object(views.notifications, "deactivate_channel") as remove, \
             patch.object(views.notifications, "send_whatsapp_otp") as send:
            for view, method in ((views.VerifyNotificationChannelView, "post"),
                                 (views.ResendNotificationChannelView, "post"),
                                 (views.NotificationChannelView, "delete"),
                                 (views.NotificationChannelView, "patch")):
                with self.subTest(view=view.__name__, method=method):
                    response = self.call(view, method=method, data={"code": "123456", "deliveryEnabled": True}, channel_id=OTHER)
                    self.assertEqual(response.status_code, 404)
                    channel.assert_called_with(self.company.organization, OTHER)
            verify.assert_not_called()
            remove.assert_not_called()
            send.assert_not_called()

    def test_notification_create_is_authenticated_and_scoped_before_provider_effects(self):
        with patch.object(views.notifications, "_resolve_context_or_response", return_value=(Obj(organization=self.company.organization), None)), \
             patch.object(views.notifications, "initiate_email_channel", return_value=Obj()) as create, \
             patch.object(views.notifications, "serialize_channel", return_value={"id": OTHER}), \
             patch.object(views.notifications, "_org_automation", return_value=None):
            self.assertEqual(self.call(views.NotificationChannelsView, method="post", data={"channelType": "email"}, authenticated=False).status_code, 401)
            create.assert_not_called()
            response = self.call(views.NotificationChannelsView, method="post", data={"channelType": "email"})
            self.assertEqual(response.status_code, 200)
            create.assert_called_once_with(organization=self.company.organization, user=self.user, route_id="")

    def test_learned_rule_retraction_is_selected_organization_scoped(self):
        with patch.object(views.marketing, "_resolve_context_or_response", return_value=(Obj(organization=self.company.organization), None)), \
             patch.object(views.marketing.ContentFactoryHealingRecord.objects, "filter") as records, \
             patch.object(views.marketing, "_request_editorial_learnings_fold") as fold:
            records.return_value.first.return_value = None
            response = self.call(views.LearnedRuleView, method="delete", rule_id=42)
        self.assertEqual(response.status_code, 404)
        records.assert_called_once_with(domain="owned.test", failure_kind="article_component_feedback", pk=42)
        fold.assert_not_called()

    def test_sources_alias_uses_same_selected_company_projection_as_connections(self):
        with patch("community_chat.startups.views.SourcesView.get", autospec=True,
                   return_value=Response({"sources": [{"provider": "github"}]})) as sources:
            response = self.call(views.SourcesStatusView)
        self.assertEqual(response.status_code, 200)
        self.assertIs(sources.call_args.args[0].company, self.company)
        self.assertEqual(response.data["sources"][0]["provider"], "github")

    def test_automation_write_cannot_bypass_settings_price_and_prerequisite_gate(self):
        with patch.object(views.notifications.VibeMarketingResearchAutomationView, "post") as write:
            response = self.call(views.AutomationStatusView, method="post", data={"enabled": True})
        self.assertEqual(response.status_code, 405)
        write.assert_not_called()


class GitHubSettingsReturnTests(SimpleTestCase):
    def test_return_uses_fixed_editor_path_and_owned_setup_context(self):
        from urllib.parse import parse_qs, urlencode, urlsplit
        from community_chat.startups.connections import github_settings_return
        setup = f"/my-startup/onboarding?step=repository&company_id={COMPANY}"
        result = github_settings_return("https://foreign.example/my-startup/connections/github?" + urlencode({"company_id": COMPANY, "return_to": setup}), COMPANY)
        self.assertEqual(urlsplit(result).path, "/my-startup/connections/github")
        self.assertEqual(parse_qs(urlsplit(result).query)["return_to"], [setup])

    def test_foreign_company_and_external_nested_return_are_discarded(self):
        from urllib.parse import parse_qs, urlencode, urlsplit
        from community_chat.startups.connections import github_settings_return
        self.assertIsNone(github_settings_return(f"/my-startup/connections/github?company_id={OTHER}", COMPANY))
        result = github_settings_return("/my-startup/connections/github?" + urlencode({"company_id": COMPANY, "return_to": "https://other.example/my-startup/onboarding"}), COMPANY)
        self.assertNotIn("return_to", parse_qs(urlsplit(result).query))
