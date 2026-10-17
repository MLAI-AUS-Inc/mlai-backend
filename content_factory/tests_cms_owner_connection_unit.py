"""CMS consent races, destination proof and private persistence without a DB."""

from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import Mock, patch
from uuid import uuid4

from django.contrib.auth import get_user_model
from django.test import SimpleTestCase, override_settings

from . import cms_owner_connection as owner
from . import cms_provider_identity as provider
from .cms_api_transport import CmsApiError
from .cms_connection_contract import CmsCredential, open_credential
from .website_contract import WebsiteAuthorityError
from .website_models import WebsiteConnection, WebsiteConnectionOperation
from .website_views import WebsiteConnectionActionView
from . import tests_publishing_channels_unit as choices
from . import tests_cms_connection_contract_unit as keys


class CmsIdentityTests(SimpleTestCase):
    def setUp(self):
        self.binding = {"platform": "wordpress", "public_origin": "https://fixture.test/",
            "api_origin": "https://fixture.test/wordpress/"}
        self.credential = CmsCredential(platform="wordpress", username="owner", secret="synthetic-application-password")
        self.site = {"home": self.binding["public_origin"], "url": self.binding["api_origin"],
            "description": "Provider text must not be stored"}
        self.user = {"id": 7, "capabilities": {"edit_posts": True, "publish_posts": True, "upload_files": True},
            "email": "private@example.test", "name": "Private Name"}

    def test_wordpress_public_home_and_install_then_authenticated_permissions_are_minimized(self):
        with patch.object(provider, "request_json", side_effect=[(self.site, {}), (self.user, {})]) as http:
            identity = provider.inspect_identity(self.binding, self.credential)
        self.assertEqual(http.call_args_list[0].args[1], None)
        self.assertIs(http.call_args_list[1].args[1], self.credential)
        self.assertEqual(identity, {"schema_version": 1, "platform": "wordpress", "principal_id": "7",
            "can_edit_posts": True, "can_publish_posts": True, "can_upload_media": True})
        self.assertNotIn("private", str(identity).lower())
        self.assertEqual(http.call_args_list[1].kwargs, {"method": "GET", "resource": "users", "identifier": "me", "query": {"context": "edit"}})

    def test_foreign_site_or_other_install_stops_before_credential_request(self):
        for site in ({**self.site, "home": "https://other.test/"}, {**self.site, "url": "https://fixture.test/other/"},
                     {"home": "https://user:password@fixture.test/"}, [], {}):
            with self.subTest(site=site), patch.object(provider, "request_json", return_value=(site, {})) as http:
                with self.assertRaises(CmsApiError):
                    provider.inspect_identity(self.binding, self.credential)
                self.assertEqual(http.call_count, 1)

    def test_missing_edit_permission_invalid_ids_and_string_booleans_are_rejected(self):
        for user in ({**self.user, "id": True}, {**self.user, "id": "7"}, [],
                     {**self.user, "capabilities": {}}, {**self.user, "capabilities": {"edit_posts": "true"}}):
            with self.subTest(user=user), patch.object(provider, "request_json", side_effect=[(self.site, {}), (user, {})]), self.assertRaises(CmsApiError):
                provider.inspect_identity(self.binding, self.credential)
        user = {**self.user, "capabilities": {"edit_posts": True, "publish_posts": "true"}}
        with patch.object(provider, "request_json", side_effect=[(self.site, {}), (user, {})]):
            identity = provider.inspect_identity(self.binding, self.credential)
        self.assertFalse(identity["can_publish_posts"])
        self.assertFalse(identity["can_upload_media"])

    def test_ghost_public_site_is_not_authentication_and_separate_admin_origin_is_explicit(self):
        binding = {**self.binding, "platform": "ghost", "api_origin": "https://admin.fixture.test/"}
        credential = CmsCredential(platform="ghost", secret="a" * 24 + ":" + "b" * 64)
        with patch.object(provider, "request_json", side_effect=[({"site": {"url": self.binding["public_origin"]}}, {}), ({"posts": []}, {})]) as http:
            identity = provider.inspect_identity(binding, credential)
        self.assertEqual(identity["principal_id"], "a" * 24)
        self.assertIsNone(http.call_args_list[0].args[1])
        self.assertIs(http.call_args_list[1].args[1], credential)
        with patch.object(provider, "request_json", side_effect=[({"site": {"url": self.binding["public_origin"]}}, {}), CmsApiError("credentials_required")]), self.assertRaises(CmsApiError):
            provider.inspect_identity(binding, credential)
        with patch.object(provider, "request_json", return_value=({"site": {"url": "https://other.test/"}}, {})) as http, self.assertRaises(CmsApiError):
            provider.inspect_identity(binding, credential)
        self.assertEqual(http.call_count, 1)


@override_settings(ARTICLES_CHANNEL_DISCOVERY_ENABLED=True, ARTICLES_PUBLISHING_CHANNELS_ENABLED=True,
    ARTICLES_CMS_CHANNEL_ENABLED=True, CONNECTOR_CREDENTIAL_KEYS=keys.json.dumps({"test": keys.TEST_KEY}),
    CONNECTOR_CREDENTIAL_ACTIVE_KEY_ID="test")
class CmsOwnerConnectionTests(SimpleTestCase):
    update = choices.PublishingChannelChoiceTests.update

    def setUp(self):
        # Avoid capturing a patched transaction.atomic in lazy billing decorators.
        from roo import services
        choices.PublishingChannelChoiceTests.setUp(self)
        self.user = get_user_model()(pk=4, is_active=True)
        self.user.refresh_from_db = Mock()
        self.profile.user_id = 4
        self.company.refresh_from_db = Mock()
        self.update()
        self.update(selected="cms")
        self.data = {"client_request_id": str(uuid4()), "channel_scope": deepcopy(owner.scope_for(self.context, self.config)),
            "channel_revision": 2, "platform": "ghost", "credential_origin_confirmed": True,
            "public_origin": "https://fixture.test/", "api_origin": "https://admin.fixture.test/",
            "secret": "a" * 24 + ":" + "b" * 64}
        self.identity = {"schema_version": 1, "platform": "ghost", "principal_id": "a" * 24,
            "can_edit_posts": True, "can_publish_posts": True, "can_upload_media": True}
        self.created = []
        def create_connection(**kwargs):
            value = WebsiteConnection(**kwargs)
            value.save = Mock()
            self.created.append(value)
            return value
        def create_operation(**kwargs):
            value = WebsiteConnectionOperation(**kwargs)
            value.save = Mock()
            self.created.append(value)
            return value
        self.extra = [patch.object(owner, "inspect_identity", return_value=self.identity),
            patch.object(WebsiteConnection.objects, "create", side_effect=create_connection),
            patch.object(WebsiteConnectionOperation.objects, "create", side_effect=create_operation),
            patch.object(WebsiteConnectionOperation.objects, "filter"),
            patch("content_factory.website_connections.invalidate_repository_config"),
            patch("content_factory.website_connections.require_unlocked_remote_call")]
        self.http, self.connections, self.operations, self.rows, self.invalidate, self.unlocked = [p.start() for p in self.extra]
        self.rows.return_value.first.return_value = None
        for p in self.extra:
            self.addCleanup(p.stop)

    def connect(self, data=None):
        return owner.connect(self.context, self.config, user=self.user, data=self.data if data is None else data)

    def test_connect_has_no_github_identity_and_credential_only_in_private_encrypted_row(self):
        before = deepcopy(self.data)
        connection = self.connect()
        self.assertEqual(self.data, before)
        self.assertIs(self.config.website_connection, connection)
        self.assertEqual((connection.github_repo, connection.branch, connection.app_root, connection.installation_id, connection.repository_id), ("", "", "", "", None))
        self.assertFalse(connection.capabilities.get("publishingReady"))
        private = next(row for row in self.created if isinstance(row, WebsiteConnectionOperation) and row.action == "channel-credential")
        public = next(row for row in self.created if isinstance(row, WebsiteConnectionOperation) and row.action == "cms-connect")
        self.assertNotIn(self.data["secret"], str(private.payload))
        self.assertNotIn("sealed_credential", str(public.receipt))
        self.assertNotIn(self.data["secret"], str(connection.capabilities))
        self.assertEqual(open_credential(private.payload["sealed_credential"], binding=connection.capabilities["_cms_binding"]).secret, self.data["secret"])
        choice = self.config.article_system["publishing_channel"]
        self.assertEqual(choice["scope"], owner.scope_for(self.context, self.config))
        self.assertEqual(choice["revision"], 3)
        self.assertEqual(choice["selection"]["channel"], "cms")
        self.unlocked.assert_called_once()

    def test_disabled_wrong_owner_stale_choice_or_destination_confusion_never_sends_key(self):
        for changes in ({"channel_revision": 1}, {"channel_revision": True}, {"channel_scope": {}},
                        {"credential_origin_confirmed": "true"}, {"public_origin": "https://other.test/"},
                        {"api_origin": "https://user:secret@fixture.test/"}, {"platform": "wordpress"},
                        {"client_request_id": "not-a-uuid"}):
            with self.subTest(changes=changes), self.assertRaises(WebsiteAuthorityError):
                self.connect({**self.data, **changes})
        self.user.is_active = False
        with self.assertRaises(WebsiteAuthorityError): self.connect()
        self.user.is_active = True
        self.profile.user_id = 99
        with self.assertRaises(WebsiteAuthorityError): self.connect()
        self.profile.user_id = 4
        with override_settings(ARTICLES_CMS_CHANNEL_ENABLED=False), self.assertRaises(WebsiteAuthorityError): self.connect()
        self.http.assert_not_called()
        self.operations.assert_not_called()

    def test_owner_transfer_site_revision_and_disconnect_during_probe_cannot_persist(self):
        for target, name, value in ((self.profile, "user_id", 99), (self.company, "profile_id", 99),
                                    (self.company, "domain", "other.test"), (self.org, "domain", "other.test"),
                                    (self.user, "is_active", False)):
            old = getattr(target, name)
            def change(*args):
                setattr(target, name, value)
                return self.identity
            self.http.side_effect = change
            with self.subTest(name=name), self.assertRaises(WebsiteAuthorityError): self.connect()
            setattr(target, name, old)
        def revision_change(*args):
            self.config.article_system["publishing_channel"]["revision"] += 1
            return self.identity
        self.http.side_effect = revision_change
        with self.assertRaises(WebsiteAuthorityError): self.connect()
        self.config.article_system["publishing_channel"]["revision"] -= 1
        def connection_change(*args):
            self.config.website_connection = WebsiteConnection(organization=self.org, github_repo="", state="disconnected")
            return self.identity
        self.http.side_effect = connection_change
        with self.assertRaises(WebsiteAuthorityError): self.connect()
        self.connections.assert_not_called()
        self.operations.assert_not_called()

    def test_provider_failure_has_safe_diagnostic_and_no_local_binding(self):
        for category in ("credentials_required", "site_mismatch", "transient", "invalid_response"):
            self.http.side_effect = CmsApiError(category)
            with self.subTest(category=category), self.assertRaises(WebsiteAuthorityError) as error:
                self.connect()
            self.assertNotIn(self.data["secret"], str(error.exception))
            self.assertEqual(error.exception.retryable, category == "transient")
        self.connections.assert_not_called()
        self.operations.assert_not_called()

    def test_same_request_reuses_current_connection_without_http_and_changed_secret_conflicts(self):
        connection = self.connect()
        self.http.reset_mock()
        private = next(row for row in self.created if isinstance(row, WebsiteConnectionOperation) and row.action == "channel-credential")
        relation = Mock(); relation.filter.return_value.first.return_value = private
        with patch.object(WebsiteConnection, "operations", relation):
            self.assertIs(self.connect(), connection)
            with self.assertRaises(WebsiteAuthorityError) as error:
                self.connect({**self.data, "secret": "a" * 24 + ":" + "c" * 64})
            self.assertEqual(error.exception.code, "operation_key_conflict")
        self.http.assert_not_called()
        self.assertEqual(self.connections.call_count, 1)

    def test_request_from_a_superseded_connection_does_not_restart_provider_work(self):
        self.rows.return_value.first.return_value = SimpleNamespace(connection_id=uuid4())
        with self.assertRaises(WebsiteAuthorityError) as error:
            self.connect()
        self.assertEqual(error.exception.code, "operation_key_conflict")
        self.http.assert_not_called()

    def test_existing_repository_uses_composite_disconnect_before_new_binding(self):
        previous = WebsiteConnection(organization=self.org, github_repo="owner/site", repository_id=123)
        self.config.website_connection = previous
        # The owner chooses CMS for the currently selected repository scope.
        self.update(); self.update(selected="cms")
        self.data.update(channel_scope=owner.scope_for(self.context, self.config), channel_revision=4)
        with patch("content_factory.website_disconnect.start_disconnect") as disconnect:
            self.connect()
        self.assertIs(disconnect.call_args.args[0], self.config)
        self.assertEqual(disconnect.call_args.kwargs["data"]["website_connection_id"], str(previous.pk))
        self.assertIs(disconnect.call_args.kwargs["data"]["remove_setup"], False)

    def test_owner_action_uses_cms_controller_without_entering_git_bind_or_probe(self):
        request = SimpleNamespace(data=self.data, user=self.user)
        with patch("content_factory.website_views._context", return_value=(self.context, self.config, None)), \
                patch.object(owner, "connect") as connect, patch("content_factory.website_views.bind_website") as git, \
                patch("content_factory.cms_owner_access.connection_receipt", return_value={"requestId": self.data["client_request_id"]}) as receipt, \
                patch("content_factory.website_views.summary_for", return_value={"channel": "cms"}):
            response = WebsiteConnectionActionView().post(request, "cms-connect")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Cache-Control"], "private, no-store")
        self.assertEqual(response.data["cmsConnectionReceipt"], receipt.return_value)
        connect.assert_called_once()
        git.assert_not_called()

    def test_disconnect_is_available_with_rollouts_off_and_keeps_provider_content(self):
        from .publishing_channel_adapters import CmsChannel
        from .publishing_channel_contract import ChannelRequest
        self.config.website_connection = WebsiteConnection(organization=self.org, github_repo="",
            capabilities={"_cms_binding": {"platform": "ghost"}})
        operation = SimpleNamespace(receipt={}, save=Mock())
        request = ChannelRequest(context=self.context, config=self.config, user=self.user, data={"connection_generation": 1})
        with override_settings(ARTICLES_PUBLISHING_CHANNELS_ENABLED=False, ARTICLES_CMS_CHANNEL_ENABLED=False), \
                patch("content_factory.website_disconnect.start_disconnect", return_value=operation) as disconnect:
            self.assertIs(CmsChannel().disconnect(request), operation)
        self.assertIs(disconnect.call_args.kwargs["data"]["remove_setup"], False)
        self.assertIs(operation.receipt["credential_erased"], True)
        self.assertIs(operation.receipt["published_content_retained"], True)
        self.assertIn("Revoke", operation.receipt["credential_revocation_instruction"])
        with patch("content_factory.website_disconnect.start_disconnect") as disconnect, self.assertRaises(WebsiteAuthorityError):
            CmsChannel().disconnect(ChannelRequest(context=self.context, config=self.config, user=self.user, data={"remove_setup": True}))
        disconnect.assert_not_called()
