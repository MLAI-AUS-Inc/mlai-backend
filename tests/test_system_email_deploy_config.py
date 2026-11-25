"""The deploy env validator accepts every registered system email key."""
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
REGISTERED = re.findall(r'key="([a-z0-9_]+)"', (ROOT / "system_email" / "registry.py").read_text())


class SystemEmailResendMessagesDeployConfigTests(unittest.TestCase):
    def run_writer(self, value):
        with tempfile.TemporaryDirectory() as directory:
            sandbox = Path(directory)
            (sandbox / "scripts").mkdir()
            shutil.copy(ROOT / "scripts" / "upsert_env_value_from_stdin.sh", sandbox / "scripts")
            (sandbox / ".env").write_text("KEEP_ME=yes\n")
            return subprocess.run(["bash", "scripts/upsert_env_value_from_stdin.sh", "SYSTEM_EMAIL_RESEND_MESSAGES"],
                cwd=sandbox, input=value, text=True, capture_output=True, timeout=5)

    def test_registry_is_parsed(self):
        self.assertIn("daily_research_topics", REGISTERED)
        self.assertGreaterEqual(len(REGISTERED), 10)

    def test_every_registered_key_and_all_are_accepted(self):
        for value in (*REGISTERED, "all", ",".join(REGISTERED), "magic_link, daily_research_topics", ""):
            with self.subTest(value=value):
                result = self.run_writer(value)
                self.assertEqual(result.returncode, 0, result.stderr)

    def test_unknown_keys_are_rejected(self):
        for value in ("not_a_message", "magic_link,not_a_message"):
            with self.subTest(value=value):
                result = self.run_writer(value)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("must list registered message keys", result.stderr)


if __name__ == "__main__":
    unittest.main()
