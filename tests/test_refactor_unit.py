import ast
import hashlib
from pathlib import Path
import re
import shlex
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from core.scheduling import run_runners
from core.user_compat import user_team_profile
from scripts.check_test_assignment import classify, selected_labels

ROOT = Path(__file__).resolve().parents[1]


class SchedulerResultTests(unittest.TestCase):
    def test_returned_failure_and_exception_do_not_skip_later_runners(self):
        later = Mock(return_value={"status": "ok"})
        results, failures = run_runners([
            ("bad_config", lambda: {"status": "failed", "reason": "missing_config"}),
            ("exception", Mock(side_effect=ValueError("unavailable"))),
            ("later", later),
        ])
        self.assertEqual(failures, ["bad_config", "exception"])
        self.assertEqual(results["later"], {"status": "ok"})
        later.assert_called_once_with()

    def test_nested_failure_halt_and_invalid_result_are_failures(self):
        for result in (
            {"status": "ok", "queued_run": {"status": "failed"}},
            {"status": "halted"}, {}, None,
        ):
            with self.subTest(result=result):
                _, failures = run_runners([("runner", lambda: result)])
                self.assertEqual(failures, ["runner"])

    def test_skipped_and_queued_are_successful_ticks(self):
        for status in ("skipped", "queued", "completed", "ok"):
            self.assertEqual(run_runners([("runner", lambda: {"status": status})])[1], [])


class RuntimeInventoryTests(unittest.TestCase):
    def test_deployment_failures_exit_after_the_appropriate_recovery(self):
        # Execute the actual error handler and complete schema decision block.
        # Docker and management commands are stubs; no database is constructed.
        deploy = (ROOT / "deploy.sh").read_text()
        start = deploy.index("    runtime_restore_attempted=0")
        end = deploy.index('    echo "🧬 Re-auditing Office Manager provenance', start)
        boundary = deploy[start:end].replace("\\$", "$")
        plan = "Apply app.0001_example"
        approved_hash = hashlib.sha256(plan.encode()).hexdigest()
        script = r'''set -euo pipefail
source scripts/runtime-services.sh
rollback_manifest="$1"
previous_runtime_container_ids=()
previous_scheduler_container_id=""
previous_app_release=previous
web_proxy_preexisting=1
web_candidate_started=0
migration_applied=0
upsert_env_value() { :; }
verify_scheduler_recovery_tick() { :; }
verify_current_main_release_on_host() { :; }
docker() {
    printf 'docker:%s\n' "$*"
    if [ "$1 $2" = 'compose stop' ]; then return "$STOP_STATUS"; fi
}
compose_run_web() {
    case " $* " in
        *' --plan '*) printf '%s\n' 'Apply app.0001_example'; return 0 ;;
        *' --check '*)
            if [ "$PENDING" = 0 ] || [ "$migration_applied" = 1 ]; then
                return 0
            fi
            return 1 ;;
    esac
    printf 'migrate:%s\n' "$*"
    migration_applied=1
    return "$MIGRATE_STATUS"
}
'''
        # The deployed host has GNU sha256sum. Keep this shell harness portable
        # to developer macOS without changing the deployment command.
        script += (
            'sha256sum() { ' + shlex.quote(sys.executable)
            + ' -c "import hashlib,sys; print(hashlib.sha256(sys.stdin.buffer.read()).hexdigest())"; }\n'
            + f"read_env_value() {{ printf '%s' '{approved_hash}'; }}\n"
        )
        for pending, stop_status, migrate_status in (
            (1, 0, 0), (1, 42, 0), (1, 0, 43), (0, 42, 0),
        ):
            with self.subTest(pending=pending, stop=stop_status, migrate=migrate_status), tempfile.TemporaryDirectory() as directory:
                manifest = Path(directory) / "rollback-manifest"
                manifest.write_text("web|old-image|image-ref|rollback-tag\n")
                result = subprocess.run(
                    ["bash", "-c",
                     f"PENDING={pending}\nSTOP_STATUS={stop_status}\nMIGRATE_STATUS={migrate_status}\n"
                     + script + boundary + "\nprintf 'boundary-complete\\n'\n",
                     "deployment-boundary-test", str(manifest)],
                    cwd=ROOT, text=True, capture_output=True, timeout=5,
                )
                expected_status = (stop_status or migrate_status) if pending else 0
                self.assertEqual(result.returncode, expected_status, result.stderr)
                if not pending:
                    self.assertNotIn("docker:", result.stdout)
                    self.assertNotIn("migrate:", result.stdout)
                else:
                    self.assertIn("docker:compose stop web scheduler jobs-worker", result.stdout)
                    self.assertIn("committee-remuneration web-candidate", result.stdout)
                    if stop_status:
                        self.assertIn("docker:compose up -d --no-deps --force-recreate web", result.stdout)
                        self.assertNotIn("migrate:", result.stdout)
                    elif migrate_status:
                        self.assertIn("keeping all runtime writers safely disabled", result.stdout)
                        self.assertNotIn("docker:compose up", result.stdout)
                    else:
                        self.assertIn("migrate:python manage.py migrate --noinput", result.stdout)
                self.assertEqual("boundary-complete" in result.stdout, expected_status == 0)

    def test_manifest_covers_every_compose_application_writer(self):
        output = subprocess.check_output(
            ["bash", "-c", 'source scripts/runtime-services.sh; printf "%s\\n" "${all_runtime_writer_services[@]}"'],
            cwd=ROOT, text=True,
        )
        writers = output.splitlines()
        self.assertEqual(len(writers), len(set(writers)))
        service_section = (ROOT / "docker-compose.yml").read_text().split("services:\n", 1)[1]
        service_section = re.split(r"^[^\s#]", service_section, maxsplit=1, flags=re.M)[0]
        services = set(re.findall(r"^  ([a-z][a-z0-9-]*):$", service_section, re.M))
        # The separate database is infrastructure, not an application writer.
        self.assertEqual(set(writers), services - {"db"})

    def test_password_and_jobs_workers_are_required(self):
        output = subprocess.check_output(
            ["bash", "-c", 'source scripts/runtime-services.sh; printf "%s\\n" "${runtime_services[@]}"'],
            cwd=ROOT, text=True,
        )
        self.assertIn("password-email-worker", output.splitlines())
        self.assertIn("jobs-worker", output.splitlines())
        self.assertNotIn("committee-remuneration", output.splitlines())
        self.assertNotIn("web-candidate", output.splitlines())

    def test_committee_worker_is_opt_in_and_disabled_service_is_stopped(self):
        deploy = (ROOT / "deploy.sh").read_text()
        start = deploy.index("    committee_remuneration_enabled=0")
        end = deploy.index("    docker network inspect", start)
        selection = deploy[start:end].replace("\\$", "$")
        start = deploy.index('    if [ "\\$committee_remuneration_enabled" != "1" ]; then')
        end = deploy.index('    echo "🔁 Verifying the running web container picked up APP_RELEASE', start)
        cleanup = deploy[start:end].replace("\\$", "$")
        for configured, enabled in (("true", True), ("1", True), ("false", False), ("", False)):
            with self.subTest(configured=configured):
                result = subprocess.run(
                    ["bash", "-c", "\n".join([
                        "set -eu",
                        "source scripts/runtime-services.sh",
                        "read_env_value() { printf '%s' " + shlex.quote(configured) + "; }",
                        'docker() { printf "docker:%s\\n" "$*"; }',
                        selection,
                        'printf "runtime:%s\\n" "${runtime_services[*]}"',
                        cleanup,
                    ])],
                    cwd=ROOT, text=True, capture_output=True, timeout=5,
                )
                self.assertEqual(result.returncode, 0, result.stderr)
                runtime = next(line for line in result.stdout.splitlines() if line.startswith("runtime:"))
                self.assertEqual("committee-remuneration" in runtime, enabled)
                self.assertEqual("docker:compose stop committee-remuneration" in result.stdout, not enabled)
                self.assertEqual("docker:compose rm -f committee-remuneration" in result.stdout, not enabled)
                self.assertNotIn("web-candidate", result.stdout)

    def test_community_routes_have_no_duplicate_literal_paths_or_names(self):
        tree = ast.parse((ROOT / "community_chat/urls.py").read_text())
        routes, names = [], []
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "path":
                if node.args and isinstance(node.args[0], ast.Constant):
                    routes.append(node.args[0].value)
                names.extend(k.value.value for k in node.keywords if k.arg == "name" and isinstance(k.value, ast.Constant))
        self.assertEqual(len(routes), len(set(routes)))
        self.assertEqual(len(names), len(set(names)))


class TeamProjectionTests(unittest.TestCase):
    def test_hospital_members_are_read_once_and_not_counted_again(self):
        members = Mock()
        members.values.return_value = [
            {"first_name": "A", "last_name": "One", "avatar_url": None},
            {"first_name": "B", "last_name": "Two", "avatar_url": "avatar"},
        ]
        team = SimpleNamespace(team_name="Team", team_id="team-1", avatar_url="", members=members)
        hospital = Mock()
        hospital.filter.return_value.first.return_value = team
        esafety = Mock()
        esafety.first.return_value = None
        generic = Mock()
        payload = user_team_profile(SimpleNamespace(hospital_teams=hospital, esafety_teams=esafety, generic_hackathon_teams=generic))
        self.assertEqual(payload["team"]["member_count"], 2)
        self.assertTrue(payload["team"]["is_valid_team_size"])
        self.assertEqual(payload["team"]["members"][0]["full_name"], "A One")
        members.values.assert_called_once()
        members.count.assert_not_called()
        generic.exists.assert_not_called()

    def test_missing_event_relations_are_supported(self):
        self.assertEqual(user_team_profile(SimpleNamespace()), {
            "team": None, "hospital_team": None, "esafety_team": None, "has_team": False,
        })


class TestAssignmentTests(unittest.TestCase):
    def test_multiline_commands_include_both_test_runners(self):
        workflow = "python manage.py test app.tests \\\n other.tests --verbosity 2\npython scripts/test_without_database.py pure.tests\n"
        self.assertEqual(selected_labels(workflow), {"app.tests", "other.tests", "pure.tests"})

    def test_partial_selections_are_not_reported_as_complete_modules(self):
        complete, partial, absent = classify(
            {"app.tests.one", "app.tests.two", "other.tests"},
            {"app.tests.one", "app.tests.two.OnlyOneClass"},
        )
        self.assertEqual(complete, {"app.tests.one"})
        self.assertEqual(partial, {"app.tests.two"})
        self.assertEqual(absent, {"other.tests"})
