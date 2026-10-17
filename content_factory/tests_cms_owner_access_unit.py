"""Authenticated access form metadata is scoped and never exports a credential."""

from copy import deepcopy
import json
from types import SimpleNamespace
from unittest.mock import patch
from uuid import uuid4

from django.test import SimpleTestCase, override_settings

from . import cms_owner_access as access
from . import tests_cms_credential_renewal_unit as fixtures
from . import tests_cms_setup_controller_unit as setup_fixtures
from .website_contract import WebsiteAuthorityError
from .website_views import WebsiteConnectionActionView
from .website_models import WebsiteConnectionOperation


@override_settings(**fixtures.CmsCredentialRenewalTests._overridden_settings)
class CmsOwnerAccessTests(SimpleTestCase):
    configure = fixtures.CmsCredentialRenewalTests.configure
    reserve = fixtures.CmsCredentialRenewalTests.reserve
    hold = fixtures.CmsCredentialRenewalTests.hold
    prepare_generation = fixtures.CmsCredentialRenewalTests.prepare_generation
    consume = fixtures.CmsCredentialRenewalTests.consume
    paid_run = fixtures.CmsCredentialRenewalTests.paid_run
    connect = fixtures.CmsCredentialRenewalTests.connect

    def setUp(self):
        fixtures.CmsCredentialRenewalTests.setUp(self)
        self.goal_operation.state = "needs_user"
        self.goal_operation.receipt["user_action"] = {"id": "connect_cms"}

    def read(self, identifier=None):
        return access.for_operation(self.context, self.config, user=self.user, operation_id=identifier or str(self.goal_operation.pk))

    def test_actual_access_wait_exposes_only_the_current_destination_and_owned_nonce(self):
        original = deepcopy((self.goal_operation.payload, self.goal_operation.receipt, self.private.payload, self.config.article_system))
        value = self.read()
        form = value["cmsAccess"]
        self.assertEqual((form["companyId"], form["connectionId"], form["connectionGeneration"], form["operationId"]),
            (self.binding["company_id"], self.binding["connection_id"], self.binding["generation"], str(self.goal_operation.pk)))
        self.assertEqual((form["publicOrigin"], form["apiOrigin"]), (self.binding["public_origin"], self.binding["api_origin"]))
        self.assertEqual(form["channelScope"], self.data["channel_scope"])
        self.assertEqual(form["channelRevision"], self.data["channel_revision"])
        self.assertEqual(value["cmsConnectionReceipt"]["requestId"], self.old_request)
        rendered = json.dumps(value)
        for private in ("synthetic-application-password", "sealed_credential", "credential_digest", "principal_id", "spend_ledger_id", "consent"):
            self.assertNotIn(private, rendered)
        self.assertEqual((self.goal_operation.payload, self.goal_operation.receipt, self.private.payload, self.config.article_system), original)
        self.http.assert_not_called(); self.create.assert_not_called(); self.spend.assert_called_once()

    def test_manual_review_working_and_cancelled_states_cannot_offer_credential_input(self):
        for state, choice in (("needs_user", "review_draft"), ("pending", "connect_cms"), ("cancelled", "connect_cms")):
            self.goal_operation.state = state
            self.goal_operation.receipt["user_action"] = {"id": choice}
            with self.subTest(state=state, choice=choice): self.assertIsNone(self.read()["cmsAccess"])
        self.http.assert_not_called()

    def test_foreign_missing_malformed_and_wrong_company_original_operations_are_refused(self):
        for identifier in (str(uuid4()), "invalid", True):
            with self.subTest(identifier=identifier), self.assertRaises(WebsiteAuthorityError): self.read(identifier)
        self.goal_operation.payload["company_id"] = str(uuid4())
        with self.assertRaises(WebsiteAuthorityError): self.read()
        self.http.assert_not_called()

    def test_actual_accepted_replacement_nonce_is_readable_without_repeating_provider_work(self):
        self.connect()
        self.http.reset_mock()
        receipt = access.connection_receipt(self.context, self.config, user=self.user)
        self.assertEqual(receipt["requestId"], self.data["client_request_id"])
        self.assertEqual(self.read()["cmsConnectionReceipt"], receipt)
        self.http.assert_not_called(); self.create.assert_called_once(); self.spend.assert_called_once()

    def test_revoked_or_concurrently_changed_owner_cannot_receive_old_form_metadata(self):
        self.user.is_active = False
        with self.assertRaises(WebsiteAuthorityError): self.read()
        self.user.is_active = True
        self.goal_operation.refresh_from_db.side_effect = lambda: setattr(self.user, "is_active", False)
        with self.assertRaises(WebsiteAuthorityError): self.read()
        self.user.is_active = True
        self.goal_operation.refresh_from_db.side_effect = WebsiteConnectionOperation.DoesNotExist()
        with self.assertRaises(WebsiteAuthorityError): self.read()
        self.http.assert_not_called()

    def test_changed_selection_credential_or_non_uuid_nonce_is_not_an_acknowledgement(self):
        self.connection.capabilities["_cms_connect_request_id"] = "untrusted-current-value"
        with self.assertRaises(WebsiteAuthorityError): self.read()
        self.connection.capabilities["_cms_connect_request_id"] = self.old_request
        self.config.article_system["publishing_channel"]["scope"]["generation"] += 1
        with self.assertRaises(WebsiteAuthorityError): self.read()
        self.http.assert_not_called()

    def test_owner_get_route_is_no_store_and_unknown_actions_do_not_read_private_rows(self):
        request = SimpleNamespace(user=self.user, query_params={"operation_id": str(self.goal_operation.pk)})
        with patch("content_factory.website_views._context", return_value=(self.context, self.config, None)), \
                patch("content_factory.cms_goal_status.for_context", return_value=None):
            response = WebsiteConnectionActionView().get(request, "cms-access")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Cache-Control"], "private, no-store")
        self.assertEqual(response.data, self.read())
        with patch.object(access, "for_operation") as private:
            self.assertEqual(WebsiteConnectionActionView().get(request, "disconnect").status_code, 404)
        private.assert_not_called(); self.http.assert_not_called()

    def test_owner_change_during_status_read_cannot_return_previously_built_form(self):
        request = SimpleNamespace(user=self.user, query_params={"operation_id": str(self.goal_operation.pk)})
        def revoked(*_args, **_kwargs):
            self.user.is_active = False
            return None
        with patch("content_factory.website_views._context", return_value=(self.context, self.config, None)), \
                patch("content_factory.cms_goal_status.for_context", side_effect=revoked):
            response = WebsiteConnectionActionView().get(request, "cms-access")
        self.assertNotEqual(response.status_code, 200)
        self.assertNotIn("cmsAccess", response.data)
        self.assertNotIn(self.binding["api_origin"], json.dumps(response.data))


@override_settings(**setup_fixtures.CmsSetupControllerTests._overridden_settings)
class CmsPreparatoryAccessTests(SimpleTestCase):
    configure = setup_fixtures.CmsSetupControllerTests.configure
    reserve = setup_fixtures.CmsSetupControllerTests.reserve
    accept = setup_fixtures.CmsSetupControllerTests.accept

    def setUp(self):
        setup_fixtures.CmsSetupControllerTests.setUp(self)
        self.connection.capabilities["_cms_identity"] = {"schema_version": 1, "platform": "wordpress", "principal_id": "7",
            "can_edit_posts": True, "can_publish_posts": True, "can_upload_media": True}
        self.accept()
        self.parent.state = "needs_user"
        self.parent.receipt["user_action"] = {"id": "connect_cms"}

    def test_pre_policy_access_form_uses_the_original_setup_without_inventing_native_goal_or_policy(self):
        before = deepcopy((self.parent.payload, self.parent.receipt))
        result = access.for_operation(self.context, self.config, user=self.user, operation_id=str(self.parent.pk))
        self.assertEqual(result["cmsAccess"]["operationId"], str(self.parent.pk))
        self.assertEqual(result["cmsAccess"]["apiOrigin"], self.binding["api_origin"])
        self.assertEqual((self.parent.payload, self.parent.receipt), before)
        self.assertEqual(self.parent.action, "cms-setup")
        self.assertNotIn("goal", self.parent.receipt)
        self.http.assert_not_called(); self.spend.assert_called_once()

    def test_paused_setup_rollout_does_not_offer_a_new_credential_action(self):
        with override_settings(ARTICLES_CMS_SETUP_ENABLED=False), self.assertRaises(WebsiteAuthorityError):
            access.for_operation(self.context, self.config, user=self.user, operation_id=str(self.parent.pk))
        self.http.assert_not_called()
