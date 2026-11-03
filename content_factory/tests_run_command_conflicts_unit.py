"""Local closed commands keep their purpose without weakening CF failures."""
from django.test import SimpleTestCase, override_settings
from .website_contract import RunCommandConflict, RUN_COMMAND_CONFLICT_CODES, WebsiteAuthorityError
from .website_failure_scope import failure_scope


@override_settings(ARTICLES_FAILURE_TAXONOMY_POLICY_ENABLED=True,
    ARTICLES_FAILURE_TAXONOMY_ENROLLED_ORGANIZATIONS=("7",))
class RunCommandConflictTests(SimpleTestCase):
    def test_closed_backend_conflicts_keep_safe_local_keys_under_enrolled_taxonomy(self):
        with failure_scope(7):
            for code in RUN_COMMAND_CONFLICT_CODES:
                with self.subTest(code=code):
                    error = RunCommandConflict(code, "Review the current saved request.")
                    self.assertIsInstance(error, WebsiteAuthorityError)
                    self.assertEqual(error.status, 409)
                    self.assertEqual(error.as_dict(), {"allowed": False, "error": code, "code": code,
                        "detail": "Review the current saved request.", "retryable": False, "fieldErrors": []})
    def test_foreign_code_cannot_use_local_conflict_and_worker_membership_still_fails_closed(self):
        with failure_scope(7):
            with self.assertRaises(ValueError):
                RunCommandConflict("foreign-worker-code", "Unknown.")
            worker = WebsiteAuthorityError("foreign-worker-code", "Foreign text.").as_dict()
            self.assertEqual(worker["failure"]["code"], "UNREGISTERED_FAILURE_CODE")
            self.assertEqual(worker["failure"]["failure_class"], "unsupported")
            self.assertNotIn("Foreign text", str(worker["failure"]))
