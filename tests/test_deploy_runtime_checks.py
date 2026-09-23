"""Execute deployed validators without credentials, services, or a database."""

import hashlib
import json
from pathlib import Path
import re
import subprocess
import sys
import unittest

from roo.office_manager_policy import OFFICE_MANAGER_TEST_CHANNEL_ID


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
        plan = "Apply app.0001_example"
        approved_hash = hashlib.sha256(plan.encode()).hexdigest()
        for check_result, plan_result, approval, expected_exit, expected_pending, expected_calls in (
            (0, 0, "", 0, "0", "check,"),
            (1, 0, "", 1, None, "check,plan,"),
            (1, 0, "wrong-plan-hash", 1, None, "check,plan,"),
            (1, 0, approved_hash, 0, "1", "check,plan,"),
            (1, 2, approved_hash, 2, None, "check,plan,"),
        ):
            with self.subTest(check_result=check_result, approval=approval, plan_result=plan_result):
                script = "\n".join([
                    "set -euo pipefail",
                    'calls_file="$(mktemp)"',
                    "compose_run_web() {",
                    "  case \" $* \" in",
                    "    *' --check '*) printf check, >> \"$calls_file\"; return " + str(check_result) + ";;",
                    "    *' --plan '*) printf plan, >> \"$calls_file\"; echo " + plan + "; return " + str(plan_result) + ";;",
                    "  esac",
                    "}",
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
