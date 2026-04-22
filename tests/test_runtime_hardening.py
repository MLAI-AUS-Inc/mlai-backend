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
        dockerfile = (ROOT / "Dockerfile").read_text()

        for content in (compose, dockerfile):
            self.assertIn("--worker-class", content)
            self.assertIn("sync", content)
            self.assertIn("--keep-alive", content)
            self.assertIn("--timeout", content)
            self.assertIn("--graceful-timeout", content)
            self.assertIn("--max-requests", content)
            self.assertNotIn("--threads", content)

        self.assertIn("${GUNICORN_WORKERS:-3}", compose)
        self.assertIn("${GUNICORN_TIMEOUT:-30}", compose)
        self.assertIn("${GUNICORN_GRACEFUL_TIMEOUT:-30}", compose)
        self.assertIn("${GUNICORN_MAX_REQUESTS:-300}", compose)
        self.assertIn('"3"', dockerfile)
        self.assertIn('"30"', dockerfile)
        self.assertIn('"300"', dockerfile)

    def test_web_runtime_does_not_mutate_schema_or_collect_static(self):
        for compose_filename in ("docker-compose.yml", "docker-compose.local.yml"):
            command = self._web_command(compose_filename)
            self.assertNotIn("manage.py migrate", command)
            self.assertNotIn("collectstatic", command)
            self.assertTrue(command.startswith("gunicorn "))

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

        self.assertIn("docker compose stop web", deploy)
        self.assertIn("unique_active_booking_per_user_date is missing", deploy)
        self.assertLess(
            deploy.index("docker compose stop web"),
            deploy.index("docker compose up -d web scheduler bridge-worker"),
        )
