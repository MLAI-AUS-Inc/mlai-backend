"""CRM release switches reach the host only when a release sets them."""

import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

from scripts.validate_crm_deploy_config import FLAG_KEYS, KEYS, validate, validate_single


ROOT = Path(__file__).resolve().parents[1]
SID = "MG" + "0123456789abcdef" * 2


class CrmDeployConfigTests(unittest.TestCase):
    def test_every_setting_is_optional(self):
        validate({})
        validate({key: "" for key in KEYS})

    def test_valid_settings_pass(self):
        validate({**{key: "true" for key in FLAG_KEYS},
                  "LISTMONK_BASE_URL": "http://listmonk:9000",
                  "LISTMONK_API_USER": "mlai-backend",
                  "TWILIO_MESSAGING_SERVICE_SID": SID})
        validate({key: "false" for key in FLAG_KEYS})

    def test_malformed_settings_fail_without_echoing_them(self):
        for key, value in (("COMMUNITY_CHAT_CRM_ENABLED", "yes-sensitive"),
                           ("CRM_SMS_ENABLED", "TRUE"),
                           ("LISTMONK_BASE_URL", "http://user:sensitive@listmonk:9000"),
                           ("LISTMONK_BASE_URL", "http://listmonk:9000/api?token=sensitive"),
                           ("LISTMONK_BASE_URL", "ftp://listmonk"),
                           ("LISTMONK_API_USER", "sensitive user"),
                           ("TWILIO_MESSAGING_SERVICE_SID", "AC" + SID[2:]),
                           ("CRM_SENDING_ENABLED", "true\nsensitive")):
            with self.subTest(key=key), self.assertRaises(ValueError) as caught:
                validate({key: value})
            self.assertNotIn("sensitive", str(caught.exception))
        with self.assertRaises(ValueError):
            validate_single("LISTMONK_API_TOKEN", "x" * 40)

    def test_host_upsert_accepts_crm_settings_and_keeps_the_rest(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "scripts").mkdir()
            for name in ("upsert_env_value_from_stdin.sh", "validate_crm_deploy_config.py"):
                shutil.copy2(ROOT / "scripts" / name, root / "scripts" / name)
            env_file = root / ".env"
            env_file.write_text("REDIS_URL=redis://synthetic\nCRM_SMS_ENABLED=false\n")

            def upsert(key, value):
                return subprocess.run(["bash", str(root / "scripts/upsert_env_value_from_stdin.sh"), key],
                    input=value, text=True, capture_output=True, env={"PATH": os.defpath}, check=False)

            for key in FLAG_KEYS:
                self.assertEqual(upsert(key, "true").returncode, 0, key)
            self.assertEqual(upsert("LISTMONK_BASE_URL", "http://listmonk:9000").returncode, 0)
            self.assertEqual(upsert("TWILIO_MESSAGING_SERVICE_SID", SID).returncode, 0)
            before = env_file.read_bytes()
            result = upsert("CRM_SENDING_ENABLED", "sensitive")
            self.assertNotEqual(result.returncode, 0)
            self.assertNotIn("sensitive", result.stdout + result.stderr)
            self.assertEqual(env_file.read_bytes(), before)
            text = env_file.read_text()
            self.assertIn("REDIS_URL=redis://synthetic\n", text)
            self.assertIn("CRM_SMS_ENABLED=true\n", text)
            self.assertEqual(text.count("CRM_SMS_ENABLED="), 1)
            self.assertIn("LISTMONK_BASE_URL=http://listmonk:9000\n", text)

    def test_listmonk_token_is_an_allowed_secret(self):
        secret_script = (ROOT / "scripts/upsert_env_secret_from_stdin.sh").read_text()
        self.assertIn("LISTMONK_API_TOKEN) ;;", secret_script)

    def test_release_validates_early_and_installs_only_set_values(self):
        deploy = (ROOT / "deploy.sh").read_text()
        workflow = (ROOT / ".github/workflows/deploy.yml").read_text()
        self.assertLess(deploy.index("python3 scripts/validate_crm_deploy_config.py"),
                        deploy.index("rsync -avz"))
        settings_line = next(line for line in deploy.splitlines() if line.startswith("CRM_DEPLOY_SETTINGS="))
        self.assertEqual(settings_line.split("=", 1)[1].strip('"').split(), list(KEYS))
        for key in KEYS:
            self.assertIn(f"{key}: ${{{{ vars.{key} || '' }}}}", workflow)
        self.assertIn("LISTMONK_API_TOKEN: ${{ secrets.LISTMONK_API_TOKEN || '' }}", workflow)
        install = deploy[deploy.index("rsync -avz"):]
        self.assertIn('    if [ -n "${!crm_setting}" ]; then\n'
                      '        install_remote_env_value "$crm_setting" "${!crm_setting}"', install)
        self.assertIn('if [ -n "$LISTMONK_API_TOKEN" ]; then\n'
                      '    install_remote_env_secret LISTMONK_API_TOKEN "$LISTMONK_API_TOKEN"', install)


if __name__ == "__main__":
    unittest.main()
