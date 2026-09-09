import unittest
from unittest.mock import MagicMock

from core.management.commands.office_manager_deploy_requirements import integration_required


class OfficeManagerDeployRequirementsTests(unittest.TestCase):
    def database(self, tables, rows=()):
        database = MagicMock()
        database.introspection.table_names.return_value = tables
        database.ops.quote_name.side_effect = lambda name: '"' + name + '"'
        database.cursor.return_value.__enter__.return_value.fetchone.side_effect = rows
        return database

    def test_enabled_always_requires_companion(self):
        database = self.database([])
        self.assertTrue(integration_required(database, enabled=True))
        database.introspection.table_names.assert_not_called()

    def test_pristine_disabled_installation_can_bootstrap(self):
        database = self.database(['django_migrations', 'roo_coworkingbooking'])
        self.assertFalse(integration_required(database, enabled=False))
        database.cursor.return_value.__enter__.return_value.execute.assert_not_called()

    def test_migrated_but_unused_disabled_installation_can_bootstrap(self):
        database = self.database(['roo_officemanagerday', 'roo_officemanagerassignment'], [None, None])
        self.assertFalse(integration_required(database, enabled=False))

    def test_any_stored_state_requires_recovery_integration(self):
        database = self.database(['roo_officemanagerday', 'roo_officemanagerclaimattempt'], [None, (1,)])
        self.assertTrue(integration_required(database, enabled=False))
        self.assertEqual(database.cursor.return_value.__enter__.return_value.execute.call_count, 2)

    def test_database_failure_does_not_become_bootstrap_permission(self):
        database = self.database([])
        database.introspection.table_names.side_effect = RuntimeError('database unavailable')
        with self.assertRaises(RuntimeError):
            integration_required(database, enabled=False)

    def test_query_failure_does_not_become_bootstrap_permission(self):
        database = self.database(['roo_officemanagerday'])
        database.cursor.return_value.__enter__.return_value.execute.side_effect = RuntimeError('query failed')
        with self.assertRaises(RuntimeError):
            integration_required(database, enabled=False)
