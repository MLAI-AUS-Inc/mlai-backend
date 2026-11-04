"""Bind fixture tests to the actual disposable PostgreSQL test database."""
from django.db import connection
from django.conf import settings
from django.test import override_settings


def bind_disposable_fixture_database(test):
    from community_chat.tests.test_staging_password import PILOT
    for key, value in PILOT.items():
        test.assertEqual(getattr(settings, key), value, key)
    test.assertEqual(connection.vendor, "postgresql")
    name = str(connection.settings_dict["NAME"])
    test.assertTrue(name.startswith("test_"), "A disposable Django test database is required.")
    # A nested UserSettingsHolder has its own SETTINGS_MODULE class attribute.
    # Keep the selected isolated profile while binding the test database.
    test.enterContext(override_settings(SETTINGS_MODULE=settings.SETTINGS_MODULE, STAGING_FIXTURE_DATABASE=name))
