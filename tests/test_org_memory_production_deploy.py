import contextlib
import importlib.util
import io
import json
import subprocess
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


def _load_production_approval_module():
    spec = importlib.util.spec_from_file_location(
        "resolve_org_memory_production_approval",
        ROOT / "scripts" / "resolve_org_memory_production_approval.py",
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
        postmigrate = (
            ROOT / "core" / "management" / "commands" / "deploy_postmigrate.py"
        ).read_text()

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
        }
        for setting in required_settings:
            self.assertIn(f"upsert_env_value {setting}", deploy)

        self.assertIn("stage_org_memory_pilot", deploy)
        self.assertIn("recover_org_memory_stopped_worker_work", deploy)
        self.assertIn("cancel_org_memory_superseded_extraction_work", deploy)
        self.assertIn("cancel_org_memory_superseded_consolidation_work", deploy)

        # The one-shot repairs that used to be pinned here have been removed
        # from deploy.sh. Each drained on the deploy that shipped it and
        # reported zero candidates on every deploy afterwards, so they only
        # lengthened the window where web is stopped. What must stay is the
        # recurring work: recovery from the worker this deploy stops, and
        # cancellation of queued work whose target this deploy supersedes.
        retired_incident_repairs = (
            "reconcile_org_memory_access_restored_dead_letters",
            "reconcile_org_memory_consolidation_lock_dead_letters",
            "reconcile_org_memory_naive_datetime_dead_letters",
        )
        for command in retired_incident_repairs:
            self.assertNotIn(command, deploy)

        # reconcile_org_memory_extraction_dead_letters itself is *not* banned:
        # bumping the extractor version is expected to add one back for the
        # version being superseded. What must not come back are the drained
        # v1-v4 backlogs, so pin the superseded-version flags rather than the
        # command name.
        drained_extractor_versions = (
            "--superseded-extractor-version org-memory-extractor-v1",
            "--superseded-extractor-version org-memory-extractor-v2",
            "--superseded-extractor-version org-memory-extractor-v3",
            "--superseded-extractor-version org-memory-extractor-v4",
            "--superseded-schema-version org-memory-extraction-schema-v1",
        )
        for flag in drained_extractor_versions:
            self.assertNotIn(flag, deploy)

        self.assertIn(
            "paused_runtime_services=(web scheduler memory-worker memory-scheduler community-email-worker)",
            deploy,
        )
        self.assertIn('docker compose stop "\\${paused_runtime_services[@]}"', deploy)
        self.assertIn(
            'docker compose up -d --force-recreate "\\${paused_runtime_services[@]}"',
            deploy,
        )
        self.assertIn("previous_scheduler_container_id", deploy)
        self.assertIn("previous_scheduler_image_id", deploy)
        self.assertIn("previous_scheduler_tick_mtime", deploy)
        self.assertIn('docker start "\\${previous_runtime_container_ids[@]}"', deploy)
        self.assertIn("verify_scheduler_recovery_tick", deploy)
        self.assertNotIn("restore_staged_scheduler", deploy)
        self.assertNotIn("docker compose stop scheduler", deploy)
        self.assertIn("schedule_org_memory_reextraction", deploy)
        self.assertIn("reconcile_org_memory_auto_activation", deploy)
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
        # Admin Brain activation still runs after the generic post-migration
        # setup, which now includes the Firebase Storage CORS step that used to
        # anchor this assertion directly.
        self.assertIn("configure_firebase_storage_cors", postmigrate)
        self.assertGreater(
            deploy.index("activate_org_memory_pilot"),
            deploy.index("compose_run_web python manage.py deploy_postmigrate"),
        )
        self.assertLess(
            deploy.index("recover_org_memory_stopped_worker_work"),
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
            deploy.index("recover_org_memory_stopped_worker_work"),
        )
        self.assertLess(
            deploy.index("compose_run_web python manage.py deploy_preflight"),
            deploy.index('docker compose stop "\\${paused_runtime_services[@]}"'),
        )
        self.assertLess(
            deploy.index("compose_run_web python manage.py audit_office_manager_migrations"),
            deploy.index('docker compose stop "\\${paused_runtime_services[@]}"'),
        )
        post_migration_audit = deploy.index(
            "Re-auditing Office Manager provenance after migrations"
        )
        self.assertGreater(
            post_migration_audit,
            deploy.index("compose_run_web python manage.py migrate --noinput"),
        )
        self.assertLess(
            post_migration_audit,
            deploy.index("compose_run_web python manage.py deploy_postmigrate"),
        )
        self.assertIn(
            'upsert_env_value OFFICE_MANAGER_ENABLED "false"',
            deploy[post_migration_audit:],
        )
        self.assertIn(
            'docker compose up -d --force-recreate "\\${paused_runtime_services[@]}"',
            deploy[post_migration_audit:],
        )
        self.assertGreater(
            deploy.index("new_runtime_replacement_started=1"),
            deploy.index("compose_run_web python manage.py deploy_postmigrate"),
        )
        self.assertLess(
            deploy.index("schedule_org_memory_reextraction"),
            deploy.index("stage_org_memory_pilot"),
        )
        self.assertLess(
            deploy.index("reconcile_org_memory_auto_activation"),
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

    def test_office_manager_contract_docs_include_attempt_and_replay_semantics(self):
        docs = (ROOT / "docs" / "office-manager.md").read_text()

        self.assertIn('"attempt_id": "4482112f-79e1-4ca0-940b-06b24903f796"', docs)
        self.assertIn("a replay of the winning attempt is still `201`", docs)
        self.assertIn("A genuinely new `attempt_id` from", docs)
        self.assertIn("returns `200` with `status: already_claimed_by_you`", docs)
        self.assertIn("`https://api.mlai.au` in production", docs)
        self.assertIn("with no `/api/v1` path", docs)
        self.assertIn("`conversations.history`", docs)
        self.assertIn("reconcile_office_manager_provenance", docs)
        self.assertIn("roo.0037_quarantine_legacy_office_manager_provenance", docs)

        workflow = (ROOT / ".github" / "workflows" / "deploy.yml").read_text()
        self.assertIn("run: bash -n deploy.sh", workflow)
        deploy = (ROOT / "deploy.sh").read_text()
        self.assertIn("https://slack.com/api/conversations.history", deploy)
        self.assertIn(
            "Public Roo cannot inspect Office Manager message history",
            deploy,
        )

    def test_remote_deployment_script_is_valid_bash(self):
        deploy = (ROOT / "deploy.sh").read_text()
        remote = deploy.split('ssh "$DEPLOY_SSH_TARGET" <<EOF\n', 1)[1]
        remote = remote.rsplit("\nEOF", 1)[0].replace("\\$", "$")

        result = subprocess.run(
            ["bash", "-n"],
            input=remote,
            text=True,
            capture_output=True,
            check=False,
        )

        self.assertEqual(result.returncode, 0, result.stderr)

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
            "ORG_MEMORY_PRODUCTION_PUBLIC_CHANNEL_ADMIN_SCOPE_APPROVED: ${{ vars.ORG_MEMORY_PRODUCTION_PUBLIC_CHANNEL_ADMIN_SCOPE_APPROVED || 'false' }}",
            workflow,
        )
        self.assertIn(
            "scripts/resolve_org_memory_production_approval.py",
            (ROOT / "deploy.sh").read_text(),
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


class AdminBrainProductionApprovalResolutionTests(SimpleTestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.resolver = _load_production_approval_module()

    def manifest(self):
        return {
            "organization_domain": "mlai.au",
            "allowed_slack_contexts": ["dm:U090FV0GTT4"],
        }

    def test_public_admin_scope_is_added_once_without_changing_other_approval(self):
        original = self.manifest()

        resolved = self.resolver.effective_manifest(
            original,
            approve_public_admin_scope=True,
        )
        repeated = self.resolver.effective_manifest(
            resolved,
            approve_public_admin_scope=True,
        )

        self.assertEqual(original["allowed_slack_contexts"], ["dm:U090FV0GTT4"])
        self.assertEqual(
            resolved["allowed_slack_contexts"],
            ["dm:U090FV0GTT4", "public_channels:pilot_admins"],
        )
        self.assertEqual(repeated, resolved)

    def test_disabled_overlay_preserves_exact_manifest(self):
        original = self.manifest()
        self.assertEqual(
            self.resolver.effective_manifest(
                original,
                approve_public_admin_scope=False,
            ),
            original,
        )
