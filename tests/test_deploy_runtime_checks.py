"""Execute deployed validators without credentials, services, or a database."""

import hashlib
import json
from pathlib import Path
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import unittest

from roo.office_manager_policy import OFFICE_MANAGER_TEST_CHANNEL_ID
from scripts.validate_website_deploy_config import canonical_migration_plan, validate as validate_website, validate_single


DEPLOY_SCRIPT = (Path(__file__).resolve().parents[1] / "deploy.sh").read_text()


class DeploymentRuntimeChecksTests(unittest.TestCase):
    def test_failed_post_audit_keeps_code_only_runtime_serving(self):
        start = '    if ! run_office_manager_migration_audit "\\$office_manager_post_attestation"; then'
        branch = DEPLOY_SCRIPT.split(start, 1)[1].split(
            '    # Migration readiness, vector installation', 1
        )[0]
        branch = (start + branch).replace("\\$", "$")
        for migrations_pending, should_recreate in (("0", False), ("1", True)):
            with self.subTest(migrations_pending=migrations_pending):
                script = "\n".join([
                    "set -euo pipefail",
                    f"migrations_pending={migrations_pending}",
                    'office_manager_post_attestation=reviewed',
                    'runtime_services=(web scheduler)',
                    'runtime_restore_attempted=0',
                    'run_office_manager_migration_audit() { return 1; }',
                    'upsert_env_value() { :; }',
                    'restore_host_writer_watchdogs() { :; }',
                    'writer_pause_sentinel="$(mktemp)"',
                    'docker() { echo docker-recreate; }',
                    'verify_scheduler_recovery_tick() { echo scheduler-verified; }',
                    branch,
                    'echo unexpectedly-continued',
                ])
                result = subprocess.run(["bash", "-c", script], text=True, capture_output=True, timeout=5)
                self.assertEqual(result.returncode, 1, result.stderr)
                self.assertNotIn("unexpectedly-continued", result.stdout)
                self.assertEqual("docker-recreate" in result.stdout, should_recreate)
                if not should_recreate:
                    self.assertIn("existing runtime remains online", result.stdout)

    def test_migration_free_release_keeps_runtime_online_during_checks(self):
        # Execute the deployment's actual decision block with stubbed Django
        # commands. No database or production service is involved.
        start = '    echo "🔎 Checking pending migrations before pausing runtime services..."'
        decision = DEPLOY_SCRIPT.split(start, 1)[1].split(
            "    # Recovery disables errexit", 1
        )[0]
        decision = (start + decision).replace("\\$", "$")
        plan = "Planned operations:\napp.0001_example\n    Example"
        approved_hash = hashlib.sha256(plan.encode()).hexdigest()
        for check_result, plan_result, approval, expected_exit, expected_pending, expected_calls in (
            (0, 0, "", 0, "0", "check,"),
            (1, 0, "", 1, None, "check,plan,"),
            (1, 0, "wrong-plan-hash", 1, None, "check,plan,"),
            (1, 0, hashlib.sha256((plan + " changed").encode()).hexdigest(), 1, None, "check,plan,"),
            (1, 0, approved_hash, 0, "1", "check,plan,"),
            (1, 2, approved_hash, 2, None, "check,plan,"),
        ):
            with self.subTest(check_result=check_result, approval=approval, plan_result=plan_result):
                script = "\n".join([
                    "set -euo pipefail",
                    # This test exercises the steady-state migration gate;
                    # first proxy adoption has its own fail-closed check.
                    "web_proxy_preexisting=1",
                    'calls_file="$(mktemp)"',
                    "compose_run_web() {",
                    "  case \" $* \" in",
                    "    *' --check '*) printf check, >> \"$calls_file\"; return " + str(check_result) + ";;",
                    "    *' --plan '*) printf plan, >> \"$calls_file\"; printf '%s\\n' " + shlex.quote(plan) + "; return " + str(plan_result) + ";;",
                    "  esac",
                    "}",
                    # Production uses GNU sha256sum; use Python in the shell
                    # harness so macOS exercises the same hash/approval gate.
                    'sha256sum() { ' + shlex.quote(sys.executable)
                    + ' -c "import hashlib,sys; print(hashlib.sha256(sys.stdin.buffer.read()).hexdigest())"; }',
                    'read_env_value() { echo "' + approval + '"; }',
                    'trap \'printf "calls=%s\\n" "$(cat "$calls_file")"; rm -f "$calls_file"\' EXIT',
                    decision,
                    'printf "pending=%s\\n" "$migrations_pending"',
                ])
                result = subprocess.run(["bash", "-c", script], text=True, capture_output=True, timeout=5)
                self.assertEqual(result.returncode, expected_exit, result.stderr)
                self.assertIn(f"calls={expected_calls}", result.stdout)
                if expected_pending is not None:
                    self.assertIn(f"pending={expected_pending}", result.stdout)
                else:
                    self.assertNotIn("pending=", result.stdout)

    def test_reviewed_multiline_migration_plan_uses_exact_shell_digest(self):
        start = '    echo "🔎 Checking pending migrations before pausing runtime services..."'
        decision = (start + DEPLOY_SCRIPT.split(start, 1)[1].split("    # Recovery disables errexit", 1)[0]).replace("\\$", "$")
        plan = "Planned operations:\ncontent_factory.0042_website_connection_lifecycle\n    Create model WebsiteConnection"
        for reviewed_plan, expected in ((plan, 0), (plan + "\n    Create model Unreviewed", 1)):
            approved = hashlib.sha256(reviewed_plan.encode()).hexdigest()
            script = "\n".join([
                "set -euo pipefail", "web_proxy_preexisting=1",
                "compose_run_web() { case \" $* \" in *' --check '*) return 1;; *' --plan '*) printf '%s\\n' " + shlex.quote(plan) + ";; esac; }",
                'read_env_value() { printf "%s" "' + approved + '"; }', decision,
            ])
            result = subprocess.run(["bash", "-c", script], text=True, capture_output=True, timeout=5)
            self.assertEqual(result.returncode, expected, result.stderr)

    def test_container_probes_do_not_consume_remaining_ssh_script(self):
        release_probe = next(
            line for line in DEPLOY_SCRIPT.splitlines()
            if line.strip().startswith("running_release=")
        )
        sync_probe = next(
            line for line in DEPLOY_SCRIPT.splitlines()
            if "if docker compose exec -T bridge-worker" in line
        )
        for probe in (release_probe, sync_probe + "\n:; fi"):
            with self.subTest(probe=probe):
                # SSH feeds bash via stdin. Simulate Docker attaching to that
                # stream: later deployment checks must still be executed.
                script = "\n".join([
                    "set -euo pipefail",
                    "docker() { cat >/dev/null; }",
                    probe.replace("\\$", "$"),
                    "echo subsequent-health-check-executed",
                ])
                result = subprocess.run(
                    ["bash"], input=script, text=True, capture_output=True,
                    timeout=5,
                )
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(
                    result.stdout.strip(), "subsequent-health-check-executed"
                )

    def test_recovery_preserves_failure_and_stops_deployment(self):
        # Use the actual trap after the outer SSH heredoc removes its escapes.
        trap = re.search(r"^    trap .* ERR$", DEPLOY_SCRIPT, re.MULTILINE)
        self.assertIsNotNone(trap)
        remote_trap = trap.group(0).replace("\\$", "$")
        for failing_step in ("false", "bash -c 'exit 23'"):
            with self.subTest(failing_step=failing_step):
                result = subprocess.run(
                    ["bash", "-c", "\n".join([
                        "set -euo pipefail",
                        "restore_runtime_on_error() { trap - ERR; set +e; echo recovered; }",
                        remote_trap,
                        failing_step,
                        "echo incorrectly-continued",
                    ])],
                    text=True, capture_output=True, timeout=5,
                )
                self.assertEqual(result.returncode, 1 if failing_step == "false" else 23)
                self.assertEqual(result.stdout.strip(), "recovered")

    def validate_contract(self, payload, enabled="false"):
        section = DEPLOY_SCRIPT.split('printf \'%s\' "\\$office_manager_preflight_body"', 1)[1]
        validator = section.split("python3 -c '\n", 1)[1].split("\n'", 1)[0]
        return subprocess.run(
            [sys.executable, "-c", validator, enabled],
            input=json.dumps(payload), text=True, capture_output=True, timeout=5,
        )

    def valid_contract(self, enabled=False):
        return {
            "status": "ok",
            "contract": "office-manager-v1",
            "credential_scope": "strict_roo",
            "claim_generation_supported": True,
            "claim_generation_required": True,
            "claim_channel_required": True,
            "allowed_channel_id": OFFICE_MANAGER_TEST_CHANNEL_ID,
            "timezone": "Australia/Melbourne",
            "enabled": enabled,
        }

    def test_accepts_current_enabled_and_disabled_contract(self):
        for enabled in (True, False):
            with self.subTest(enabled=enabled):
                result = self.validate_contract(self.valid_contract(enabled), str(enabled).lower())
                self.assertEqual(result.returncode, 0, result.stderr)

    def test_rejects_incompatible_or_less_restricted_contract(self):
        for key, value in (
            ("claim_channel_required", False),
            ("allowed_channel_id", "C_WRONG_CHANNEL"),
            ("credential_scope", "internal"),
            ("claim_generation_required", False),
            ("enabled", True),
            ("timezone", "UTC"),
            ("unexpected", "field"),
        ):
            with self.subTest(key=key):
                payload = self.valid_contract()
                payload[key] = value
                self.assertNotEqual(self.validate_contract(payload).returncode, 0)
        for key in self.valid_contract():
            with self.subTest(missing=key):
                payload = self.valid_contract()
                del payload[key]
                self.assertNotEqual(self.validate_contract(payload).returncode, 0)


class WebsiteDeploymentConfigTests(unittest.TestCase):
    def test_plan_canonicalization_retains_every_operation_and_rejects_ambiguous_output(self):
        plan = "Planned operations:\ncontent_factory.0042_website_connection_lifecycle\n    Create model WebsiteConnection"
        for prefix in ("", "Initializing Firebase with project ID: synthetic\nDEBUG: ESAFETY VIEWS MODULE LOADED\n"):
            self.assertEqual(canonical_migration_plan(prefix + plan + "\n"), plan)
        self.assertNotEqual(canonical_migration_plan(plan + "\n    Unreviewed operation"), plan)
        for invalid in ("", "Import failed", plan + "\nPlanned operations:\nUnexpected"):
            with self.assertRaises(ValueError):
                canonical_migration_plan(invalid)

    def test_paused_default_and_each_rollout_mode_are_explicit(self):
        validate_website({})
        validate_website({"WEBSITE_CONNECTION_WRITE_MODE": "disabled"})
        validate_website({"WEBSITE_CONNECTION_WRITE_MODE": "enabled"})
        validate_website({"WEBSITE_CONNECTION_WRITE_MODE": "canary", "WEBSITE_CONNECTION_CANARY_DOMAINS": "talathrive.com,site.example.test"})
        for mode in ("", "true", "DISABLED", "typo", "disabled\nenabled"):
            with self.subTest(mode=mode), self.assertRaises(ValueError):
                validate_website({"WEBSITE_CONNECTION_WRITE_MODE": mode})
        with self.assertRaises(ValueError):
            validate_website({"WEBSITE_CONNECTION_WRITE_MODE": "canary"})

    def test_canary_domains_reject_noncanonical_or_ambiguous_authority(self):
        for domains in ("https://site.test", "*.site.test", "site.test.", "SITE.TEST", "site.test, site2.test", "site.test,site.test", "site.test,", "127.0.0.1", "localhost", "-bad.site.test", "site.test\nother.test", "site.test/path"):
            with self.subTest(domains=domains), self.assertRaises(ValueError):
                validate_single("WEBSITE_CONNECTION_CANARY_DOMAINS", domains)
        for value in ("a" * 63, "A" * 64, "a" * 65, "a" * 64 + "\n", "not-a-hash"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                validate_single("APPROVED_MIGRATION_PLAN_SHA256", value)

    def test_workflow_and_remote_writer_install_approved_values_before_runtime_mutation(self):
        root = Path(__file__).resolve().parents[1]
        workflow = (root / ".github/workflows/deploy.yml").read_text()
        for key, default in (("WEBSITE_CONNECTION_WRITE_MODE", "disabled"), ("WEBSITE_CONNECTION_CANARY_DOMAINS", ""), ("APPROVED_MIGRATION_PLAN_SHA256", "")):
            self.assertIn(key + ": ${{ vars." + key + " || '" + default + "' }}", workflow)
            self.assertIn('install_remote_env_value ' + key + ' "$' + key + '"', DEPLOY_SCRIPT)
            self.assertLess(DEPLOY_SCRIPT.index('install_remote_env_value ' + key), DEPLOY_SCRIPT.index('    migrations_pending=1'))
        self.assertLess(DEPLOY_SCRIPT.index('python3 scripts/validate_website_deploy_config.py'), DEPLOY_SCRIPT.index('rsync -avz'))

    def test_stdin_writer_clears_old_approval_and_canary_without_touching_other_environment(self):
        root = Path(__file__).resolve().parents[1]
        values = {
            "WEBSITE_CONNECTION_WRITE_MODE": ("disabled", "canary", "enabled", "", "true", "disabled\nenabled"),
            "WEBSITE_CONNECTION_CANARY_DOMAINS": ("site.example.test", "", "https://site.test", "*.site.test"),
            "APPROVED_MIGRATION_PLAN_SHA256": ("a" * 64, "", "A" * 64, "a" * 63, "a" * 64 + "\nnot-a-hash"),
        }
        with tempfile.TemporaryDirectory() as directory:
            sandbox = Path(directory)
            (sandbox / "scripts").mkdir()
            for name in ("upsert_env_value_from_stdin.sh", "validate_website_deploy_config.py"):
                shutil.copy(root / "scripts" / name, sandbox / "scripts")
            for key, candidates in values.items():
                for value in candidates:
                    with self.subTest(key=key, value=value):
                        env_file = sandbox / ".env"
                        original = "KEEP_ME=yes\n" + key + "=old-value\n"
                        env_file.write_text(original)
                        result = subprocess.run(["bash", "scripts/upsert_env_value_from_stdin.sh", key], cwd=sandbox,
                                                input=value, text=True, capture_output=True, timeout=5)
                        try:
                            validate_single(key, value)
                            valid = True
                        except ValueError:
                            valid = False
                        self.assertEqual(result.returncode == 0, valid, result.stderr)
                        self.assertEqual(env_file.read_text(), "KEEP_ME=yes\n" + key + "=" + value + "\n" if valid else original)
                        self.assertEqual(result.stdout, "")
                        if valid:
                            self.assertEqual(env_file.stat().st_mode & 0o777, 0o600)
