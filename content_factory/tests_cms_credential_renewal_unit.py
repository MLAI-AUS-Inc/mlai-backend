"""Original native payment, source and provider journal survive credential renewal."""

from copy import deepcopy
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import Mock, patch
from uuid import uuid4

from django.test import SimpleTestCase, override_settings
from founder_tools.models import VibeRaisingCompany

from . import cms_owner_connection as owner
from . import cms_credential_renewal as renewal
from . import tests_cms_goal_controller_unit as fixtures
from .channel_artifacts import authorize_payload
from .cms_connection_authority import current_credential
from .cms_connection_contract import open_credential, seal_credential
from .website_contract import WebsiteAuthorityError
from .website_models import WebsiteConnection, WebsiteConnectionOperation


@override_settings(**fixtures.CmsGoalControllerTests._overridden_settings)
class CmsCredentialRenewalTests(SimpleTestCase):
    configure = fixtures.CmsGoalControllerTests.configure
    reserve = fixtures.CmsGoalControllerTests.reserve
    hold = fixtures.CmsGoalControllerTests.hold
    prepare_generation = fixtures.CmsGoalControllerTests.prepare_generation
    consume = fixtures.CmsGoalControllerTests.consume
    paid_run = fixtures.CmsGoalControllerTests.paid_run

    def setUp(self):
        fixtures.CmsGoalControllerTests.setUp(self)
        self.goal_operation.next_attempt_at = self.now + timedelta(seconds=180)
        self.paid_run()
        self.identity = {"schema_version": 1, "platform": "wordpress", "principal_id": str(self.policy["policy"]["native_template"]["author"]),
            "can_edit_posts": True, "can_publish_posts": True, "can_upload_media": True}
        self.old_request = str(uuid4())
        self.connection.capabilities.update(_cms_identity=deepcopy(self.identity), _cms_connect_request_id=self.old_request)
        self.connection.refresh_from_db = Mock()
        self.private.save = Mock(); self.private.refresh_from_db = Mock()
        self.saved.append(self.private)
        old_ack = WebsiteConnectionOperation(connection=self.connection, generation=self.connection.generation, action="cms-connect", state="completed",
            idempotency_key=f"cms-connect:{self.org.pk}:{self.company.pk}:{self.old_request}", payload={}, receipt={})
        self.saved.append(old_ack)
        self.data = {"client_request_id": str(uuid4()), "channel_scope": owner.scope_for(self.context, self.config),
            "channel_revision": self.config.article_system["publishing_channel"]["revision"], "platform": "wordpress",
            "public_origin": self.binding["public_origin"], "api_origin": self.binding["api_origin"],
            "credential_origin_confirmed": True, "username": "renewed-owner", "secret": "synthetic-replacement-application-password"}
        self.http = self.stack.enter_context(patch.object(owner, "inspect_identity", return_value=deepcopy(self.identity)))
        self.stack.enter_context(patch.object(VibeRaisingCompany.objects, "select_for_update", return_value=SimpleNamespace(get=lambda **_kw: self.company)))
        self.new_connection = self.stack.enter_context(patch.object(WebsiteConnection.objects, "create"))
        self.disconnect = self.stack.enter_context(patch("content_factory.website_disconnect.start_disconnect"))
        self.invalidate = self.stack.enter_context(patch("content_factory.website_connections.invalidate_repository_config"))
        def create(**values):
            row = WebsiteConnectionOperation(**values)
            row.save = Mock(); row.refresh_from_db = Mock()
            self.saved.append(row)
            return row
        self.create = self.stack.enter_context(patch.object(WebsiteConnectionOperation.objects, "create", side_effect=create))
        self.stack.enter_context(patch.object(WebsiteConnectionOperation.objects, "select_related", return_value=self.manager))

    def connect(self, **changes):
        return owner.connect(self.context, self.config, user=self.user, data={**self.data, **changes})

    def test_same_native_principal_renews_original_key_without_replacing_paid_source_or_choices(self):
        original = deepcopy((self.config.article_system, self.goal_operation.payload, self.goal_operation.receipt,
            self.run.run_request, self.outbox.payload, self.connection.configuration_version))
        credential_id, generation = self.connection.capabilities["_cms_credential_operation_id"], self.connection.generation
        encrypted = deepcopy(self.private.payload)
        self.assertIs(self.connect(), self.connection)
        self.assertEqual(self.connection.capabilities["_cms_credential_operation_id"], credential_id)
        self.assertEqual(self.connection.generation, generation)
        self.assertNotEqual(self.private.payload, encrypted)
        self.assertEqual(current_credential(self.context, self.config, self.connection, user=self.user)[1].secret, self.data["secret"])
        self.assertEqual((self.config.article_system, self.goal_operation.payload, self.goal_operation.receipt,
            self.run.run_request, self.outbox.payload, self.connection.configuration_version), original)
        self.assertEqual(str(authorize_payload(self.run.run_request).operation.pk),
            self.run.run_request["publishing_channel_artifact"]["artifact_operation_id"])
        self.new_connection.assert_not_called(); self.disconnect.assert_not_called(); self.invalidate.assert_not_called()
        self.http.assert_called_once(); self.spend.assert_called_once()
        self.assertNotIn(self.data["secret"], str(self.create.call_args.kwargs))

    def test_current_retry_reuses_actual_renewal_but_superseded_nonce_cannot_roll_back_the_key(self):
        original_key = current_credential(self.context, self.config, self.connection, user=self.user)[1]
        self.connect(); encrypted = deepcopy(self.private.payload)
        self.assertIs(self.connect(), self.connection)
        with self.assertRaises(WebsiteAuthorityError): self.connect(secret="synthetic-conflicting-key")
        with self.assertRaises(WebsiteAuthorityError):
            self.connect(client_request_id=self.old_request, username=original_key.username, secret=original_key.secret)
        self.assertEqual(self.private.payload, encrypted)
        self.http.assert_called_once(); self.create.assert_called_once(); self.spend.assert_called_once()

    def test_different_principal_or_reduced_publishing_permission_cannot_replace_original_article(self):
        original = deepcopy(self.private.payload)
        for identity in ({**self.identity, "principal_id": "999"}, {**self.identity, "can_publish_posts": False},
                {**self.identity, "can_publish_posts": 1}, {**self.identity, "schema_version": True}):
            self.http.return_value = identity
            with self.subTest(identity=identity), self.assertRaises(WebsiteAuthorityError): self.connect()
            self.assertEqual(self.private.payload, original)
        self.new_connection.assert_not_called(); self.disconnect.assert_not_called(); self.create.assert_not_called()

    def test_owner_or_newer_credential_during_probe_fences_the_late_result(self):
        original = deepcopy(self.private.payload)
        def revoked(*_args):
            self.user.is_active = False
            return self.identity
        self.http.side_effect = revoked
        with self.assertRaises(WebsiteAuthorityError): self.connect()
        self.assertEqual(self.private.payload, original)
        self.user.is_active = True
        newer = seal_credential(self.binding, username="latest-owner", secret="synthetic-later-key")
        def rotated(*_args):
            self.private.payload["sealed_credential"] = newer
            return self.identity
        self.http.side_effect = rotated
        with self.assertRaises(WebsiteAuthorityError): self.connect()
        self.assertEqual(self.private.payload["sealed_credential"], newer)
        self.create.assert_not_called(); self.disconnect.assert_not_called()

    def test_issued_original_post_stays_single_and_cannot_move_to_another_installation(self):
        post = WebsiteConnectionOperation(connection=self.connection, generation=self.connection.generation, action="cms-publish", state="pending",
            payload={"post_state": {"phase": "create_issued", "create_count": 1, "provider_id": None}}, receipt={"outcome_unknown": True})
        self.saved.append(post)
        original = deepcopy((post.payload, post.receipt))
        self.connect()
        self.assertEqual((post.payload, post.receipt), original)
        self.http.reset_mock()
        with self.assertRaises(WebsiteAuthorityError): self.connect(client_request_id=str(uuid4()), api_origin=self.binding["api_origin"] + "another/")
        self.http.assert_not_called(); self.new_connection.assert_not_called(); self.disconnect.assert_not_called()

    def test_deliberately_disconnected_or_deleted_private_key_is_not_revived(self):
        self.connection.state = "disconnected"
        with self.assertRaises(WebsiteAuthorityError): self.connect()
        self.connection.state = "connected"; self.private.state = "deleted"
        with self.assertRaises(WebsiteAuthorityError): self.connect()
        self.http.assert_not_called(); self.new_connection.assert_not_called(); self.create.assert_not_called()

    def test_private_renewal_selector_and_provider_identity_are_closed_and_platform_bound(self):
        with self.assertRaises(WebsiteAuthorityError):
            renewal.renew({}, self.context, self.config, user=self.user, proposal=self.binding, credential=None,
                identity=self.identity, request_id=self.data["client_request_id"], operation_key="untrusted")
        ghost = {**self.identity, "platform": "ghost", "principal_id": "a" * 24}
        self.assertEqual(renewal._identity(ghost, platform="ghost", publishing=True), "a" * 24)
        for changed in ({**ghost, "principal_id": "A" * 24}, {**ghost, "extra": True}, {**ghost, "principal_id": True}):
            with self.assertRaises(WebsiteAuthorityError): renewal._identity(changed, platform="ghost", publishing=True)
