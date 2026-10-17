"""Native goal setup accepts one exact current owner/policy/price and lease."""

from copy import deepcopy
from datetime import timedelta
import json
from unittest.mock import Mock, patch
from uuid import uuid4

from django.test import SimpleTestCase, override_settings
from django.utils import timezone

from . import cms_goal_contract as goals
from . import cms_goal_consent as consent
from . import cms_goal_authority as authority
from . import tests_cms_publication_policy_unit as fixtures
from .channel_artifact_contract import digest
from .website_contract import WebsiteAuthorityError
from .website_models import WebsiteConnectionOperation


@override_settings(ARTICLES_CHANNEL_DISCOVERY_ENABLED=True, ARTICLES_PUBLISHING_CHANNELS_ENABLED=True,
    ARTICLES_CMS_CHANNEL_ENABLED=True, ARTICLES_CHANNEL_ARTIFACTS_ENABLED=True, ARTICLES_CMS_GOALS_ENABLED=True,
    ARTICLES_FIRST_ARTICLE_GOAL_ENABLED=True, ARTICLES_WEBSITE_GOAL_RUNNER_ENABLED=True,
    ARTICLES_WEBSITE_GOAL_ENROLLED_ORGANIZATIONS=["7"], ARTICLES_FAILURE_TAXONOMY_POLICY_ENABLED=True,
    ARTICLES_FAILURE_TAXONOMY_ENROLLED_ORGANIZATIONS=["7"],
    CONNECTOR_CREDENTIAL_KEYS=json.dumps({"test": fixtures.TEST_KEY}), CONNECTOR_CREDENTIAL_ACTIVE_KEY_ID="test")
class CmsGoalConsentTests(SimpleTestCase):
    configure = fixtures.CmsPublicationPolicyTests.configure
    reserve = fixtures.CmsPublicationPolicyTests.reserve

    def setUp(self):
        fixtures.CmsPublicationPolicyTests.setUp(self)
        self.policy = self.configure()
        self.now = timezone.now()
        identifier = str(uuid4())
        self.scope = goals.scope(self.binding, policy_operation_id=self.policy["operation_id"], policy_digest=self.policy["policy"]["digest"])
        self.goal = goals.new_goal(identifier, self.scope, now=self.now)
        self.consent = consent.seal(self.goal, user_id=self.user.pk, review_policy="auto", expected_points=6, confirmed=True, now=self.now)
        self.goal_operation = WebsiteConnectionOperation(pk=identifier, action=authority.ACTION, connection=self.connection,
            generation=1, state="running", next_attempt_at=self.now + timedelta(seconds=90),
            payload={"schema_version": 1, "binding": deepcopy(self.binding), "credential_operation_id": str(self.private.pk),
                "policy_operation_id": self.policy["operation_id"], "policy_digest": self.policy["policy"]["digest"],
                "company_id": self.binding["company_id"], "requested_by_user_id": str(self.user.pk),
                "consent": self.consent, "capture_operation_id": None, "generation_data": None, "source_run_id": None,
                "failures": 0, "lease_id": "server-lease"}, receipt={"goal": self.goal})
        self.goal_operation.save = Mock()
        self.goal_operation.refresh_from_db = Mock()
        self.saved.append(self.goal_operation)

    def test_native_setup_choice_is_bound_to_actual_owner_policy_scope_and_original_price(self):
        self.assertEqual(consent.validate(self.consent, self.goal, review_policy="auto"), self.consent)
        value = authority.current(self.goal_operation, user=self.user, lease_id="server-lease")
        self.assertEqual(value.consent["answers"]["billing"]["expected_points"], 6)
        self.assertEqual(value.scope, self.scope)
        self.assertNotIn("ci_mode", self.consent["answers"])
        self.assertNotIn("page_route", self.consent["answers"])
        self.assertNotIn("synthetic-application-password", repr(value.scope))

    def test_real_boolean_confirmation_and_nonnegative_integer_price_are_required(self):
        for changes in ({"confirmed": 1}, {"expected_points": True}, {"expected_points": -1}, {"user_id": 99}, {"review_policy": "approved"}):
            with self.subTest(changes=changes), self.assertRaises(goals.CmsGoalError):
                consent.seal(self.goal, **{"user_id": self.user.pk, "review_policy": "auto", "expected_points": 6,
                    "confirmed": True, "now": self.now, **changes})

    def test_rechecksummed_boolean_or_foreign_consent_does_not_change_native_policy_or_owner(self):
        for changed in ({"version": True}, {"owner_user_id": "99"}, {"goal_id": str(uuid4())},
                {"answers": {"publish_mode": "review", "billing": self.consent["answers"]["billing"]}},
                {"approved": True}):
            value = {**deepcopy(self.consent), **changed}
            value["digest"] = digest({key: item for key, item in value.items() if key != "digest"})
            with self.assertRaises(goals.CmsGoalError): consent.validate(value, self.goal, review_policy="auto")

    def test_expired_newer_or_cancelled_lease_cannot_dispatch_under_an_old_reference(self):
        with self.assertRaises(WebsiteAuthorityError): authority.current(self.goal_operation, lease_id="old-lease")
        self.goal_operation.next_attempt_at = self.now - timedelta(seconds=1)
        with self.assertRaises(WebsiteAuthorityError): authority.current(self.goal_operation, lease_id="server-lease")
        self.goal_operation.next_attempt_at = self.now + timedelta(seconds=90)
        self.goal_operation.state = "cancelled"
        with self.assertRaises(WebsiteAuthorityError): authority.current(self.goal_operation, lease_id="server-lease")

    def test_revocation_policy_or_owner_changes_deny_current_dispatch_but_keep_historical_financial_choice(self):
        self.private.state = "deleted"
        with self.assertRaises(WebsiteAuthorityError): authority.current(self.goal_operation)
        self.assertEqual(authority.historical(self.goal_operation).consent, self.consent)
        self.private.state = "completed"; self.user.is_active = False
        with self.assertRaises(WebsiteAuthorityError): authority.current(self.goal_operation)
        self.user.is_active = True
        self.connection.capabilities["_cms_publication_policy_id"] = str(uuid4())
        with self.assertRaises(WebsiteAuthorityError): authority.current(self.goal_operation)
        self.assertEqual(authority.historical(self.goal_operation).consent["owner_user_id"], str(self.user.pk))

    def test_rollout_and_enrollment_changes_do_not_read_current_provider_scope_or_erase_financial_history(self):
        for setting in ("ARTICLES_CMS_GOALS_ENABLED", "ARTICLES_FIRST_ARTICLE_GOAL_ENABLED", "ARTICLES_FAILURE_TAXONOMY_POLICY_ENABLED"):
            with override_settings(**{setting: False}), patch.object(authority, "current_scope") as scope:
                with self.assertRaises(WebsiteAuthorityError): authority.current(self.goal_operation)
                self.assertEqual(authority.historical(self.goal_operation).scope, self.scope)
            scope.assert_not_called()
        with override_settings(ARTICLES_WEBSITE_GOAL_ENROLLED_ORGANIZATIONS=[]), self.assertRaises(WebsiteAuthorityError):
            authority.current(self.goal_operation)

    def test_completed_goal_is_readable_only_through_explicit_read_state_and_never_a_dispatch_lease(self):
        self.goal_operation.state = "completed"
        self.goal_operation.payload.pop("lease_id")
        with self.assertRaises(WebsiteAuthorityError): authority.current(self.goal_operation)
        self.assertEqual(authority.current(self.goal_operation, states={"completed"}).goal, self.goal)
        with self.assertRaises(WebsiteAuthorityError): authority.current(self.goal_operation, states={"completed"}, lease_id="server-lease")

    def test_malformed_private_native_goal_identity_or_source_cannot_be_a_financial_reservation(self):
        original = deepcopy(self.goal_operation.payload)
        for changes in ({"schema_version": True}, {"requested_by_user_id": "99"}, {"company_id": "foreign"},
                {"credential_operation_id": "not-a-private-id"}, {"failures": True}, {"approved": True}):
            self.goal_operation.payload = {**deepcopy(original), **changes}
            with self.subTest(changes=changes), self.assertRaises(WebsiteAuthorityError): authority.historical(self.goal_operation)
