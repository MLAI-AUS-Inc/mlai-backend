import contextlib
import importlib.util
import io
import json
import tempfile
from pathlib import Path

from django.test import SimpleTestCase


ROOT = Path(__file__).resolve().parents[1]


def _load_staging_skip_module():
    spec = importlib.util.spec_from_file_location(
        "org_memory_staging_skip",
        ROOT / "scripts" / "org_memory_staging_skip.py",
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _blocked_staging_stdout(blockers):
    return "\n".join(
        (
            "DEBUG: ESAFETY VIEWS MODULE LOADED",
            "Initializing Firebase with project ID: mlai-main-website",
            json.dumps(
                {
                    "applied": False,
                    "readiness": {
                        "blockers": blockers,
                        "ready": not blockers,
                        "warnings": ["selector_label_gate_not_met"],
                    },
                },
                sort_keys=True,
            ),
        )
    )


class OrgMemoryProductionDeployTests(SimpleTestCase):
    def test_admin_brain_deploy_is_explicit_direct_production_and_non_shadow(self):
        deploy = (ROOT / "deploy.sh").read_text()

        required_settings = {
            'ORG_MEMORY_QUERY_API_ENABLED "true"',
            'ORG_MEMORY_PILOT_ORGANIZATION_DOMAIN "mlai.au"',
            'ORG_MEMORY_EXTRACTOR_VERSION "org-memory-extractor-v5"',
            'ORG_MEMORY_EXTRACTION_SCHEMA_VERSION "org-memory-extraction-schema-v2"',
            'ORG_MEMORY_EXTRACTION_PROMPT_VERSION "org-memory-extraction-prompt-v2"',
            'ORG_MEMORY_ENABLED_PROVIDERS "google_drive"',
            'ORG_MEMORY_PUBLICATION_ENABLED "false"',
            'ORG_MEMORY_ACTIONS_ENABLED "false"',
            'ORG_MEMORY_ACTION_LINEAR_EXECUTION_ENABLED "false"',
            'ORG_MEMORY_SELECTOR_EXPORT_ENABLED "false"',
            'ORG_MEMORY_SELECTOR_SHADOW_ENABLED "false"',
        }
        for setting in required_settings:
            self.assertIn(f"upsert_env_value {setting}", deploy)

        self.assertIn("stage_org_memory_pilot", deploy)
        self.assertIn("reconcile_org_memory_access_restored_dead_letters", deploy)
        self.assertIn("recover_org_memory_stopped_worker_work", deploy)
        self.assertIn("reconcile_org_memory_consolidation_lock_dead_letters", deploy)
        self.assertIn("cancel_org_memory_superseded_extraction_work", deploy)
        self.assertIn("cancel_org_memory_superseded_consolidation_work", deploy)
        self.assertIn("reconcile_org_memory_extraction_dead_letters", deploy)
        self.assertIn("--superseded-extractor-version org-memory-extractor-v1", deploy)
        self.assertIn("--superseded-prompt-version org-memory-extraction-prompt-v1", deploy)
        self.assertIn("--superseded-extractor-version org-memory-extractor-v2", deploy)
        self.assertIn("--superseded-extractor-version org-memory-extractor-v3", deploy)
        self.assertIn("--superseded-extractor-version org-memory-extractor-v4", deploy)
        self.assertIn("paused_runtime_services=(web memory-worker memory-scheduler)", deploy)
        self.assertIn('docker compose stop "\\${paused_runtime_services[@]}"', deploy)
        self.assertIn(
            'docker compose up -d --force-recreate "\\${paused_runtime_services[@]}"',
            deploy,
        )
        self.assertIn("schedule_org_memory_reextraction", deploy)
        self.assertIn("refresh_org_memory_daily_reconciliation", deploy)
        self.assertIn("request_org_memory_reprocess", deploy)
        self.assertIn("committee-drive-parser-v2-extraction-v2", deploy)
        self.assertIn("activate_org_memory_pilot", deploy)
        self.assertIn("--environment production", deploy)
        self.assertIn("--require-active", deploy)
        self.assertIn("check_org_memory_pilot_access_matrix", deploy)
        self.assertIn("compose_run_web_with_approval", deploy)
        self.assertIn('ORG_MEMORY_PRODUCTION_DEPLOY_ENABLED="${ORG_MEMORY_PRODUCTION_DEPLOY_ENABLED:-false}"', deploy)
        self.assertIn('upsert_env_value ORG_MEMORY_QUERY_API_ENABLED "false"', deploy)
        self.assertIn('if [ "\\$org_memory_production_deploy_enabled" = "true" ]; then', deploy)
        self.assertNotIn("--environment staging", deploy)
        self.assertGreater(
            deploy.index("activate_org_memory_pilot"),
            deploy.index("configure_firebase_storage_cors"),
        )
        self.assertLess(
            deploy.index("reconcile_org_memory_access_restored_dead_letters"),
            deploy.index("stage_org_memory_pilot"),
        )
        self.assertLess(
            deploy.index("reconcile_org_memory_consolidation_lock_dead_letters"),
            deploy.index("stage_org_memory_pilot"),
        )
        self.assertLess(
            deploy.index("cancel_org_memory_superseded_extraction_work"),
            deploy.index("schedule_org_memory_reextraction"),
        )
        self.assertLess(
            deploy.index("cancel_org_memory_superseded_consolidation_work"),
            deploy.index("schedule_org_memory_reextraction"),
        )
        self.assertLess(
            deploy.index('docker compose stop "\\${paused_runtime_services[@]}"'),
            deploy.index("reconcile_org_memory_access_restored_dead_letters"),
        )
        self.assertLess(
            deploy.index('docker compose stop "\\${paused_runtime_services[@]}"'),
            deploy.index("recover_org_memory_stopped_worker_work"),
        )
        self.assertLess(
            deploy.index("reconcile_org_memory_extraction_dead_letters"),
            deploy.index("stage_org_memory_pilot"),
        )
        self.assertLess(
            deploy.index("schedule_org_memory_reextraction"),
            deploy.index("stage_org_memory_pilot"),
        )
        self.assertLess(
            deploy.index("refresh_org_memory_daily_reconciliation"),
            deploy.index("stage_org_memory_pilot"),
        )
        self.assertGreater(
            deploy.index("request_org_memory_reprocess"),
            deploy.index("check_org_memory_pilot_access_matrix"),
        )

    def test_admin_brain_staging_awaiting_evidence_skips_but_stays_fail_closed(self):
        deploy = (ROOT / "deploy.sh").read_text()

        # The staging attempt is guarded so a blocked readiness report can be
        # inspected instead of tripping the ERR trap immediately.
        staging_guard = 'if [ "\\$staging_applied" = "1" ]; then'
        self.assertIn(staging_guard, deploy)
        self.assertIn("scripts/org_memory_staging_skip.py", deploy)

        # Activation and every post-binding verification run only after a
        # successful staging apply.
        skip_probe_index = deploy.index("scripts/org_memory_staging_skip.py")
        self.assertGreater(deploy.index(staging_guard), deploy.index("stage_org_memory_pilot"))
        for verified_only_after_staging in (
            "activate_org_memory_pilot",
            "check_org_memory_pilot_release_gate",
            "report_org_memory_pilot_deployment",
            "check_org_memory_pilot_access_matrix",
        ):
            self.assertGreater(
                deploy.index(verified_only_after_staging),
                deploy.index(staging_guard),
            )
            self.assertLess(
                deploy.index(verified_only_after_staging),
                skip_probe_index,
            )

        # The awaiting-evidence skip re-disables the query API before runtime
        # services start, so retrieval stays fail-closed without a binding.
        self.assertGreater(
            deploy.rindex('upsert_env_value ORG_MEMORY_QUERY_API_ENABLED "false"'),
            skip_probe_index,
        )
        self.assertIn(
            "Admin Brain pilot staging skipped: the pilot has no retrievable evidence yet.",
            deploy,
        )

        # Any other staging failure still aborts the deploy through the ERR
        # trap so web is restored.
        self.assertIn(
            "Admin Brain production staging failed; aborting the deploy.",
            deploy,
        )

    def test_main_deploy_requires_approval_key_and_independent_operators(self):
        workflow = (ROOT / ".github" / "workflows" / "deploy.yml").read_text()

        for secret_name in (
            "ORG_MEMORY_PILOT_ALLOWLIST_KEY_VERSION",
            "ORG_MEMORY_PILOT_ALLOWLIST_HMAC_KEY",
            "ORG_MEMORY_PRODUCTION_APPROVAL_MANIFEST",
            "ORG_MEMORY_PRODUCTION_STAGE_OPERATOR_EMAIL",
            "ORG_MEMORY_PRODUCTION_ACTIVATION_OPERATOR_EMAIL",
        ):
            self.assertIn(f"secrets.{secret_name}", workflow)
        self.assertIn(
            "Admin Brain production staging and activation operators must be distinct",
            workflow,
        )
        self.assertIn(
            "ORG_MEMORY_PRODUCTION_DEPLOY_ENABLED: ${{ vars.ORG_MEMORY_PRODUCTION_DEPLOY_ENABLED || 'false' }}",
            workflow,
        )
        self.assertIn(
            'if [ "$ORG_MEMORY_PRODUCTION_DEPLOY_ENABLED" = "true" ]; then',
            workflow,
        )


class AdminBrainStagingSkipDecisionTests(SimpleTestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.skip_module = _load_staging_skip_module()

    def test_skips_when_only_blocker_is_missing_retrievable_evidence(self):
        stdout_text = _blocked_staging_stdout(["retrievable_evidence_missing"])
        self.assertTrue(self.skip_module.staging_skip_allowed(stdout_text))

    def test_fails_when_any_non_tolerated_blocker_is_present(self):
        stdout_text = _blocked_staging_stdout(
            ["retrievable_evidence_missing", "connections_not_approved"]
        )
        self.assertFalse(self.skip_module.staging_skip_allowed(stdout_text))

    def test_fails_when_readiness_report_is_absent(self):
        for stdout_text in (
            "",
            "CommandError: Organization does not exist.",
            "DEBUG: ESAFETY VIEWS MODULE LOADED\nnot-json {",
        ):
            with self.subTest(stdout_text=stdout_text):
                self.assertFalse(
                    self.skip_module.staging_skip_allowed(stdout_text)
                )

    def test_fails_on_successful_apply_output_shape(self):
        # A post-readiness apply failure prints the deployment result shape
        # (readiness_hash string, no readiness object) or nothing at all;
        # neither may be treated as the awaiting-evidence state.
        stdout_text = json.dumps(
            {"applied": True, "readiness_hash": "ab" * 32, "state": "staged"}
        )
        self.assertFalse(self.skip_module.staging_skip_allowed(stdout_text))

    def test_fails_on_empty_blocker_list(self):
        stdout_text = _blocked_staging_stdout([])
        self.assertFalse(self.skip_module.staging_skip_allowed(stdout_text))

    def test_last_readiness_report_in_output_wins(self):
        tolerated = _blocked_staging_stdout(["retrievable_evidence_missing"])
        blocked = _blocked_staging_stdout(["pilot_actors_not_exact"])
        self.assertFalse(
            self.skip_module.staging_skip_allowed(tolerated + "\n" + blocked)
        )
        self.assertTrue(
            self.skip_module.staging_skip_allowed(blocked + "\n" + tolerated)
        )

    def test_main_exit_codes(self):
        quiet = contextlib.ExitStack()
        quiet.enter_context(contextlib.redirect_stdout(io.StringIO()))
        quiet.enter_context(contextlib.redirect_stderr(io.StringIO()))
        self.addCleanup(quiet.close)
        with tempfile.TemporaryDirectory() as tmp:
            tolerated_path = Path(tmp) / "tolerated.txt"
            tolerated_path.write_text(
                _blocked_staging_stdout(["retrievable_evidence_missing"]),
                encoding="utf-8",
            )
            blocked_path = Path(tmp) / "blocked.txt"
            blocked_path.write_text(
                _blocked_staging_stdout(["provider_governance_invalid"]),
                encoding="utf-8",
            )
            self.assertEqual(
                self.skip_module.main(["prog", str(tolerated_path)]), 0
            )
            self.assertEqual(
                self.skip_module.main(["prog", str(blocked_path)]), 1
            )
            self.assertEqual(
                self.skip_module.main(
                    ["prog", str(Path(tmp) / "missing.txt")]
                ),
                2,
            )
            self.assertEqual(self.skip_module.main(["prog"]), 2)
