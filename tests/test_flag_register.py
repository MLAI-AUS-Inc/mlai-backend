"""F15: fixture tests for the feature-flag register checker. Stdlib only; no database."""
import contextlib
import importlib.util
import io
import json
from pathlib import Path
import tempfile
import textwrap
import unittest

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("check_flag_register", ROOT / "scripts/check_flag_register.py")
checker = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(checker)

PATTERN = r"^(?:(?:ARTICLES|MY_STARTUP|FOUNDER|FLEET)_[A-Z0-9_]*[A-Z0-9]|[A-Z][A-Z0-9_]*_ENABLED)$"
SETTINGS = '''
import os
ARTICLES_NEW_FLAG_ENABLED = os.getenv("ARTICLES_NEW_FLAG_ENABLED", "false").lower() == "true"
'''
READER = '''
from django.conf import settings
def on():
    return settings.ARTICLES_NEW_FLAG_ENABLED and getattr(settings, "FOUNDER_FALLBACK_ENABLED", False)
'''
COMPLETE = {"name": "ARTICLES_NEW_FLAG_ENABLED", "default": "false", "owner": "platform", "expiry": "2027-02-28"}
FALLBACK = {"name": "FOUNDER_FALLBACK_ENABLED", "default": "(unset)", "owner": "platform", "expiry": "2027-02-28"}


class FlagRegisterTests(unittest.TestCase):
    def repo(self, flags, *, not_flags=()):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        root = Path(directory.name)
        (root / "mlai").mkdir()
        (root / "mlai/settings.py").write_text(textwrap.dedent(SETTINGS))
        (root / "app").mkdir()
        (root / "app/logic.py").write_text(textwrap.dedent(READER))
        (root / "app/tests_logic.py").write_text('import os\nos.getenv("ARTICLES_TEST_ONLY_ENABLED")\n')
        register = {"scan": {"pattern": PATTERN, "roots": [""], "exclude": ["*/tests_*.py"],
                             "settings_modules": ["mlai/settings.py"]},
                    "not_flags": list(not_flags), "flags": flags}
        (root / "flag-register.json").write_text(json.dumps(register))
        return root

    def run_checker(self, root, *extra):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = checker.main(["--root", str(root), "--today", "2026-11-20", *extra])
        return code, out.getvalue()

    def test_a_flag_without_owner_and_expiry_fails(self):
        code, out = self.run_checker(self.repo([{"name": "ARTICLES_NEW_FLAG_ENABLED", "default": "false"}, FALLBACK]))
        self.assertEqual(code, 1)
        self.assertIn("ARTICLES_NEW_FLAG_ENABLED: no owner", out)
        self.assertIn("ARTICLES_NEW_FLAG_ENABLED: no expiry", out)

    def test_a_flag_read_in_code_but_unregistered_fails(self):
        code, out = self.run_checker(self.repo([COMPLETE]))
        self.assertEqual(code, 1)
        # getattr(settings, "X", fallback) is a read.
        self.assertIn("FOUNDER_FALLBACK_ENABLED: read in code (app/logic.py) but missing from the register", out)
        self.assertNotIn("ARTICLES_TEST_ONLY_ENABLED", out)

    def test_complete_register_passes(self):
        self.assertEqual(self.run_checker(self.repo([COMPLETE, FALLBACK]))[0], 0)

    def test_settings_default_must_match_and_permanent_needs_a_reason(self):
        code, out = self.run_checker(self.repo([{**COMPLETE, "default": "true"}, {**FALLBACK, "expiry": "permanent"}]))
        self.assertEqual(code, 1)
        self.assertIn('register default "true" but the code declares "false"', out)
        self.assertIn("FOUNDER_FALLBACK_ENABLED: a permanent flag needs a reason", out)

    def test_stale_rows_and_unexplained_not_flags_fail(self):
        gone = {**COMPLETE, "name": "ARTICLES_REMOVED_ENABLED"}
        code, out = self.run_checker(self.repo([COMPLETE, FALLBACK, gone], not_flags=[{"name": "FLEET_X_ENABLED"}]))
        self.assertEqual(code, 1)
        self.assertIn("ARTICLES_REMOVED_ENABLED: in the register but no longer read in code", out)
        self.assertIn("needs a name and a reason", out)

    def test_expired_flag_warns_only(self):
        code, out = self.run_checker(self.repo([{**COMPLETE, "expiry": "2026-01-31"}, FALLBACK]))
        self.assertEqual(code, 0)
        self.assertIn("::warning", out)

    def test_write_never_invents_an_owner_or_expiry(self):
        root = self.repo([COMPLETE])
        code, _ = self.run_checker(root, "--write")
        self.assertEqual(code, 1)
        added = [row for row in json.loads((root / "flag-register.json").read_text())["flags"]
                 if row["name"] == "FOUNDER_FALLBACK_ENABLED"]
        self.assertEqual(added, [{"name": "FOUNDER_FALLBACK_ENABLED", "default": "(unset)",
                                  "owner": "", "expiry": "", "reason": ""}])

    def test_committed_register_matches_the_code(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = checker.main(["--root", str(ROOT)])
        self.assertEqual(code, 0, out.getvalue())

    def test_ci_runs_the_checker(self):
        workflow = (ROOT / ".github/workflows/deploy.yml").read_text()
        self.assertIn("python scripts/check_flag_register.py", workflow)
        self.assertIn("tests.test_flag_register", workflow)


if __name__ == "__main__":
    unittest.main()
