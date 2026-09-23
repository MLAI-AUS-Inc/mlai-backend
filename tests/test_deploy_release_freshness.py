"""No-service checks for the stale-main release guard."""

from pathlib import Path
import subprocess
import unittest


DEPLOY = (Path(__file__).resolve().parents[1] / "deploy.sh").read_text()
CURRENT = "a" * 40
STALE = "b" * 40


def _function(name: str) -> str:
    start = DEPLOY.index(f"{name}() {{")
    end = DEPLOY.index("\n}", start) + len("\n}")
    return DEPLOY[start:end].replace("\\$", "$")


class DeployReleaseFreshnessTests(unittest.TestCase):
    def test_runner_rejects_stale_or_unknown_main_before_any_host_mutation(self):
        guard = _function("verify_current_main_release")
        for latest, git_status, expected_status in (
            (CURRENT, 0, 0),
            (STALE, 0, 1),
            ("", 1, 1),
        ):
            with self.subTest(latest=latest, git_status=git_status):
                script = "\n".join(
                    [
                        "set -euo pipefail",
                        f"APP_RELEASE={CURRENT}",
                        f"git() {{ printf '%s\\trefs/heads/main\\n' '{latest}'; return {git_status}; }}",
                        guard,
                        "verify_current_main_release",
                        "echo host-mutation-allowed",
                    ]
                )
                result = subprocess.run(
                    ["bash", "-c", script], capture_output=True, text=True, timeout=5
                )
                self.assertEqual(result.returncode, expected_status, result.stderr)
                self.assertEqual(
                    "host-mutation-allowed" in result.stdout,
                    expected_status == 0,
                )

    def test_host_recheck_rejects_stale_main(self):
        start = DEPLOY.index("    verify_current_main_release_on_host() {")
        end = DEPLOY.index("\n    }", start) + len("\n    }")
        guard = DEPLOY[start:end].replace("\\$", "$" )
        for latest, git_status, expected_status in (
            (CURRENT, 0, 0),
            (STALE, 0, 1),
            ("", 1, 1),
        ):
            with self.subTest(latest=latest, git_status=git_status):
                script = "\n".join(
                    [
                        "set -euo pipefail",
                        f"APP_RELEASE={CURRENT}",
                        f"git() {{ printf '%s\\trefs/heads/main\\n' '{latest}'; return {git_status}; }}",
                        guard,
                        "verify_current_main_release_on_host",
                        "echo runtime-mutation-allowed",
                    ]
                )
                result = subprocess.run(
                    ["bash", "-c", script], capture_output=True, text=True, timeout=5
                )
                self.assertEqual(result.returncode, expected_status, result.stderr)
                self.assertEqual(
                    "runtime-mutation-allowed" in result.stdout,
                    expected_status == 0,
                )

    def test_rechecks_precede_sync_build_migration_and_code_replacement(self):
        self.assertLess(
            DEPLOY.index("verify_current_main_release\necho"),
            DEPLOY.index("rsync -avz"),
        )
        self.assertLess(
            DEPLOY.index("    verify_current_main_release_on_host\n"),
            DEPLOY.index("    upsert_env_value() {"),
        )
        self.assertIn(
            'verify_current_main_release_on_host\n    docker compose build', DEPLOY
        )
        self.assertIn(
            'verify_current_main_release_on_host\n        echo "⏸️ Pausing', DEPLOY
        )
        self.assertIn(
            'verify_current_main_release_on_host\n    fi\n    new_runtime_replacement_started=1',
            DEPLOY,
        )

    def test_code_release_rechecks_before_candidate_and_both_route_flips(self):
        handoff = DEPLOY.split('    echo "🌐 Starting runtime services:', 1)[1]
        self.assertIn(
            'verify_current_main_release_on_host\n        if ! systemctl',
            handoff,
        )
        self.assertIn(
            'wait_for_origin_web_health 8002 "$APP_RELEASE"\n'
            '        verify_current_main_release_on_host',
            handoff,
        )
        self.assertIn(
            'wait_for_origin_web_health 8001 "$APP_RELEASE"\n'
            '        verify_current_main_release_on_host',
            handoff,
        )
        self.assertLess(
            handoff.index('wait_for_nginx_workers_to_drain "\\$old_nginx_workers"'),
            handoff.index('new_runtime_replacement_started=1'),
        )

    def test_first_proxy_adoption_rejects_pending_migration_before_pause(self):
        marker = '    if [ "\\$migrations_pending" = "1" ] && [ "\\$web_proxy_preexisting" != "1" ]; then'
        self.assertLess(DEPLOY.index(marker), DEPLOY.index('echo "⏸️ Pausing all runtime writers before DB migrations'))
        guard = (marker + DEPLOY.split(marker, 1)[1].split("\n    fi", 1)[0] + "\n    fi").replace("\\$", "$")
        for pending, proxy_present, expected in ((1, 0, 1), (0, 0, 0), (1, 1, 0)):
            with self.subTest(pending=pending, proxy_present=proxy_present):
                result = subprocess.run(
                    ["bash", "-c", f"set -euo pipefail\nmigrations_pending={pending}\nweb_proxy_preexisting={proxy_present}\n{guard}\necho allowed"],
                    capture_output=True,
                    text=True,
                    timeout=5,
                )
                self.assertEqual(result.returncode, expected, result.stderr)
                self.assertEqual("allowed" in result.stdout, expected == 0)


if __name__ == "__main__":
    unittest.main()
