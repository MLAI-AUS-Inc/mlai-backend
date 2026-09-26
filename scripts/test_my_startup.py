"""Run isolated My startup tests without database creation or migrations."""

import os
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ.update(
    {
        "DJANGO_SETTINGS_MODULE": "mlai.settings",
        "DEBUG": "True",
        "APP_ENV": "test",
        "SECRET_KEY": "my-startup-isolated-tests-only",
        "DATABASE_URL": "sqlite:///:memory:",
        "REDIS_URL": "",
    }
)
with patch("dotenv.load_dotenv", return_value=False):
    import django

    django.setup()

# A test must mock its domain/storage collaborators explicitly. Accidentally
# opening a connection fails rather than touching an unapproved database.
with patch(
    "django.db.backends.base.base.BaseDatabaseWrapper.ensure_connection",
    side_effect=AssertionError("No database access in this test suite"),
):
    suite = unittest.defaultTestLoader.loadTestsFromName(
        "founder_tools.my_startup.tests"
    )
    result = unittest.TextTestRunner(verbosity=2).run(suite)
raise SystemExit(not result.wasSuccessful())
