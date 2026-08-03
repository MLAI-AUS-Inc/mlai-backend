from pathlib import Path

from django.test import SimpleTestCase


ROOT = Path(__file__).resolve().parents[1]


class RuntimeHardeningConfigTests(SimpleTestCase):
    def _web_command(self, compose_filename):
        lines = (ROOT / compose_filename).read_text().splitlines()
        web_index = lines.index("  web:")
        for line in lines[web_index + 1:]:
            if line.startswith("  ") and not line.startswith("    "):
                break
            stripped = line.strip()
            if stripped.startswith("command:"):
                return stripped.removeprefix("command:").strip()
        self.fail(f"Missing web command in {compose_filename}")

    def test_production_gunicorn_config_uses_sync_workers_and_short_timeouts(self):
        compose = (ROOT / "docker-compose.yml").read_text()
        start_script = (ROOT / "scripts" / "start-web.sh").read_text()

        self.assertIn("--worker-class", start_script)
        self.assertIn("sync", start_script)
        self.assertIn("--keep-alive", start_script)
        self.assertIn("--timeout", start_script)
        self.assertIn("--graceful-timeout", start_script)
        self.assertIn("--max-requests", start_script)
        self.assertNotIn("--threads", start_script)

        self.assertIn("${GUNICORN_WORKERS:-3}", start_script)
        self.assertIn("${GUNICORN_TIMEOUT:-30}", start_script)
        self.assertIn("${GUNICORN_GRACEFUL_TIMEOUT:-30}", start_script)
        self.assertIn("${GUNICORN_MAX_REQUESTS:-300}", start_script)
        self.assertIn('RUN_MIGRATIONS_ON_START: "0"', compose)

    def test_web_runtime_does_not_mutate_schema_or_collect_static(self):
        production_compose = (ROOT / "docker-compose.yml").read_text()
        local_compose = (ROOT / "docker-compose.local.yml").read_text()
        start_script = (ROOT / "scripts" / "start-web.sh").read_text()

        self.assertEqual(self._web_command("docker-compose.yml"), "sh /app/scripts/start-web.sh")
        self.assertEqual(self._web_command("docker-compose.local.yml"), "sh /app/scripts/start-web.sh")
        self.assertIn('RUN_MIGRATIONS_ON_START: "0"', production_compose)
        self.assertIn('RUN_MIGRATIONS_ON_START: "1"', local_compose)
        self.assertIn('${RUN_MIGRATIONS_ON_START:-0}', start_script)
        self.assertNotIn("collectstatic", start_script)

    def test_healthcheck_closes_connection_after_reading_body(self):
        compose = (ROOT / "docker-compose.yml").read_text()

        self.assertIn("'Connection':'close'", compose)
        self.assertIn("body=resp.read()", compose)
        self.assertIn("resp.close()", compose)

    def test_watchdog_has_restart_rate_limit(self):
        script = (ROOT / "ops" / "docker-health-watchdog.sh").read_text()
        service = (ROOT / "ops" / "docker-health-watchdog.service.example").read_text()

        self.assertIn("WATCHDOG_MAX_RESTARTS", script)
        self.assertIn("WATCHDOG_RESTART_WINDOW_SECONDS", script)
        self.assertIn("rate_limited=true", script)
        self.assertIn("StartLimitIntervalSec=600", service)
        self.assertIn("StartLimitBurst=3", service)

    def test_coworking_repair_migration_has_duplicate_preflight(self):
        migration = (
            ROOT
            / "roo"
            / "migrations"
            / "0018_ensure_coworking_unique_active_booking.py"
        ).read_text()

        self.assertIn("HAVING COUNT(*) > 1", migration)
        self.assertIn("CREATE UNIQUE INDEX IF NOT EXISTS unique_active_booking_per_user_date", migration)
        self.assertIn("WHERE status = 'booked'", migration)

    def test_deploy_pauses_web_until_constraint_is_verified(self):
        deploy = (ROOT / "deploy.sh").read_text()

        self.assertIn(
            'paused_runtime_services=(web memory-worker memory-scheduler community-email-worker)',
            deploy,
        )
        self.assertIn('docker compose stop "\\${paused_runtime_services[@]}"', deploy)
        self.assertIn("unique_active_booking_per_user_date is missing", deploy)
        self.assertIn(
            'runtime_services=(web scheduler memory-worker memory-scheduler community-email-worker)',
            deploy,
        )
        self.assertIn("community-email-worker:", (ROOT / "docker-compose.yml").read_text())
        self.assertIn("run_email_code_worker", (ROOT / "docker-compose.yml").read_text())
        self.assertIn('runtime_services+=(bridge-worker bridge-retention)', deploy)
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
        self.assertIn('&& env_has_value SLACK_BRIDGE_BOT_TOKEN \\', deploy)
        self.assertIn('env_has_value DISCORD_BRIDGE_BOT_TOKEN \\', deploy)
        self.assertIn('env_has_value BUZZ_BRIDGE_ADAPTER_URL \\', deploy)
        self.assertIn('env_has_value BUZZ_BRIDGE_ADAPTER_TOKEN \\', deploy)
        self.assertIn('env_has_value BUZZ_BRIDGE_CALLBACK_SECRET;', deploy)
        self.assertIn(
            'python3 scripts/validate_community_bridge_adapter_url.py '
            '"$BUZZ_BRIDGE_ADAPTER_URL"',
            deploy,
        )
        self.assertIn(
            "compose_run_web python manage.py upsert_community_bridge_channel",
            deploy,
        )
        self.assertIn('--slack-workspace-id "$SLACK_BRIDGE_WORKSPACE_ID"', deploy)
        self.assertIn('--slack-channel-id "$SLACK_BRIDGE_CHANNEL_ID"', deploy)
        self.assertIn('--destination-platform buzz', deploy)
        self.assertIn(
            '--destination-channel-id "$BUZZ_BRIDGE_DESTINATION_CHANNEL_ID"',
            deploy,
        )
        self.assertIn('docker compose stop bridge-worker || true', deploy)
        self.assertIn('docker compose rm -f bridge-worker || true', deploy)
        self.assertLess(
            deploy.index("compose_run_web python manage.py migrate --check --noinput"),
            deploy.index("compose_run_web python manage.py upsert_community_bridge_channel"),
        )
        self.assertLess(
            deploy.index('docker compose stop "\\${paused_runtime_services[@]}"'),
            deploy.index('docker compose up -d --force-recreate "\\${runtime_services[@]}"'),
        )

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

    def test_deploy_compose_run_does_not_consume_ssh_stdin(self):
        deploy = (ROOT / "deploy.sh").read_text()

        self.assertNotIn("docker compose run --rm", deploy)
        self.assertNotIn("docker compose run --no-TTY", deploy)
        self.assertEqual(deploy.count("docker compose run -T --rm --no-deps web"), 1)
        self.assertIn('docker compose run -T --rm --no-deps web "\\$@" </dev/null', deploy)
        self.assertIn("compose_run_web python manage.py migrate --noinput", deploy)
