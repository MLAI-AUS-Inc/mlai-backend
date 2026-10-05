from pathlib import Path
import importlib.util
import os
import re
import shlex
import subprocess
import tempfile
from types import SimpleNamespace
from unittest.mock import patch

from django.test import SimpleTestCase


ROOT = Path(__file__).resolve().parents[1]


class RuntimeHardeningConfigTests(SimpleTestCase):
    def _web_command(self, compose_filename):
        lines = (ROOT / compose_filename).read_text().splitlines()
        web_index = lines.index("  web:")
        for line in lines[web_index + 1 :]:
            if line.startswith("  ") and not line.startswith("    "):
                break
            stripped = line.strip()
            if stripped.startswith("command:"):
                return stripped.removeprefix("command:").strip()
        self.fail(f"Missing web command in {compose_filename}")

    def _web_healthcheck_test(self, compose_filename):
        lines = (ROOT / compose_filename).read_text().splitlines()
        web_index = lines.index("  web:")
        healthcheck_index = None
        for index, line in enumerate(lines[web_index + 1 :], start=web_index + 1):
            if line.startswith("  ") and not line.startswith("    "):
                break
            if line == "    healthcheck:":
                healthcheck_index = index
                break

        if healthcheck_index is None:
            self.fail(f"Missing web healthcheck in {compose_filename}")

        for line in lines[healthcheck_index + 1 :]:
            if line.startswith("    ") and not line.startswith("      "):
                break
            stripped = line.strip()
            if stripped.startswith("test:"):
                return stripped.removeprefix("test:").strip()
        self.fail(f"Missing web healthcheck test in {compose_filename}")

    def test_production_gunicorn_config_warms_routes_after_fork(self):
        compose = (ROOT / "docker-compose.yml").read_text()
        start_script = (ROOT / "scripts" / "start-web.sh").read_text()
        gunicorn_config = (ROOT / "scripts" / "gunicorn.conf.py").read_text()
        deploy = (ROOT / "deploy.sh").read_text()

        self.assertIn("--worker-class", start_script)
        self.assertIn("sync", start_script)
        self.assertIn("--keep-alive", start_script)
        self.assertIn("--timeout", start_script)
        self.assertIn("--graceful-timeout", start_script)
        self.assertIn("--max-requests", start_script)
        self.assertNotIn("--preload", start_script)
        self.assertIn("--config /app/scripts/gunicorn.conf.py", start_script)
        self.assertIn("def post_worker_init(worker):", gunicorn_config)
        self.assertIn("get_resolver().url_patterns", gunicorn_config)
        self.assertNotIn("def when_ready", gunicorn_config)
        self.assertNotIn("--threads", start_script)

        self.assertIn("${GUNICORN_WORKERS:-3}", start_script)
        self.assertIn("${GUNICORN_TIMEOUT:-90}", start_script)
        self.assertIn('upsert_env_value GUNICORN_WORKERS "4"', deploy)
        self.assertIn('upsert_env_value GUNICORN_TIMEOUT "90"', deploy)
        self.assertIn("${GUNICORN_GRACEFUL_TIMEOUT:-30}", start_script)
        self.assertIn("${GUNICORN_MAX_REQUESTS:-300}", start_script)
        self.assertIn('RUN_MIGRATIONS_ON_START: "0"', compose)

        spec = importlib.util.spec_from_file_location(
            "mlai_gunicorn_config", ROOT / "scripts" / "gunicorn.conf.py"
        )
        config_module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(config_module)
        accessed = []

        class Resolver:
            @property
            def url_patterns(self):
                accessed.append(True)
                return []

        with patch("django.urls.get_resolver", return_value=Resolver()):
            config_module.post_worker_init(SimpleNamespace(log=SimpleNamespace(info=lambda *args: None)))
        self.assertEqual(accessed, [True])

    def test_web_runtime_does_not_mutate_schema_or_collect_static(self):
        production_compose = (ROOT / "docker-compose.yml").read_text()
        local_compose = (ROOT / "docker-compose.local.yml").read_text()
        start_script = (ROOT / "scripts" / "start-web.sh").read_text()

        self.assertEqual(
            self._web_command("docker-compose.yml"), "sh /app/scripts/start-web.sh"
        )
        self.assertEqual(
            self._web_command("docker-compose.local.yml"),
            "sh /app/scripts/start-web.sh",
        )
        self.assertIn('RUN_MIGRATIONS_ON_START: "0"', production_compose)
        self.assertIn('RUN_MIGRATIONS_ON_START: "1"', local_compose)
        self.assertIn("${RUN_MIGRATIONS_ON_START:-0}", start_script)
        self.assertNotIn("collectstatic", start_script)

    def test_image_build_collects_production_static_manifest(self):
        dockerfile = (ROOT / "Dockerfile").read_text()
        settings = (ROOT / "mlai" / "settings.py").read_text()

        self.assertIn(
            "RUN DJANGO_STATIC_BUILD=True python manage.py collectstatic --noinput",
            dockerfile,
        )
        self.assertIn("/app/staticfiles/staticfiles.json", dockerfile)
        self.assertIn("admin/css/base.css", dockerfile)
        self.assertIn("rest_framework/css/bootstrap.min.css", dockerfile)
        self.assertIn("STATIC_URL = '/static/'", settings)
        self.assertIn(
            "if not DEBUG or _env_is_true('DJANGO_STATIC_BUILD', False):",
            settings,
        )

    def test_deploy_verifies_django_admin_page_and_stylesheet(self):
        deploy = (ROOT / "deploy.sh").read_text()

        self.assertIn(
            "https://api.mlai.au/admin/login/?next=%2Fadmin%2F",
            deploy,
        )
        self.assertIn(
            'staticfiles_storage.url("admin/css/base.css")',
            deploy,
        )
        self.assertIn(
            'curl -fsS -o /dev/null "https://api.mlai.au\\$admin_css_path"',
            deploy,
        )

    def test_healthcheck_uses_proxy_tls_header_and_closes_connection(self):
        healthcheck_test = self._web_healthcheck_test("docker-compose.yml")

        self.assertIn("'http://127.0.0.1:8000/healthz/live'", healthcheck_test)
        self.assertIn("'Connection':'close'", healthcheck_test)
        self.assertIn("'X-Forwarded-Proto':'https'", healthcheck_test)
        self.assertIn("body=resp.read()", healthcheck_test)
        self.assertIn("resp.close()", healthcheck_test)

    def test_backend_socket_smoke_uses_proxy_tls_header(self):
        script = (ROOT / "ops" / "backend-socket-smoke.sh").read_text()

        self.assertIn('URL="${URL:-http://127.0.0.1/healthz/ready}"', script)
        self.assertIn('-H "Connection: close" -H "X-Forwarded-Proto: https"', script)

    def test_watchdog_has_restart_rate_limit(self):
        script = (ROOT / "ops" / "docker-health-watchdog.sh").read_text()
        service = (ROOT / "ops" / "docker-health-watchdog.service.example").read_text()

        self.assertIn("WATCHDOG_MAX_RESTARTS", script)
        self.assertIn("WATCHDOG_RESTART_WINDOW_SECONDS", script)
        self.assertIn("rate_limited=true", script)
        self.assertIn("StartLimitIntervalSec=600", service)
        self.assertIn("StartLimitBurst=3", service)

    def test_watchdog_does_not_restart_writers_while_migration_is_paused(self):
        script = ROOT / "ops" / "docker-health-watchdog.sh"
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            marker = root / "writers-paused"
            marker.write_text("reviewed-release\n")
            calls = root / "compose-calls"
            compose = root / "compose"
            compose.write_text(
                "#!/bin/sh\n"
                f"printf '%s\\n' \"$*\" >> {shlex.quote(str(calls))}\n"
            )
            compose.chmod(0o755)
            environment = {
                "WATCHDOG_SERVICE_NAME": "web",
                "WATCHDOG_INTERVAL_SECONDS": "0.05",
                "WATCHDOG_WRITER_PAUSE_SENTINEL": str(marker),
                "DOCKER_COMPOSE_CMD": str(compose),
            }
            with self.assertRaises(subprocess.TimeoutExpired) as timed_out:
                subprocess.run(
                    ["bash", str(script)],
                    env=environment,
                    capture_output=True,
                    text=True,
                    timeout=0.75,
                    check=False,
                )
            self.assertIn("action=paused_for_migration", timed_out.exception.stdout.decode())
            self.assertFalse(calls.exists(), "watchdog must not inspect or start a paused writer")

    def test_watchdog_rechecks_pause_immediately_before_start(self):
        script = ROOT / "ops" / "docker-health-watchdog.sh"
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            marker = root / "writers-paused"
            calls = root / "compose-actions"
            compose = root / "compose"
            compose.write_text(
                "#!/bin/sh\n"
                'if [ "$1" = ps ]; then '
                f"printf 'reviewed-release\\n' > {shlex.quote(str(marker))}; else "
                f"printf '%s\\n' \"$*\" >> {shlex.quote(str(calls))}; fi\n"
            )
            compose.chmod(0o755)
            environment = {
                **os.environ,
                "WATCHDOG_SERVICE_NAME": "web",
                "WATCHDOG_INTERVAL_SECONDS": "0.05",
                "WATCHDOG_WRITER_PAUSE_SENTINEL": str(marker),
                "DOCKER_COMPOSE_CMD": str(compose),
            }
            with self.assertRaises(subprocess.TimeoutExpired) as timed_out:
                subprocess.run(
                    ["bash", str(script)],
                    env=environment,
                    capture_output=True,
                    text=True,
                    timeout=0.75,
                    check=False,
                )
            self.assertTrue(marker.exists(), f"stdout={timed_out.exception.stdout!r} stderr={timed_out.exception.stderr!r}")
            self.assertFalse(calls.exists(), "watchdog must recheck before starting web")

    def test_migration_pause_refuses_failed_or_unverified_writer_stop(self):
        deploy = (ROOT / "deploy.sh").read_text()
        start = deploy.index("    pause_runtime_writers_for_migration() {")
        end = deploy.index("\n    }\n    restore_runtime_on_error()", start) + len("\n    }")
        pause_function = deploy[start:end].replace("\\$", "$")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for scenario, allowed in (
                ("stopped", True),
                ("stop_failed", False),
                ("ps_failed", False),
                ("inspect_failed", False),
                ("still_running", False),
                ("already_paused", False),
                ("watchdog_failed", False),
            ):
                with self.subTest(scenario=scenario):
                    marker = root / f"pause-{scenario}"
                    if scenario == "already_paused":
                        marker.write_text("previous-release\n")
                    calls = root / f"calls-{scenario}"
                    probe = "\n".join(
                        [
                            "set -euo pipefail",
                            f"scenario={shlex.quote(scenario)}",
                            f"writer_pause_sentinel={shlex.quote(str(marker))}",
                            f"calls={shlex.quote(str(calls))}",
                            "APP_RELEASE=reviewed-release",
                            "runtime_pause_started=0",
                            "all_runtime_writer_services=(web scheduler)",
                            'pause_host_writer_watchdogs() { [ "$scenario" != watchdog_failed ]; }',
                            "restore_host_writer_watchdogs() { :; }",
                            "docker() {",
                            '  printf "%s\\n" "$*" >> "$calls"',
                            '  if [ "$1" = compose ] && [ "$2" = stop ]; then',
                            '    [ "$scenario" != stop_failed ]; return $?',
                            "  fi",
                            '  if [ "$1" = compose ] && [ "$2" = ps ]; then',
                            '    [ "$scenario" != ps_failed ] || return 1',
                            '    [ "$5" != web ] || printf "%s\\n" web-container',
                            "    return 0",
                            "  fi",
                            '  if [ "$1" = inspect ]; then',
                            '    [ "$scenario" != inspect_failed ] || return 1',
                            '    [ "$scenario" != still_running ] || { echo true; return 0; }',
                            "    echo false; return 0",
                            "  fi",
                            "  return 1",
                            "}",
                            pause_function,
                            "pause_runtime_writers_for_migration",
                            "echo migration-started",
                        ]
                    )
                    result = subprocess.run(
                        ["bash", "-c", probe], capture_output=True, text=True, timeout=5
                    )
                    self.assertEqual(result.returncode == 0, allowed, result.stderr)
                    self.assertEqual("migration-started" in result.stdout, allowed)
                    self.assertEqual(marker.exists(), scenario != "watchdog_failed")
                    if marker.exists():
                        self.assertEqual(
                            marker.read_text(),
                            "previous-release\n" if scenario == "already_paused" else "reviewed-release\n",
                        )
                    if scenario in ("already_paused", "watchdog_failed"):
                        self.assertFalse(calls.exists())
                    else:
                        self.assertIn("compose stop web scheduler", calls.read_text())

    def test_legacy_host_watchdog_is_stopped_before_migration(self):
        deploy = (ROOT / "deploy.sh").read_text()
        start = deploy.index("    pause_host_writer_watchdogs() {")
        end = deploy.index("    pause_runtime_writers_for_migration() {", start)
        host_functions = deploy[start:end].replace("\\$", "$")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for scenario, allowed in (
                ("stopped", True),
                ("none", True),
                ("inventory_failed", False),
                ("stop_failed", False),
                ("still_active", False),
                ("lingering_process", False),
            ):
                with self.subTest(scenario=scenario):
                    state = root / f"state-{scenario}"
                    state.write_text("active\n")
                    script = "\n".join(
                        [
                            "set -euo pipefail",
                            f"scenario={shlex.quote(scenario)}",
                            f"state={shlex.quote(str(state))}",
                            "writer_watchdog_units_to_restore=()",
                            "systemctl() {",
                            '  if [ "$1" = list-units ]; then',
                            '    [ "$scenario" != inventory_failed ] || return 1',
                            '    if [ "$scenario" = none ]; then echo unrelated.service; else printf "%s\\n" legacy-watchdog.service unrelated.service; fi',
                            "  elif [ \"$1\" = show ]; then",
                            '    if [ "$2" = --property=ExecStart ]; then',
                            '      if [ "$4" = legacy-watchdog.service ]; then echo "/srv/mlai-backend/ops/docker-health-watchdog.sh"; else echo /usr/bin/other; fi',
                            '    else cat "$state"; fi',
                            '  elif [ "$1" = stop ]; then',
                            '    [ "$scenario" != stop_failed ] || return 1',
                            '    [ "$scenario" = still_active ] || echo inactive > "$state"',
                            '  elif [ "$1" = start ]; then echo active > "$state"',
                            "  else return 1; fi",
                            "}",
                            'pgrep() { if [ "$scenario" = lingering_process ]; then echo "999 /srv/mlai-backend/ops/docker-health-watchdog.sh"; else return 1; fi; }',
                            host_functions,
                            "pause_host_writer_watchdogs",
                            "echo migration-started",
                            "restore_host_writer_watchdogs",
                            "echo restored",
                        ]
                    )
                    result = subprocess.run(
                        ["bash", "-c", script], capture_output=True, text=True, timeout=5
                    )
                    self.assertEqual(result.returncode == 0, allowed, result.stderr)
                    self.assertEqual("migration-started" in result.stdout, allowed)
                    self.assertEqual(state.read_text(), "active\n" if allowed or scenario in ("inventory_failed", "stop_failed", "still_active") else "inactive\n")
                    if allowed:
                        self.assertIn("restored", result.stdout)

    def test_writer_watchdog_pause_clears_only_after_safe_rollback(self):
        deploy = (ROOT / "deploy.sh").read_text()
        start = deploy.index("    restore_runtime_on_error() {")
        end = deploy.index("\n    # A code-only release", start)
        recovery_function = deploy[start:end].replace("\\$", "$")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            manifest = root / "rollback-manifest"
            manifest.write_text("web|old-image|old-ref|rollback-web\n")
            for recreation_succeeds in (True, False):
                with self.subTest(recreation_succeeds=recreation_succeeds):
                    marker = root / "writers-paused"
                    marker.write_text("reviewed-release\n")
                    script = "\n".join(
                        [
                            "set -euo pipefail",
                            f"rollback_manifest={shlex.quote(str(manifest))}",
                            f"writer_pause_sentinel={shlex.quote(str(marker))}",
                            f"recreation_succeeds={1 if recreation_succeeds else 0}",
                            "runtime_restore_attempted=0",
                            "migrations_pending=1",
                            "runtime_pause_started=1",
                            "new_runtime_replacement_started=0",
                            "migration_started=0",
                            "schema_transition_completed=0",
                            "web_candidate_started=0",
                            "web_only_rollback=0",
                            "previous_app_release=old-release",
                            "previous_runtime_container_ids=()",
                            "previous_scheduler_container_id=",
                            "upsert_env_value() { :; }",
                            "restore_host_writer_watchdogs() { :; }",
                            'docker() { [ "$2" != compose ] || :; if [ "$1" = compose ] && [ "$2" = up ]; then [ "$recreation_succeeds" = 1 ]; else return 0; fi; }',
                            recovery_function,
                            "restore_runtime_on_error",
                        ]
                    )
                    result = subprocess.run(
                        ["bash", "-c", script], capture_output=True, text=True, timeout=5
                    )
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertEqual(marker.exists(), not recreation_succeeds)
        # The success path removes the marker only after release health checks,
        # while an incomplete schema transition intentionally retains it.
        self.assertLess(deploy.index("Expected video upload session preflight"), deploy.index('rm -f "\\$writer_pause_sentinel"\n    fi\n    trap - ERR EXIT'))
        self.assertIn("Keep the watchdog pause until an operator repairs the schema.", deploy)

    def test_coworking_repair_migration_has_duplicate_preflight(self):
        migration = (
            ROOT
            / "roo"
            / "migrations"
            / "0018_ensure_coworking_unique_active_booking.py"
        ).read_text()

        self.assertIn("HAVING COUNT(*) > 1", migration)
        self.assertIn(
            "CREATE UNIQUE INDEX IF NOT EXISTS unique_active_booking_per_user_date",
            migration,
        )
        self.assertIn("WHERE status = 'booked'", migration)

    def test_deploy_pauses_web_until_constraint_is_verified(self):
        deploy = (ROOT / "deploy.sh").read_text()

        self.assertIn(
            "source scripts/runtime-services.sh",
            deploy,
        )
        self.assertIn(
            'for service in "\\${all_runtime_writer_services[@]}"; do', deploy
        )
        self.assertIn('running_writer_services+=("\\$service")', deploy)
        self.assertIn(
            'docker compose stop "\\${all_runtime_writer_services[@]}"', deploy
        )

        # The coworking booking guard is still verified on every deploy, but it
        # now lives in deploy_postmigrate alongside the other post-migration
        # checks instead of in its own `manage.py shell` container.
        self.assertIn("compose_run_web python manage.py deploy_postmigrate", deploy)
        postmigrate = (
            ROOT / "core" / "management" / "commands" / "deploy_postmigrate.py"
        ).read_text()
        self.assertIn("unique_active_booking_per_user_date", postmigrate)
        self.assertIn("to_regclass", postmigrate)
        self.assertLess(
            deploy.index('docker compose stop "\\${all_runtime_writer_services[@]}"'),
            deploy.index("compose_run_web python manage.py deploy_postmigrate"),
        )
        self.assertIn(
            "source scripts/runtime-services.sh",
            deploy,
        )
        self.assertIn(
            'if [ "\\$internal_status" != "401" ] || [ "\\$missing_status" != "401" ]; then',
            deploy,
        )
        self.assertIn(
            "community-email-worker:", (ROOT / "docker-compose.yml").read_text()
        )
        self.assertIn(
            "run_email_code_worker", (ROOT / "docker-compose.yml").read_text()
        )
        self.assertIn(
            'runtime_services+=("\\${bridge_runtime_services[@]}")',
            deploy,
        )
        self.assertIn(
            'COMMUNITY_BRIDGE_PRODUCTION_ENABLED="${COMMUNITY_BRIDGE_PRODUCTION_ENABLED:-false}"',
            deploy,
        )
        self.assertIn(
            'if [ "\\$community_bridge_production_enabled" = "true" ] \\',
            deploy,
        )
        self.assertIn(
            'upsert_env_value COMMUNITY_BRIDGE_PRODUCTION_ENABLED "\\$community_bridge_production_enabled"',
            deploy,
        )
        self.assertIn("&& env_has_value SLACK_BRIDGE_BOT_TOKEN \\", deploy)
        self.assertIn("env_has_value DISCORD_BRIDGE_BOT_TOKEN \\", deploy)
        self.assertIn("env_has_value BUZZ_BRIDGE_ADAPTER_URL \\", deploy)
        self.assertIn("env_has_value BUZZ_BRIDGE_ADAPTER_TOKEN \\", deploy)
        self.assertIn("env_has_value BUZZ_BRIDGE_CALLBACK_SECRET;", deploy)
        self.assertIn(
            "python3 scripts/validate_community_bridge_adapter_url.py "
            '"$BUZZ_BRIDGE_ADAPTER_URL"',
            deploy,
        )
        self.assertIn(
            "compose_run_web python manage.py upsert_community_bridge_channel",
            deploy,
        )
        self.assertIn('--slack-workspace-id "$SLACK_BRIDGE_WORKSPACE_ID"', deploy)
        self.assertIn('--slack-channel-id "$SLACK_BRIDGE_CHANNEL_ID"', deploy)
        self.assertIn("--destination-platform buzz", deploy)
        self.assertIn(
            '--destination-channel-id "$BUZZ_BRIDGE_DESTINATION_CHANNEL_ID"',
            deploy,
        )
        self.assertIn(
            "docker compose stop bridge-worker bridge-reconciler bridge-retention || true",
            deploy,
        )
        self.assertIn(
            "docker compose rm -f bridge-worker bridge-reconciler bridge-retention || true",
            deploy,
        )
        # deploy_postmigrate verifies migration readiness as its first step, so
        # the bridge mapping still cannot be written against a half-migrated
        # database.
        self.assertLess(
            deploy.index("compose_run_web python manage.py deploy_postmigrate"),
            deploy.index(
                "compose_run_web python manage.py upsert_community_bridge_channel"
            ),
        )
        self.assertLess(
            deploy.index('docker compose stop "\\${all_runtime_writer_services[@]}"'),
            deploy.index(
                'docker compose up -d --no-deps --force-recreate "\\${runtime_services[@]}"'
            ),
        )

    def test_deploy_restores_only_before_forward_only_schema_advancement(self):
        deploy = (ROOT / "deploy.sh").read_text()

        self.assertIn("rollback_manifest=\\$(mktemp)", deploy)
        self.assertIn("docker inspect --format '{{.Image}}'", deploy)
        self.assertIn('docker image tag "\\$image_id" "\\$rollback_tag"', deploy)
        self.assertIn("migration_started=0", deploy)
        self.assertIn("migration_started=1", deploy)
        self.assertIn("restoring the last known-good runtime images", deploy)
        self.assertIn(
            "keeping all runtime writers safely disabled",
            deploy,
        )
        self.assertIn(
            'docker compose stop "\\${all_runtime_writer_services[@]}"', deploy
        )
        self.assertIn('docker image tag "\\$image_id" "\\$image_ref"', deploy)
        self.assertIn(
            'docker compose up -d --no-deps --force-recreate "\\${restored_services[@]}"',
            deploy,
        )
        failure_trap = re.search(r"^    trap .* ERR$", deploy, re.MULTILINE)
        self.assertIsNotNone(failure_trap)
        self.assertLess(
            failure_trap.start(),
            deploy.index("Verifying external Vibe Raising video upload CORS preflight"),
        )
        self.assertGreater(
            deploy.rindex("trap - ERR"),
            deploy.index("Verifying external Vibe Raising video upload CORS preflight"),
        )
        trapped_deploy = deploy[
            failure_trap.start() : deploy.rindex(
                "trap - ERR"
            )
        ]
        self.assertNotIn(
            "exit 1",
            trapped_deploy,
            "explicit exit bypasses Bash ERR traps and therefore recovery",
        )

    def test_post_migration_failure_executes_safe_disabled_recovery(self):
        deploy = (ROOT / "deploy.sh").read_text()
        function_start = deploy.index("    restore_runtime_on_error() {")
        function_end = deploy.index(
            "\n    }\n\n    # A code-only release",
            function_start,
        ) + len("\n    }")
        recovery_function = deploy[function_start:function_end].replace("\\$", "$")
        probe = (
            recovery_function
            + r"""
runtime_restore_attempted=0
migration_started=1
schema_transition_completed=0
runtime_pause_started=1
source scripts/runtime-services.sh
rollback_manifest="$1"
docker() {
    printf 'docker:%s\n' "$*"
}
trap 'deployment_status=$?; restore_runtime_on_error; exit "$deployment_status"' ERR
false
printf 'unexpected-continuation\n'
"""
        )

        with tempfile.TemporaryDirectory() as directory:
            manifest = Path(directory) / "rollback-manifest"
            manifest.touch()
            completed = subprocess.run(
                ["bash", "-c", probe, "recovery-test", str(manifest)],
                cwd=ROOT,
                check=False,
                capture_output=True,
                text=True,
            )

        self.assertEqual(completed.returncode, 1, completed.stderr)
        self.assertIn("keeping all runtime writers safely disabled", completed.stdout)
        self.assertIn("docker:compose stop web scheduler jobs-worker", completed.stdout)
        self.assertIn("committee-remuneration", completed.stdout)
        self.assertNotIn("unexpected-continuation", completed.stdout)

    def test_code_only_health_failure_rolls_back_after_replacement(self):
        deploy = (ROOT / "deploy.sh").read_text()
        function_start = deploy.index("    restore_runtime_on_error() {")
        function_end = deploy.index(
            "\n    }\n\n    # A code-only release", function_start
        ) + len("\n    }")
        recovery_function = deploy[function_start:function_end].replace("\\$", "$")
        probe = recovery_function + r"""
runtime_restore_attempted=0
runtime_pause_started=0
new_runtime_replacement_started=1
migration_started=0
previous_scheduler_container_id=""
previous_runtime_container_ids=()
runtime_services=(web scheduler)
rollback_manifest="$(mktemp)"
docker_log="$(mktemp)"
printf 'web|old-image-id|mlai-backend-web|rollback-tag\n' > "$rollback_manifest"
upsert_env_value() { :; }
docker() { printf '%s\n' "$*" >> "$docker_log"; }
restore_runtime_on_error
cat "$docker_log"
rm -f "$rollback_manifest" "$docker_log"
"""
        completed = subprocess.run(
            ["bash", "-c", probe], check=False, capture_output=True, text=True
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertIn("image tag old-image-id mlai-backend-web", completed.stdout)
        self.assertIn("compose up -d --no-deps --force-recreate web", completed.stdout)
        self.assertNotIn("compose up -d --no-deps --force-recreate web scheduler", completed.stdout)

    def test_bridge_deploy_validation_requires_explicit_production_activation(self):
        workflow = (ROOT / ".github" / "workflows" / "deploy.yml").read_text()

        self.assertIn(
            "COMMUNITY_BRIDGE_PRODUCTION_ENABLED: "
            "${{ vars.COMMUNITY_BRIDGE_PRODUCTION_ENABLED || 'false' }}",
            workflow,
        )
        self.assertIn(
            'if [ "$COMMUNITY_BRIDGE_PRODUCTION_ENABLED" = "true" ]; then',
            workflow,
        )
        self.assertIn(
            "Slack and Buzz bridge repository settings must be fully configured "
            "when the production bridge is enabled",
            workflow,
        )
        self.assertIn(
            "Community bridge production activation is disabled; staged bridge "
            "settings will not be installed.",
            workflow,
        )

    def test_bridge_reconciler_multiline_command_keeps_shell_continuations(self):
        continued_fragments = (
            "python manage.py reconcile_recent_community_bridge_slack",
            "--lookback-seconds",
            "--max-roots-per-channel",
            "--maximum-history-messages",
        )
        for filename in ("docker-compose.yml", "docker-compose.local.yml"):
            lines = (ROOT / filename).read_text().splitlines()
            for fragment in continued_fragments:
                matching = [line for line in lines if fragment in line]
                self.assertEqual(len(matching), 1, f"{fragment} in {filename}")
                self.assertTrue(
                    matching[0].rstrip().endswith("\\"),
                    f"{fragment} must continue in {filename}",
                )

    def test_meeting_room_feature_flag_is_deployment_managed_and_smoke_tested(self):
        workflow = (ROOT / ".github" / "workflows" / "deploy.yml").read_text()
        deploy = (ROOT / "deploy.sh").read_text()

        self.assertIn(
            "MEETING_ROOM_BOOKING_ENABLED: "
            "${{ vars.MEETING_ROOM_BOOKING_ENABLED || 'false' }}",
            workflow,
        )
        self.assertIn(
            'MEETING_ROOM_BOOKING_ENABLED="${MEETING_ROOM_BOOKING_ENABLED:-false}"',
            deploy,
        )
        self.assertIn(
            "upsert_env_value MEETING_ROOM_BOOKING_ENABLED "
            '"\\$meeting_room_booking_enabled"',
            deploy,
        )
        self.assertIn(
            "https://api.mlai.au/api/v1/points/meeting-rooms/rooms/",
            deploy,
        )
        self.assertIn('expected = {"small-meeting-room", "big-meeting-room"}', deploy)
        self.assertLess(
            deploy.index("upsert_env_value MEETING_ROOM_BOOKING_ENABLED"),
            deploy.index("compose_run_web python manage.py migrate --noinput"),
        )

    def test_deploy_compose_run_does_not_consume_ssh_stdin(self):
        deploy = (ROOT / "deploy.sh").read_text()

        self.assertNotIn("docker compose run --rm", deploy)
        self.assertNotIn("docker compose run --no-TTY", deploy)
        self.assertEqual(deploy.count("docker compose run -T --rm --no-deps web"), 1)
        self.assertIn(
            'docker compose run -T --rm --no-deps web "\\$@" </dev/null', deploy
        )
        self.assertIn("compose_run_web python manage.py migrate --noinput", deploy)

    def test_office_manager_deploy_probes_exact_authenticated_contract(self):
        deploy = (ROOT / "deploy.sh").read_text()

        self.assertIn(
            "/api/v1/points/coworking/office-manager/preflight/",
            deploy,
        )
        self.assertIn('"credential_scope": "strict_roo"', deploy)
        self.assertIn(
            "Public Roo reported an inconsistent Office Manager backend contract",
            deploy,
        )
        self.assertIn("Live Office Manager preflight returned the wrong contract", deploy)
