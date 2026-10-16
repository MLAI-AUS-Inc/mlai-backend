"""Execute the activation script only against synthetic files and a fake Docker CLI."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from django.test import SimpleTestCase
import yaml

ROOT = Path(__file__).resolve().parents[1]
SHA = "a" * 40
DOCKER = r'''
import json, os, sys
args = sys.argv[1:]
with open(os.environ["FAKE_DOCKER_TRACE"], "a") as stream:
    stream.write(json.dumps(args) + "\n")
if "config" in args:
    print(json.dumps({"services": {"web": {"environment": {"RUN_MIGRATIONS_ON_START": os.environ.get("FAKE_MIGRATIONS", "0")}}}}))
elif "exec" in args and "-c" in args:
    print(os.environ["FAKE_LIVE_RELEASE"])
elif "manage.py" in args:
    command = args[args.index("manage.py") + 1]
    if command == os.environ.get("FAKE_FAIL_COMMAND") and (os.environ.get("FAKE_FAIL_POSTCHECK") != "true" or "exec" in args):
        raise SystemExit(17)
elif "up" in args and os.environ.get("FAKE_FAIL_RESTART") == "true":
    raise SystemExit(18)
'''


class AdminBrainActivationTests(SimpleTestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name)
        self.project, self.ops, self.bin = (self.directory / name for name in ("project", "operations", "bin"))
        for directory in (self.project, self.ops, self.bin):
            directory.mkdir()
        self.env = self.project / ".env"
        self.original = b"PRIVATE_FIXTURE_VALUE=must-remain-private\r\nORG_MEMORY_PILOT_ALLOWLIST_KEY_VERSION=fixture-v1\nORG_MEMORY_QUERY_API_ENABLED=false\n"
        self.env.write_bytes(self.original)
        (self.ops / "pilot-approval.json").write_text(json.dumps({"synthetic": True}))
        (self.ops / "pilot-operators").write_text("stage@example.test\nactivate@example.test\n")
        self.trace = self.directory / "trace"
        docker = self.bin / "docker"
        docker.write_text(f"#!{sys.executable}\n" + DOCKER)
        docker.chmod(0o700)

    def run_activation(self, *, expected=SHA, **changes):
        environment = {"PATH": f"{self.bin}:{os.defpath}", "MLAI_BACKEND_PROJECT_DIR": str(self.project),
            "MLAI_BACKEND_OPERATIONS_DIR": str(self.ops), "FAKE_DOCKER_TRACE": str(self.trace),
            "FAKE_LIVE_RELEASE": SHA, **changes}
        return subprocess.run(["bash", str(ROOT / "scripts/activate_admin_brain.sh"), expected],
            text=True, capture_output=True, check=False, env=environment)

    def calls(self):
        return [json.loads(line) for line in self.trace.read_text().splitlines()] if self.trace.exists() else []

    def test_healthy_activation_runs_all_strict_gates_then_changes_only_the_query_switch(self):
        result = self.run_activation()
        self.assertEqual(result.returncode, 0, result.stderr)
        calls = self.calls()
        commands = [args[args.index("manage.py") + 1] for args in calls if "manage.py" in args]
        self.assertEqual(commands, ["stage_org_memory_pilot", "activate_org_memory_pilot",
            "check_org_memory_pilot_release_gate", "report_org_memory_pilot_deployment",
            "check_org_memory_pilot_access_matrix", "check_org_memory_pilot_release_gate"])
        self.assertEqual(self.env.read_bytes(), self.original.replace(b"QUERY_API_ENABLED=false", b"QUERY_API_ENABLED=true"))
        self.assertEqual(self.env.stat().st_mode & 0o777, 0o600)
        self.assertNotIn("must-remain-private", result.stdout + result.stderr)
        for args in calls:
            self.assertNotIn("migrate", args)
            self.assertFalse(any(str(part).startswith(("recover_org_memory", "request_org_memory", "schedule_org_memory")) for part in args))
            if "run" in args:
                self.assertEqual(args[args.index("--entrypoint") + 1], "python")
                self.assertIn("--no-deps", args)
                self.assertEqual(args[args.index("--pull") + 1], "never")
        stages = [args for args in calls if "stage_org_memory_pilot" in args]
        activation = [args for args in calls if "activate_org_memory_pilot" in args]
        self.assertNotEqual(stages[0][stages[0].index("--operator-email") + 1], activation[0][activation[0].index("--operator-email") + 1])

    def test_each_failed_governance_gate_keeps_retrieval_off_and_stops_activation(self):
        commands = ["stage_org_memory_pilot", "activate_org_memory_pilot", "check_org_memory_pilot_release_gate",
            "report_org_memory_pilot_deployment", "check_org_memory_pilot_access_matrix"]
        for command in commands:
            self.trace.unlink(missing_ok=True)
            with self.subTest(command=command):
                result = self.run_activation(FAKE_FAIL_COMMAND=command)
                self.assertEqual(result.returncode, 17, result.stderr)
                self.assertIn(b"ORG_MEMORY_QUERY_API_ENABLED=false\n", self.env.read_bytes())
                self.assertNotIn("activation verified", result.stdout)
                called = [args[args.index("manage.py") + 1] for args in self.calls() if "manage.py" in args]
                self.assertEqual(called, commands[:commands.index(command) + 1])
                self.assertTrue(any("up" in args for args in self.calls()))

    def test_failed_actual_runtime_check_re_disables_the_query_switch(self):
        result = self.run_activation(FAKE_FAIL_COMMAND="check_org_memory_pilot_release_gate", FAKE_FAIL_POSTCHECK="true")
        self.assertEqual(result.returncode, 17)
        self.assertEqual(self.env.read_bytes(), self.original)
        self.assertEqual(sum("up" in args for args in self.calls()), 2)

    def test_wrong_release_and_migration_enabled_config_make_no_changes(self):
        for changes in ({"expected": "b" * 40}, {"expected": "$(must-not-execute)"}, {"FAKE_MIGRATIONS": "1"}):
            self.trace.unlink(missing_ok=True)
            with self.subTest(changes=changes):
                self.assertNotEqual(self.run_activation(**changes).returncode, 0)
                self.assertEqual(self.env.read_bytes(), self.original)
                self.assertFalse(any("run" in args or "up" in args for args in self.calls()))

    def test_operators_must_remain_distinct_and_a_failed_restart_is_reported_truthfully(self):
        (self.ops / "pilot-operators").write_text("same@example.test\nsame@example.test\n")
        result = self.run_activation(FAKE_FAIL_RESTART="true")
        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn("activation verified", result.stdout)
        self.assertIn("could not confirm the disabled runtime", result.stderr)
        self.assertFalse(any("manage.py" in args for args in self.calls()))

    def test_workflow_is_manual_main_only_confirmed_and_serialized_with_production_deploys(self):
        workflow = yaml.load((ROOT / ".github/workflows/activate-admin-brain.yml").read_text(), Loader=yaml.BaseLoader)
        deploy = yaml.load((ROOT / ".github/workflows/deploy.yml").read_text(), Loader=yaml.BaseLoader)
        self.assertEqual(set(workflow["on"]), {"workflow_dispatch"})
        inputs = workflow["on"]["workflow_dispatch"]["inputs"]
        self.assertEqual(inputs["confirm_activation"]["default"], "false")
        condition = workflow["jobs"]["activate"]["if"]
        for gate in ("refs/heads/main", "inputs.confirm_activation", "vars.ORG_MEMORY_PRODUCTION_DEPLOY_ENABLED == 'true'"):
            self.assertIn(gate, condition)
        prefix = deploy["concurrency"]["group"].split("${{")[0]
        self.assertEqual(workflow["concurrency"]["group"], prefix + "refs/heads/main")
        self.assertEqual(workflow["concurrency"]["cancel-in-progress"], "false")
        self.assertEqual(workflow["permissions"], {"contents": "read"})
