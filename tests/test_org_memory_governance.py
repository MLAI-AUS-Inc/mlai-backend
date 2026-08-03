from __future__ import annotations

import copy
import os
from io import StringIO
from unittest.mock import patch

from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import SimpleTestCase

from org_memory.governance import (
    GovernancePolicyError,
    SUPPORTED_PROVIDERS,
    assert_provider_ingestion_allowed,
    assert_provider_inventory_allowed,
    load_policy_manifest,
    load_seed_evaluations,
    parse_enabled_providers,
    validate_policy_manifest,
    validate_seed_evaluations,
)


def approved_drive_manifest():
    manifest = copy.deepcopy(load_policy_manifest())
    manifest["status"] = "approved"
    manifest["last_reviewed_at"] = "2026-07-20T00:00:00Z"
    manifest["owners"] = {
        "data": "Data Owner",
        "security": "Security Owner",
        "review": "Review Owner",
        "operations": "Operations Owner",
        "privacy_legal": "Privacy Owner",
    }
    manifest["global_rules"]["hard_delete_rules_approved"] = True
    manifest["slos"]["approval_status"] = "approved"
    manifest["slos"]["approved_by"] = "Operations Owner"
    manifest["slos"]["cost_limits"] = {
        "daily_model_budget_aud": 25,
        "monthly_model_budget_aud": 500,
        "on_limit": "pause_new_model_work_and_alert",
    }

    drive = manifest["providers"]["google_drive"]
    drive["production_enabled"] = True
    drive["source_scope"]["selectors"] = ["folder:synthetic-pilot"]
    drive["retention"] = {
        "raw_evidence_days": 365,
        "derived_memory_days": 730,
        "query_audit_days": 90,
    }
    drive["review_owner"] = "Review Owner"
    drive["approval"] = {
        "status": "approved",
        "approved_by": "Data Owner",
        "approved_at": "2026-07-20T00:00:00Z",
        "terms_reviewed_by": "Security Owner",
        "terms_reviewed_at": "2026-07-20T00:00:00Z",
    }
    return manifest


def draft_drive_manifest():
    manifest = copy.deepcopy(load_policy_manifest())
    manifest["status"] = "draft"
    manifest["last_reviewed_at"] = None
    manifest["owners"] = {
        "data": None,
        "security": None,
        "review": None,
        "operations": None,
        "privacy_legal": None,
    }
    manifest["global_rules"]["hard_delete_rules_approved"] = False
    manifest["slos"]["approval_status"] = "draft"
    manifest["slos"]["approved_by"] = None
    manifest["slos"]["cost_limits"] = {
        "daily_model_budget_aud": None,
        "monthly_model_budget_aud": None,
        "on_limit": "pause_new_model_work_and_alert",
    }
    drive = manifest["providers"]["google_drive"]
    drive["production_enabled"] = False
    drive["source_scope"]["selectors"] = []
    drive["retention"] = {
        "raw_evidence_days": None,
        "derived_memory_days": None,
        "query_audit_days": None,
    }
    drive["inventory"] = {
        "status": "draft",
        "approved_by": None,
        "approved_at": None,
        "privacy_approved_by": None,
        "max_files": None,
    }
    drive["review_owner"] = None
    drive["approval"] = {
        "status": "draft",
        "approved_by": None,
        "approved_at": None,
        "terms_reviewed_by": None,
        "terms_reviewed_at": None,
    }
    return manifest


class GovernanceManifestTests(SimpleTestCase):
    def test_checked_in_manifest_approves_only_the_reviewed_drive_sources(self):
        manifest = load_policy_manifest()

        self.assertEqual(
            validate_policy_manifest(
                manifest,
                enabled_providers={"google_drive"},
                production=True,
            ),
            [],
        )
        self.assertEqual(set(manifest["providers"]), SUPPORTED_PROVIDERS)
        self.assertEqual(
            {
                provider
                for provider, policy in manifest["providers"].items()
                if policy["production_enabled"]
            },
            {"google_drive"},
        )
        self.assertEqual(
            manifest["providers"]["google_drive"]["source_scope"]["selectors"],
            [
                "organization:16",
                "connection:12",
                "folder:1UBvYpQuZiug1QB3KDotTJtd5xltQdFKp",
                "folder:1NnAt4Sio7CDLXsOsSR3rompYa0_ouDvY",
            ],
        )

        for folder_id in (
            "1UBvYpQuZiug1QB3KDotTJtd5xltQdFKp",
            "1NnAt4Sio7CDLXsOsSR3rompYa0_ouDvY",
        ):
            policy = assert_provider_inventory_allowed(
                "google_drive",
                {
                    "organization:16",
                    "connection:12",
                    f"folder:{folder_id}",
                },
                requested_max_files=10000,
                manifest=manifest,
            )
            self.assertTrue(policy["production_enabled"])

        with self.assertRaises(GovernancePolicyError):
            assert_provider_inventory_allowed(
                "google_drive",
                {
                    "organization:16",
                    "connection:12",
                    "folder:unreviewed-folder",
                },
                requested_max_files=10000,
                manifest=manifest,
            )

    def test_enabled_provider_parser_accepts_commas_spaces_and_iterables(self):
        self.assertEqual(
            parse_enabled_providers("google_drive, slack linear"),
            {"google_drive", "slack", "linear"},
        )
        self.assertEqual(parse_enabled_providers(["notion", "gmail"]), {"notion", "gmail"})

    def test_draft_drive_policy_cannot_be_enabled_in_production(self):
        errors = validate_policy_manifest(
            draft_drive_manifest(),
            enabled_providers={"google_drive"},
            production=True,
        )

        self.assertTrue(any("manifest status" in error for error in errors))
        self.assertTrue(any("google_drive.production_enabled" in error for error in errors))
        self.assertTrue(any("google_drive.approval.status" in error for error in errors))
        self.assertTrue(any("owners.security" in error for error in errors))
        self.assertTrue(any("daily_model_budget_aud" in error for error in errors))

    def test_complete_approved_drive_policy_passes_production_gate(self):
        manifest = approved_drive_manifest()

        self.assertEqual(
            validate_policy_manifest(
                manifest,
                enabled_providers={"google_drive"},
                production=True,
            ),
            [],
        )
        policy = assert_provider_ingestion_allowed(
            "google_drive",
            manifest=manifest,
            production=True,
        )
        self.assertTrue(policy["production_enabled"])

    def test_ingestion_guard_fails_closed_for_draft_policy(self):
        with self.assertRaises(GovernancePolicyError) as context:
            assert_provider_ingestion_allowed(
                "google_drive",
                manifest=draft_drive_manifest(),
                production=True,
            )

        self.assertIn("ingestion is denied", str(context.exception))

    def test_unknown_provider_is_rejected(self):
        with self.assertRaises(GovernancePolicyError):
            assert_provider_ingestion_allowed(
                "dropbox",
                manifest=load_policy_manifest(),
                production=True,
            )

    def test_slack_dm_ingestion_cannot_be_enabled(self):
        manifest = load_policy_manifest()
        manifest["providers"]["slack"]["source_scope"]["direct_messages"] = "included"

        errors = validate_policy_manifest(manifest)

        self.assertIn(
            "providers.slack.source_scope.direct_messages must be 'excluded' for the MVP.",
            errors,
        )

    def test_finance_and_luma_privacy_invariants_are_mandatory(self):
        manifest = load_policy_manifest()
        manifest["providers"]["stripe"]["aggregate_only"] = False
        manifest["providers"]["xero"]["aggregate_only"] = False
        manifest["providers"]["luma"]["attendee_pii_allowed"] = True

        errors = validate_policy_manifest(manifest)

        self.assertIn("providers.stripe.aggregate_only must be true.", errors)
        self.assertIn("providers.xero.aggregate_only must be true.", errors)
        self.assertIn("providers.luma.attendee_pii_allowed must be false.", errors)

    def test_drive_inventory_is_denied_until_scope_and_ceiling_are_approved(self):
        with self.assertRaises(GovernancePolicyError) as context:
            assert_provider_inventory_allowed(
                "google_drive",
                {"folder:synthetic-root"},
                requested_max_files=100,
                manifest=draft_drive_manifest(),
            )

        self.assertIn("inventory.status is not 'approved'", str(context.exception))
        self.assertIn("Inventory selectors are not approved", str(context.exception))

    def test_drive_inventory_approval_is_separate_from_production_ingestion(self):
        manifest = draft_drive_manifest()
        manifest["owners"]["data"] = "Data Owner"
        manifest["owners"]["security"] = "Security Owner"
        drive = manifest["providers"]["google_drive"]
        drive["source_scope"]["selectors"] = [
            "organization:synthetic-org",
            "connection:synthetic-connection",
            "folder:synthetic-root",
        ]
        drive["inventory"] = {
            "status": "approved",
            "approved_by": "Data Owner",
            "approved_at": "2026-07-20T00:00:00Z",
            "privacy_approved_by": "Security Owner",
            "max_files": 500,
        }

        policy = assert_provider_inventory_allowed(
            "google_drive",
            {
                "organization:synthetic-org",
                "connection:synthetic-connection",
                "folder:synthetic-root",
            },
            requested_max_files=500,
            manifest=manifest,
        )

        self.assertFalse(policy["production_enabled"])
        with self.assertRaises(GovernancePolicyError):
            assert_provider_inventory_allowed(
                "google_drive",
                {
                    "organization:synthetic-org",
                    "connection:synthetic-connection",
                    "folder:synthetic-root",
                },
                requested_max_files=501,
                manifest=manifest,
            )


class GovernanceCommandTests(SimpleTestCase):
    def test_command_accepts_checked_in_approved_drive_provider(self):
        out = StringIO()
        with patch.dict(os.environ, {"ORG_MEMORY_ENABLED_PROVIDERS": "google_drive"}):
            call_command(
                "validate_org_memory_governance",
                environment="production",
                stdout=out,
            )

        self.assertIn("requested providers: google_drive", out.getvalue())

    def test_command_rejects_unapproved_provider_in_production(self):
        with patch.dict(os.environ, {"ORG_MEMORY_ENABLED_PROVIDERS": "slack"}):
            with self.assertRaises(CommandError):
                call_command(
                    "validate_org_memory_governance",
                    environment="production",
                    stdout=StringIO(),
                    stderr=StringIO(),
                )


class SeedEvaluationFixtureTests(SimpleTestCase):
    def test_seed_evaluations_are_valid_and_cover_pr0_categories(self):
        cases = load_seed_evaluations()

        self.assertEqual(validate_seed_evaluations(cases), [])
        categories = {case["category"] for case in cases}
        self.assertTrue(
            {
                "permission",
                "temporal",
                "current_state",
                "contradiction",
                "abstention",
                "prompt_injection",
                "citation",
            }.issubset(categories)
        )

    def test_seed_evaluations_use_only_synthetic_provider_records(self):
        for case in load_seed_evaluations():
            self.assertTrue(case["id"])
            for evidence in case["evidence"]:
                self.assertIn(evidence["provider"], SUPPORTED_PROVIDERS)
                self.assertNotIn("@mlai", evidence.get("text", "").lower())

    def test_malformed_seed_case_returns_errors_instead_of_crashing(self):
        errors = validate_seed_evaluations(
            [
                {
                    "id": "malformed",
                    "category": "permission",
                    "question": "Synthetic question?",
                    "actor": {},
                    "evidence": None,
                    "expected": {"outcome": "deny"},
                }
            ]
        )

        self.assertIn("case[1].evidence must be a list.", errors)
        self.assertIn("case[1].expected.required_evidence_ids must be a list.", errors)
