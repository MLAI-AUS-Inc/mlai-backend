"""The website status P2 fields (be-registry): L1 for the website status, L5 and ``copyId`` (IF-P2-05, IF-P2-06)."""
from __future__ import annotations

from copy import deepcopy
from datetime import timedelta
import json
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from django.test import TestCase, override_settings
from django.utils import timezone

from . import website_status as status
from . import tests_website_goal_unit as fixtures
from .self_heal_testing import ALL_FLAGS_ON, make_named_legacy_org, make_p2_org, self_heal_receipt

STATUS_FLAGS = dict(ARTICLES_WEBSITE_GOAL_RUNNER_ENABLED=True, ARTICLES_WEBSITE_GOAL_ENROLLED_ORGANIZATIONS=["7"],
                    ARTICLES_FAILURE_TAXONOMY_POLICY_ENABLED=True, ARTICLES_FAILURE_TAXONOMY_ENROLLED_ORGANIZATIONS=["7"])
SCENARIOS = (
    ("needs_user", {"user_action": {"id": "review_setup", "run_id": "bound"}, "setup_run_id": "bound",
                    "reason_code": "ARTICLE_SYSTEM_SETUP_REQUIRED"}),
    ("needs_user", {"user_action": {"id": "merge_pr", "run_id": "bound", "pr_number": 4}, "setup_run_id": "bound",
                    "reason_code": "manual_merge_required"}),
    ("needs_user", {"user_action": {"id": "review_github_permissions"},
                    "reason_code": "github_workflow_permission_required"}),
    ("needs_user", {"user_action": {"id": "deploy_site", "run_id": "bound"}, "setup_run_id": "bound"}),
    ("pending", {"reason_code": "GITHUB_TEMPORARILY_UNAVAILABLE"}),
    ("failed", {"reason_code": "SCAFFOLD_PREP_FAILED"}),
    ("failed", {"reason_code": "NOT_A_CODE", "self_heal_v1": self_heal_receipt(next_at="2026-11-01T00:00:00+00:00")}),
    ("cancelled", {}),
)


@override_settings(**STATUS_FLAGS)
class WebsiteStatusP2Tests(TestCase):
    def setUp(self):
        self.now = timezone.now()
        self.journey = {"prerequisites": {"account": {"status": "complete"}, "repository": {"status": "complete"}},
                        "capabilities": {"canPublishArticle": True}}

    def _op(self, state, receipt):
        op = fixtures.GoalRunnerRegressions().operation()
        op.action, op.state = "prepare", state
        op.connection.verified_sha = "a" * 40
        op.connection.github_repo = "fixture/site"
        op.updated_at = self.now
        op.next_attempt_at = self.now + timedelta(seconds=30)
        op.receipt = {**op.receipt, **deepcopy(receipt)}
        return op

    def _project(self, op):
        return status.project_status(op, scope=fixtures.scope(), source_sha="a" * 40, journey=self.journey, now=self.now)

    def _status(self, state, receipt, organization):
        op = self._op(state, receipt)
        today = self._project(op)
        return today, status.add_p2_fields(deepcopy(today), op, organization=organization)

    @override_settings(**ALL_FLAGS_ON)
    def test_l1_legacy_status_is_byte_identical_with_every_flag_on(self):
        for name in ("talathrive", "vyavos", "pawshare", "la_vie", "mlai"):
            legacy = make_named_legacy_org(name).organization
            for state, receipt in SCENARIOS:
                with self.subTest(name=name, state=state, receipt=receipt):
                    today, result = self._status(state, receipt, legacy)
                    self.assertEqual(json.dumps(result, sort_keys=False), json.dumps(today, sort_keys=False))
                    self.assertNotIn("copyId", result)

    def test_flags_off_a_p2_org_is_unchanged(self):
        org = make_p2_org().organization
        for state, receipt in SCENARIOS:
            with self.subTest(state=state, receipt=receipt), self.assertNumQueries(0):
                today, result = self._status(state, receipt, org)
                self.assertEqual(json.dumps(result), json.dumps(today))

    @override_settings(**{**ALL_FLAGS_ON, "MY_STARTUP_CANONICAL_ACTION_IDS_ENABLED": False})
    def test_l5_todays_ids_with_canonical_ids_off(self):
        org = make_p2_org().organization
        for state, receipt in SCENARIOS[:4]:
            with self.subTest(receipt=receipt):
                today, result = self._status(state, receipt, org)
                self.assertEqual(result["action"], today["action"])
                self.assertEqual(result["action"]["id"], receipt["user_action"]["id"])

    @override_settings(**ALL_FLAGS_ON)
    def test_l5_canonical_ids_with_canonical_ids_on(self):
        org = make_p2_org().organization
        expected = {"review_setup": ("approve_setup", "Add to my website"),
                    "merge_pr": ("approve_on_github", "Approve on GitHub"),
                    "review_github_permissions": ("authorize_github", "Give access on GitHub"),
                    "deploy_site": ("deploy_site", "Deploy your site")}
        for state, receipt in SCENARIOS[:4]:
            legacy_id = receipt["user_action"]["id"]
            with self.subTest(action=legacy_id):
                today, result = self._status(state, receipt, org)
                self.assertEqual((result["action"]["id"], result["action"]["label"]), expected[legacy_id])
                self.assertEqual({key: value for key, value in result["action"].items() if key not in ("id", "label")},
                                 {key: value for key, value in today["action"].items() if key not in ("id", "label")})
                self.assertEqual(result["state"], "needs_you")

    @override_settings(**ALL_FLAGS_ON)
    def test_copy_ids_for_a_p2_org_with_the_policy_flag(self):
        org = make_p2_org().organization
        expected = ["ask_approve_setup", "ask_approve_on_github", "ask_authorize_github", "working", "working",
                    "stopped", "working", "stopped"]
        for (state, receipt), copy_id in zip(SCENARIOS, expected):
            with self.subTest(state=state, receipt=receipt):
                _, result = self._status(state, receipt, org)
                self.assertEqual(result.get("copyId"), copy_id)

    @override_settings(**{**ALL_FLAGS_ON, "ARTICLES_FAILURE_POLICY_V2_ENABLED": False})
    def test_no_copy_id_while_the_policy_flag_is_off(self):
        org = make_p2_org().organization
        for state, receipt in SCENARIOS:
            with self.subTest(state=state, receipt=receipt):
                _, result = self._status(state, receipt, org)
                self.assertNotIn("copyId", result)

    @override_settings(**ALL_FLAGS_ON)
    def test_a_finished_goal_gets_no_copy_id(self):
        from . import website_goal as goals
        org = make_p2_org().organization
        op = self._op("completed", {})
        op.receipt["goal"] = goals.refresh_goal(op.receipt["goal"], fixtures.proof_set(self.now), scope=fixtures.scope(),
                                                source_sha="a" * 40, now=self.now)
        result = status.add_p2_fields(self._project(op), op, organization=org)
        self.assertEqual(result["state"], "done")
        self.assertNotIn("copyId", result)

    @override_settings(**ALL_FLAGS_ON)
    def test_lossy_legacy_action_never_overrides_registered_code(self):
        from . import registry_policy
        org = make_p2_org().organization
        from .website_failure_contract import load_registry
        codes = {entry["code"] for entry in load_registry()["by_code"].values()
                 if entry.get("action_id") == "review_setup"}
        codes.update({"COMPANY_CONTEXT_INCOMPLETE", "CONNECTION_READ_ONLY", "github_revocation_review_required",
                      "VALIDATION_ERROR", "MISSING_CONFIG", "BRANCH_PROTECTION_APPROVAL_REQUIRED", "SITE_TEST_CHANGE_REQUIRED"})
        for code in codes:
            for canonical in (False, True):
                with self.subTest(code=code, canonical=canonical), \
                        override_settings(MY_STARTUP_CANONICAL_ACTION_IDS_ENABLED=canonical):
                    _, result = self._status("needs_user", {"reason_code": code, "user_action": {"id": "review_setup"}}, org)
                    expected_ask = registry_policy.founder_ask_for_code(code, org)
                    self.assertEqual(result["copyId"], registry_policy.copy_id_for(
                        code, organization=org, terminal=False, self_heal=None))
                    if expected_ask:
                        self.assertEqual(result["action"]["id"], registry_policy.wire_action_id(expected_ask))
                    else:
                        self.assertIsNone(result["action"])
                        self.assertNotEqual(result["state"], "needs_you")

    @override_settings(**ALL_FLAGS_ON)
    def test_stored_canonical_ids_remain_owner_actions_for_both_cohorts(self):
        orgs = (make_p2_org().organization, make_named_legacy_org("vyavos").organization)
        cases = (("approve_setup", "ARTICLE_SYSTEM_SETUP_REQUIRED", "review_setup"),
                 ("approve_on_github", "BRANCH_PROTECTION_APPROVAL_REQUIRED", "merge_pr"),
                 ("describe_customer", "COMPANY_CONTEXT_INCOMPLETE", "review_setup"),
                 ("agree_terms", "TERMS_NOT_ACCEPTED", "review_setup"))
        for org in orgs:
            for canonical in (False, True):
                for stored, code, legacy_id in cases:
                    with self.subTest(org=org.pk, canonical=canonical, stored=stored), \
                            override_settings(MY_STARTUP_CANONICAL_ACTION_IDS_ENABLED=canonical):
                        today, result = self._status("needs_user", {"reason_code": code, "user_action": {"id": stored}}, org)
                        self.assertEqual(today["state"], "needs_you")
                        self.assertEqual(today["action"]["id"], legacy_id)
                        if "copyId" not in result or not canonical:
                            self.assertEqual(result["action"]["id"], legacy_id)
                        else:
                            self.assertEqual(result["action"]["id"], stored)

    @override_settings(**ALL_FLAGS_ON)
    def test_status_for_context_applies_the_p2_fields_with_the_connection_org(self):
        legacy = make_named_legacy_org("talathrive")
        p2 = make_p2_org()
        for built, has_copy in ((legacy, False), (p2, True)):
            with self.subTest(org=built.organization.domain):
                op = self._op("failed", {"reason_code": "SCAFFOLD_PREP_FAILED"})
                connection = MagicMock(organization=built.organization, pk=5, generation=3, configuration_version=1)
                connection.operations.filter.return_value.filter.return_value.order_by.return_value.first.return_value = op
                config = SimpleNamespace(website_connection=connection)
                context = SimpleNamespace(company=SimpleNamespace(pk=9))
                with override_settings(ARTICLES_WEBSITE_STATUS_V1_ENABLED=True, ARTICLES_WEBSITE_DOCTOR_ENABLED=False), \
                        patch.object(status.goals, "scope_for", return_value=fixtures.scope()), \
                        patch.object(status, "source_for", return_value="a" * 40), \
                        patch("content_factory.website_native_inventory.add_display", side_effect=lambda result, op: result):
                    result = status.status_for_context(context, config, journey=self.journey, now=self.now)
                self.assertEqual("copyId" in result, has_copy)
                if not has_copy:
                    self.assertEqual(result, self._project(op))
