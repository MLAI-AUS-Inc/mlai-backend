"""Original private setup leases fence every provider read and acknowledgement."""

from copy import deepcopy
from datetime import timedelta
from uuid import uuid4
from unittest.mock import patch

from django.test import SimpleTestCase, override_settings

from . import cms_setup_authority as authority
from . import cms_setup_contract as contracts
from . import cms_setup_quote as quotes
from . import cms_publication_policy as policies
from . import tests_cms_goal_billing_unit as fixtures
from .cms_api_transport import CmsApiError
from .website_contract import WebsiteAuthorityError
from .website_models import WebsiteConnectionOperation


@override_settings(**{**fixtures.CmsGoalBillingTests._overridden_settings,
    "ARTICLES_CMS_STATUS_ENABLED": True, "ARTICLES_CMS_SETUP_ENABLED": True})
class CmsSetupAuthorityTests(SimpleTestCase):
    configure = fixtures.CmsGoalBillingTests.configure
    reserve = fixtures.CmsGoalBillingTests.reserve

    def setUp(self):
        fixtures.CmsGoalBillingTests.setUp(self)
        self.stack.enter_context(patch.object(WebsiteConnectionOperation.objects, "select_related", return_value=self.manager))
        self.saved.remove(self.goal_operation)
        self.saved[:] = [row for row in self.saved if row.action != policies.ACTION]
        self.connection.capabilities.pop(policies.REFERENCE)
        self.connection.refresh_from_db = lambda: None
        self.quote = quotes.for_context(self.context, self.config, user=self.user, expected_binding=self.binding)
        self.identifier, self.request = str(uuid4()), str(uuid4())
        self.intent = contracts.make_intent(self.quote, operation_id=self.identifier, request_id=self.request,
            binding=self.binding, credential_operation_id=str(self.private.pk), user_id=self.user.pk,
            answers={"review_policy": "auto", "publication_confirmed": True, "native_defaults_confirmed": True,
                "billing_confirmed": True, "expected_cost_points": 6}, now=self.now)
        self.child, _ = self.manager.get_or_create(idempotency_key="cms-setup-intent:" + self.identifier, defaults={
            "connection": self.connection, "generation": self.binding["generation"], "action": authority.INTENT_ACTION,
            "state": "completed", "payload": deepcopy(self.intent)})
        self.lease = str(uuid4())
        self.parent, _ = self.manager.get_or_create(idempotency_key=f"cms-setup:{self.connection.pk}:{self.binding['generation']}:{self.request}", defaults={
            "id": self.identifier, "connection": self.connection, "generation": self.binding["generation"],
            "action": authority.ACTION, "state": "running", "next_attempt_at": self.now + timedelta(minutes=3),
            "payload": {"schema_version": 1, "binding": deepcopy(self.binding), "credential_operation_id": str(self.private.pk),
                "company_id": self.binding["company_id"], "requested_by_user_id": str(self.user.pk),
                "intent_operation_id": str(self.child.pk), "intent_digest": self.intent["digest"], "lease_id": self.lease},
            "receipt": {"status": "discovering"}})
        from .cms_goal_budget import initial_at, reserve_setup_usage
        self.parent.receipt["native_budget"] = reserve_setup_usage(initial_at(self.intent["accepted_at"]),
            started_at=self.intent["accepted_at"], operation_id=self.identifier, active_seconds=180, now=self.now)
        self.dispatch = authority.CmsSetupDispatch(self.identifier, self.lease)
        self.http.reset_mock()

    def value(self): return authority.current(self.parent, user=self.user, lease_id=self.lease)

    def discover(self, dispatch=None, data=None):
        return policies.configure(self.context, self.config, user=self.user,
            data=data if data is not None else authority.policy_data(self.value()),
            setup_scope=self.dispatch if dispatch is None else dispatch)

    def test_current_setup_resolves_its_actual_saved_site_and_original_accepted_choices(self):
        value = self.value()
        self.assertEqual(value.intent, self.intent)
        self.assertIs(value.context.company, self.company)
        self.assertIs(value.user, self.user)
        self.assertIsNone(value.policy)
        self.assertNotIn(self.lease, repr(self.dispatch))
        self.http.assert_not_called(); self.spend.assert_not_called()

    def test_original_discovery_replays_its_actual_saved_policy_after_lost_ack_without_more_gets(self):
        original_revision = self.connection.configuration_version
        data = authority.policy_data(self.value())
        first = self.discover(data=data)
        self.assertEqual(self.connection.configuration_version, original_revision + 1)
        self.assertEqual(self.discover(data=data), first)
        self.assertEqual(self.value().policy, first)
        self.assertEqual(self.http.call_count, 2)
        self.spend.assert_not_called()

    def test_raw_json_or_other_root_lease_cannot_supply_private_setup_scope(self):
        data = authority.policy_data(self.value())
        for dispatch in (self.dispatch.__dict__, {"operation_id": self.identifier, "lease_id": self.lease},
                authority.CmsSetupDispatch(str(uuid4()), self.lease), authority.CmsSetupDispatch(self.identifier, str(uuid4()))):
            with self.subTest(dispatch=type(dispatch)), self.assertRaises(WebsiteAuthorityError): self.discover(dispatch=dispatch, data=data)
        self.http.assert_not_called()

    def test_cancellation_or_replacement_during_first_get_fences_the_second_get_and_commit(self):
        original = self.http.side_effect
        def cancel(*args, **kwargs):
            result = original(*args, **kwargs)
            self.parent.state = "cancelled"
            return result
        self.http.side_effect = cancel
        with self.assertRaises(WebsiteAuthorityError): self.discover()
        self.assertEqual(self.http.call_count, 1)
        self.assertNotIn(policies.REFERENCE, self.connection.capabilities)

    def test_cancellation_after_last_get_fences_commit_under_lock(self):
        original = self.http.side_effect
        def cancel(*args, **kwargs):
            result = original(*args, **kwargs)
            if kwargs["resource"] == "categories": self.parent.state = "cancelled"
            return result
        self.http.side_effect = cancel
        with self.assertRaises(WebsiteAuthorityError): self.discover()
        self.assertEqual(self.http.call_count, 2)
        self.assertFalse(any(row.action == policies.ACTION for row in self.saved))

    def test_new_owner_connection_revision_credential_or_expired_lease_blocks_without_provider_io(self):
        for target, key, changed in ((self.user, "is_active", False), (self.company, "profile_id", 99),
                (self.connection, "generation", 2), (self.connection, "configuration_version", self.connection.configuration_version + 1),
                (self.parent, "next_attempt_at", self.now - timedelta(seconds=1))):
            original = getattr(target, key)
            setattr(target, key, changed)
            with self.subTest(key=key), self.assertRaises(WebsiteAuthorityError): self.value()
            setattr(target, key, original)
        original = self.connection.capabilities["_cms_credential_operation_id"]
        self.connection.capabilities["_cms_credential_operation_id"] = str(uuid4())
        with self.assertRaises(WebsiteAuthorityError): self.value()
        self.connection.capabilities["_cms_credential_operation_id"] = original
        self.http.assert_not_called()

    def test_rechecksummed_foreign_changed_or_deleted_intent_never_becomes_setup_authority(self):
        for changes in ({"operation_id": str(uuid4())}, {"request_id": str(uuid4())}, {"owner_user_id": "999"},
                {"binding": {**self.binding, "company_id": str(uuid4())}}):
            self.child.payload = {**deepcopy(self.intent), **changes}
            self.child.payload["digest"] = policies.digest({key: value for key, value in self.child.payload.items() if key != "digest"})
            with self.subTest(changes=changes), self.assertRaises(WebsiteAuthorityError): self.value()
        self.child.payload = deepcopy(self.intent)
        self.child.state = "deleted"
        with self.assertRaises(WebsiteAuthorityError): self.value()
        self.http.assert_not_called()

    def test_stale_or_forged_public_policy_choices_cannot_override_the_original_saved_intent(self):
        data = authority.policy_data(self.value())
        for changes in ({"review_policy": "review"}, {"client_request_id": str(uuid4())}, {"native_defaults_confirmed": 1},
                {"expected_policy_id": str(uuid4())}, {"publication_confirmed": False}, {"extra": "ignored"}):
            with self.subTest(changes=changes), self.assertRaises(WebsiteAuthorityError): self.discover(data={**data, **changes})
        self.http.assert_not_called()

    def test_background_scope_does_not_follow_the_browser_selected_company_metadata(self):
        self.profile.metadata = {"selected_company_id": str(uuid4())}
        self.assertIs(self.value().context.company, self.company)
        self.discover()
        self.assertEqual(self.http.call_count, 2)

    def test_other_real_current_policy_after_discovery_does_not_become_the_original_acknowledgement(self):
        first = self.discover()
        self.configure(client_request_id=str(uuid4()), expected_policy_id=first["operation_id"])
        self.http.reset_mock()
        with self.assertRaises(WebsiteAuthorityError): self.value()
        self.http.assert_not_called()

    def test_rollout_off_keeps_historical_accepted_intent_without_granting_provider_access(self):
        with override_settings(ARTICLES_CMS_SETUP_ENABLED=False):
            self.assertEqual(authority.historical(self.parent), self.intent)
            with self.assertRaises(WebsiteAuthorityError): self.discover(data=authority.policy_data(self.value()))
        self.http.assert_not_called()

    def test_provider_transient_is_retryable_instead_of_a_permanent_adapter_stop(self):
        for category in ("transient", "origin_unavailable"):
            self.http.side_effect = CmsApiError(category)
            with self.subTest(category=category), self.assertRaises(WebsiteAuthorityError) as caught: self.discover()
            self.assertTrue(caught.exception.retryable)
            self.assertEqual(caught.exception.status, 503)
            self.assertEqual(caught.exception.code, "website_prepare_dependency_transient")
        self.assertNotIn(policies.REFERENCE, self.connection.capabilities)

    def test_expired_successful_discovery_cannot_replay_old_policy_or_extend_its_lease(self):
        data = authority.policy_data(self.value())
        self.discover(data=data)
        self.parent.next_attempt_at = self.now - timedelta(seconds=1)
        self.http.reset_mock()
        with self.assertRaises(WebsiteAuthorityError): self.discover(data=data)
        self.http.assert_not_called()
