"""Founder-journey Articles switches reach the host only when a release sets them."""

import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

from scripts.validate_articles_deploy_config import (
    COUNT_KEYS, FLAG_KEYS, KEYS, POSITIVE_KEYS, validate, validate_single,
)


ROOT = Path(__file__).resolve().parents[1]
WINDOWS = '[{"start": "2026-11-07T22:00:00Z", "end": "2026-11-08T03:00:00Z"}]'


class ArticlesDeployConfigTests(unittest.TestCase):
    def test_every_setting_is_optional(self):
        validate({})
        validate({key: "" for key in KEYS})

    def test_valid_settings_pass(self):
        validate({**{key: "true" for key in FLAG_KEYS},
                  **{key: "0" for key in COUNT_KEYS},
                  **{key: "600" for key in POSITIVE_KEYS},
                  "ARTICLES_FOUNDER_JOURNEY_ALERTS_SINCE": "2026-10-30",
                  "ARTICLES_WORKSHOP_WINDOWS": WINDOWS})
        validate({**{key: "false" for key in FLAG_KEYS},
                  **{key: "25" for key in COUNT_KEYS},
                  **{key: "1" for key in POSITIVE_KEYS},
                  "ARTICLES_WORKSHOP_WINDOWS": "[]"})
        validate({"ARTICLES_WORKSHOP_WINDOWS":
                  '[{"start":"2026-11-08T09:00+11:00","end":"2026-11-08T14:00+11:00"},'
                  '{"start":"2026-11-15T09:00:00+11:00","end":"2026-11-15T14:00:00+11:00"}]'})

    def test_malformed_settings_fail_without_echoing_them(self):
        for key, value in (("ARTICLES_FREE_FIRST_ARTICLE_ENABLED", "yes-sensitive"),
                           ("ARTICLES_FREE_SETUP_RESEARCH_ENABLED", "TRUE"),
                           ("ARTICLES_FOUNDER_JOURNEY_ALERTS_ENABLED", "1"),
                           ("ARTICLES_FREE_FIRST_ARTICLE_DAILY_CAP", "-1"),
                           ("ARTICLES_FREE_SETUP_AUTOFILL_DAILY_CAP", "05"),
                           ("ARTICLES_FREE_FIRST_ARTICLE_SPIKE_THRESHOLD", "2.5"),
                           ("CONTENT_FACTORY_INTERACTIVE_QUEUE_SLOTS", "sensitive"),
                           ("ARTICLES_FOUNDER_JOURNEY_ALERTS_MAX_PER_TICK", "0"),
                           ("AUTH_THROTTLE_IP_VERIFY_PER_10M", "0"),
                           ("AUTH_THROTTLE_IP_CODE_REQUESTS_PER_HOUR", "1_000"),
                           ("ARTICLES_FOUNDER_JOURNEY_ALERTS_SINCE", "2026-02-30"),
                           ("ARTICLES_FOUNDER_JOURNEY_ALERTS_SINCE", "20261030"),
                           ("ARTICLES_WORKSHOP_WINDOWS", "sensitive"),
                           ("ARTICLES_WORKSHOP_WINDOWS", '{"start": "2026-11-07T22:00:00Z"}'),
                           # A naive time is read as UTC by the scheduler.
                           ("ARTICLES_WORKSHOP_WINDOWS", '[{"start": "2026-11-08T09:00", "end": "2026-11-08T14:00"}]'),
                           ("ARTICLES_WORKSHOP_WINDOWS", '[{"start": "2026-11-08T03:00:00Z", "end": "2026-11-07T22:00:00Z"}]'),
                           ("ARTICLES_WORKSHOP_WINDOWS", '[{"start": "2026-11-07T22:00:00Z", "end": "2026-11-08T03:00:00Z", "sensitive": ""}]'),
                           ("ARTICLES_WORKSHOP_WINDOWS", '[{"start": "2026-11-07T22:00:00Z", "end": "2026-11-31T03:00:00Z"}]'),
                           ("ARTICLES_WORKSHOP_WINDOWS", '[{"start": "2026-11-07T22:00:00Z", "end": "$sensitive"}]'),
                           ("ARTICLES_WORKSHOP_WINDOWS", "[" + ",".join([WINDOWS[1:-1]] * 51) + "]"),
                           ("ARTICLES_FIRST_ARTICLE_GOAL_ENABLED", "true\nsensitive")):
            with self.subTest(key=key, value=value[:40]), self.assertRaises(ValueError) as caught:
                validate({key: value})
            self.assertNotIn("sensitive", str(caught.exception))
            self.assertNotIn("2026-", str(caught.exception))
        with self.assertRaises(ValueError):
            validate_single("CONTENT_FACTORY_OPS_SLACK_CHANNEL_ID", "C0123456789")

    def test_host_upsert_accepts_articles_settings_and_keeps_the_rest(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "scripts").mkdir()
            for name in ("upsert_env_value_from_stdin.sh", "validate_articles_deploy_config.py"):
                shutil.copy2(ROOT / "scripts" / name, root / "scripts" / name)
            env_file = root / ".env"
            env_file.write_text("REDIS_URL=redis://synthetic\nARTICLES_FREE_FIRST_ARTICLE_ENABLED=false\n")

            def upsert(key, value):
                return subprocess.run(["bash", str(root / "scripts/upsert_env_value_from_stdin.sh"), key],
                    input=value, text=True, capture_output=True, env={"PATH": os.defpath}, check=False)

            for key in KEYS:
                if key in FLAG_KEYS:
                    value = "true"
                elif key == "ARTICLES_FOUNDER_JOURNEY_ALERTS_SINCE":
                    value = "2026-10-30"
                elif key == "ARTICLES_WORKSHOP_WINDOWS":
                    value = WINDOWS
                else:
                    value = "7"
                result = upsert(key, value)
                self.assertEqual(result.returncode, 0, (key, result.stderr))
            before = env_file.read_bytes()
            for key, value in (("ARTICLES_FREE_FIRST_ARTICLE_ENABLED", "sensitive"),
                               ("AUTH_THROTTLE_IP_VERIFY_PER_10M", "0"),
                               ("ARTICLES_WORKSHOP_WINDOWS", '[{"start": "sensitive"}]')):
                result = upsert(key, value)
                self.assertNotEqual(result.returncode, 0, key)
                self.assertNotIn("sensitive", result.stdout + result.stderr)
            self.assertNotEqual(upsert("ARTICLES_FREE_SETUP_RESEARCH_ENABLED", "").returncode, 0)
            self.assertEqual(env_file.read_bytes(), before)
            text = env_file.read_text()
            self.assertIn("REDIS_URL=redis://synthetic\n", text)
            self.assertIn("ARTICLES_FREE_FIRST_ARTICLE_ENABLED=true\n", text)
            self.assertEqual(text.count("ARTICLES_FREE_FIRST_ARTICLE_ENABLED="), 1)
            self.assertIn("AUTH_THROTTLE_IP_DEVICE_AUTH_PER_10M=7\n", text)
            self.assertIn(f"ARTICLES_WORKSHOP_WINDOWS={WINDOWS}\n", text)

    def test_every_setting_is_read_by_the_backend(self):
        # A misspelt name would install a value nothing reads.
        settings_source = (ROOT / "mlai/settings.py").read_text()
        for key in KEYS:
            self.assertTrue(f"'{key}'" in settings_source or f'"{key}"' in settings_source, key)

    def test_release_validates_early_and_installs_only_set_values(self):
        deploy = (ROOT / "deploy.sh").read_text()
        workflow = (ROOT / ".github/workflows/deploy.yml").read_text()
        self.assertLess(deploy.index("python3 scripts/validate_articles_deploy_config.py"),
                        deploy.index("rsync -avz"))
        settings_line = next(line for line in deploy.splitlines() if line.startswith("ARTICLES_DEPLOY_SETTINGS="))
        self.assertEqual(settings_line.split("=", 1)[1].strip('"').split(), list(KEYS))
        for key in KEYS:
            self.assertIn(f"{key}: ${{{{ vars.{key} || '' }}}}", workflow)
            self.assertNotIn(f"secrets.{key}", workflow)
        install = deploy[deploy.index("rsync -avz"):]
        self.assertIn('    if [ -n "${!articles_setting}" ]; then\n'
                      '        install_remote_env_value "$articles_setting" "${!articles_setting}"', install)


if __name__ == "__main__":
    unittest.main()
