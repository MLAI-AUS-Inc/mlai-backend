"""Setup progress is current, read-only and distinct from actual native goal proofs."""

from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import patch
import json

from django.test import SimpleTestCase, override_settings

from . import cms_goal_status as status
from . import cms_setup_quote as quotes
from . import tests_cms_setup_controller_unit as fixtures
from .cms_api_transport import CmsApiError


@override_settings(**{**fixtures.CmsSetupControllerTests._overridden_settings, "ARTICLES_WEBSITE_DOCTOR_ENABLED": True})
class CmsSetupStatusTests(SimpleTestCase):
    configure = fixtures.CmsSetupControllerTests.configure
    reserve = fixtures.CmsSetupControllerTests.reserve
    accept = fixtures.CmsSetupControllerTests.accept
    advance = fixtures.CmsSetupControllerTests.advance

    def setUp(self): fixtures.CmsSetupControllerTests.setUp(self)

    def read(self): return status.for_context(self.context, self.config, user=self.user, now=self.now)

    def test_actual_accepted_setup_has_its_own_mode_identity_and_schedule_without_fake_goal_or_proofs(self):
        original = self.accept()
        value = self.read()
        self.assertEqual((value["channel"], value["mode"], value["state"]), ("cms", "setup", "working"))
        self.assertEqual(value["operationId"], str(original.pk))
        self.assertEqual(value["target"], "first_article_live")
        self.assertIsNone(value["goalId"])
        self.assertEqual(value["milestones"], [])
        self.assertEqual(value["evidence"], [])
        self.assertEqual(value["nextAttemptAt"], original.next_attempt_at.isoformat())
        self.assertIsNone(value["doctor"]["nativeSource"])
        self.assertIsNone(value["doctor"]["goalId"])
        self.assertEqual(value["doctor"]["checks"], [])
        self.assertNotIn("nativeSetup", value)

    def test_repeated_status_reads_do_not_schedule_discover_charge_or_change_original_evidence(self):
        self.accept()
        before = deepcopy([(row.action, row.payload, row.receipt, row.attempts, row.next_attempt_at) for row in self.saved])
        self.spend.reset_mock(); self.http.reset_mock(); self.price.reset_mock()
        self.assertEqual(self.read(), self.read())
        self.assertEqual([(row.action, row.payload, row.receipt, row.attempts, row.next_attempt_at) for row in self.saved], before)
        self.spend.assert_not_called(); self.http.assert_not_called(); self.price.assert_not_called()

    def test_actual_transient_retry_exports_only_bounded_diagnostics_and_current_retry_time(self):
        self.accept()
        self.http.side_effect = CmsApiError("transient")
        self.advance()
        value = self.read()
        self.assertEqual(value["state"], "working")
        self.assertIsNone(value["action"])
        self.assertEqual(value["nextAttemptAt"], self.parent.next_attempt_at.isoformat())
        self.assertEqual(value["doctor"]["failure"]["class"], "transient")
        self.assertEqual(value["doctor"]["budget"]["activeSeconds"], 180)
        self.assertEqual(value["doctor"]["budget"]["providerSeconds"], 180)

    def test_doctor_projects_only_original_registered_failure_causes(self):
        from .website_failure_contract import classify_failure
        self.accept()
        self.parent.receipt["failure"] = classify_failure("CONNECTION_SERVICE_TRANSIENT", "cms_setup_controller")
        self.parent.receipt["effective_failure_class"] = "repairable"
        value = self.read()["doctor"]["failure"]
        self.assertEqual((value["code"], value["class"], value["effectiveClass"]), ("CONNECTION_SERVICE_TRANSIENT", "transient", "repairable"))
        self.parent.receipt["failure"]["code"] = "unregistered_synthetic_private_diagnosis"
        value = self.read()["doctor"]["failure"]
        self.assertIsNone(value["code"])
        self.assertIsNone(value["class"])
        self.assertIsNone(value["effectiveClass"])
        self.assertNotIn("unregistered", json.dumps(value))

    def test_only_recorded_real_provider_access_failure_offers_owner_reconnect_and_never_article_review(self):
        self.accept()
        self.http.side_effect = CmsApiError("credentials_required")
        self.advance()
        value = self.read()
        self.assertEqual((value["state"], value["action"]["id"]), ("needs_you", "connect_cms"))
        self.assertEqual(value["action"]["operationId"], str(self.parent.pk))
        for key in ("runId", "reviewRevision", "stageId"):
            self.assertNotIn(key, value["action"])
        self.parent.receipt["user_action"] = {"id": "review_draft"}
        self.assertNotEqual(self.read()["state"], "needs_you")

    def test_stopped_or_copied_completion_is_never_a_live_goal_or_publication(self):
        self.accept()
        self.parent.state = "completed"
        self.parent.receipt.update(status="first_article_live", public_url="https://fixture.test/copied", goal={"complete": True})
        value = self.read()
        self.assertEqual(value["state"], "stopped")
        self.assertIsNone(value["goalId"])
        self.assertNotIn("publicUrl", value)
        self.assertEqual(value["milestones"], [])

    def test_original_private_origin_secret_money_consent_or_lease_never_enters_status(self):
        self.accept()
        rendered = json.dumps(self.read())
        for private in ("api_origin", "credential_operation_id", "intent_digest", "first_article_billing", "payer_user_id",
                "spend_ledger_id", "client_request_id", "billing_confirmed", "synthetic-application-password", "sealed_credential"):
            self.assertNotIn(private, rendered)

    def test_changed_owner_company_or_connection_does_not_expose_the_original_setup(self):
        self.accept()
        for target, key, changed in ((self.user, "is_active", False), (self.company, "profile_id", 999), (self.connection, "generation", 2)):
            original = getattr(target, key)
            setattr(target, key, changed)
            with self.subTest(key=key): self.assertIsNone(self.read())
            setattr(target, key, original)

    def test_owner_change_during_private_status_read_cannot_return_an_old_doctor_or_action(self):
        from . import cms_setup_authority
        from .website_contract import WebsiteAuthorityError
        self.accept()
        def changed(*args, **kwargs):
            self.user.is_active = False
            raise WebsiteAuthorityError("website_connection_changed", "Synthetic current owner changed.")
        with patch.object(cms_setup_authority, "current", side_effect=changed): self.assertIsNone(self.read())

    def test_disabled_setup_hides_new_quotes_but_keeps_actual_owned_paused_operation_visible(self):
        with override_settings(ARTICLES_CMS_SETUP_ENABLED=False):
            value = self.read()
            self.assertNotIn("nativeSetup", value)
            self.assertIsNone(value["action"])
        self.accept()
        with override_settings(ARTICLES_CMS_SETUP_ENABLED=False):
            value = self.read()
            self.assertEqual((value["mode"], value["state"]), ("setup", "stopped"))
            self.assertEqual(value["operationId"], str(self.parent.pk))
            self.assertNotEqual(value["action"]["id"], "cms_configure")

    def test_actual_policy_promotion_changes_mode_under_the_same_operation_id(self):
        original = self.accept()
        identifier = str(original.pk)
        self.advance()
        value = self.read()
        self.assertEqual((value["mode"], value["goalId"], value["operationId"]), ("goal", identifier, identifier))
        self.assertEqual(value["state"], "working")
        self.assertEqual(len(value["milestones"]), 9)
        self.assertNotIn("nativeSetup", value)

    def test_missing_private_intent_is_read_as_stopped_without_repairing_or_inventing_a_new_start(self):
        self.accept()
        child = next(row for row in self.saved if row.action == "cms-setup-intent")
        self.saved.remove(child)
        before = deepcopy(self.parent.receipt)
        value = self.read()
        self.assertEqual(value["state"], "stopped")
        self.assertEqual(self.parent.receipt, before)
        self.assertNotIn("nativeSetup", value)

    def test_any_original_setup_state_suppresses_another_quote_and_payment_offer(self):
        self.accept()
        for state in ("pending", "running", "needs_user", "cancelled", "stopped", "needs_attention"):
            self.parent.state = state
            with self.subTest(state=state):
                self.assertIsNone(quotes.for_context(self.context, self.config, user=self.user, expected_binding=self.binding))

    def test_owner_post_accepts_once_returns_only_current_safe_progress_and_no_provider_call(self):
        from . import website_views
        request = SimpleNamespace(user=self.user, data=self.data)
        with patch.object(website_views, "_context", return_value=(self.context, self.config, None)):
            first = website_views.WebsiteConnectionActionView().post(request, "cms-setup-articles")
            second = website_views.WebsiteConnectionActionView().post(request, "cms-setup-articles")
        self.assertEqual(first.status_code, 202)
        self.assertEqual(first.data["operationId"], second.data["operationId"])
        self.assertTrue(first.data["accepted"])
        self.assertEqual(first.data["websiteStatus"]["mode"], "setup")
        self.assertEqual(first["Cache-Control"], "private, no-store")
        self.spend.assert_called_once(); self.http.assert_not_called()
