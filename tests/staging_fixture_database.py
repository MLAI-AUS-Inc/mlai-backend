"""Bind fixture tests to the actual disposable PostgreSQL test database."""
from django.db import connection
from django.test import override_settings


def bind_disposable_fixture_database(test):
    test.assertEqual(connection.vendor, "postgresql")
    name = str(connection.settings_dict["NAME"])
    test.assertTrue(name.startswith("test_"), "A disposable Django test database is required.")
    test.enterContext(override_settings(STAGING_FIXTURE_DATABASE=name))
