"""Exercise every plan scenario against the real code with no provider network."""

import io
import json
import os
from unittest.mock import patch

from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import connection
from django.test import TestCase, override_settings

from crm.deliverability import simulation
from crm.models import Blast, Contact, EmailEvent, SendingStream


class CampaignSimulationDatabaseTests(TestCase):
    def test_command_refuses_non_test_environment_and_missing_exact_name_opt_in(self):
        with override_settings(APP_ENV='production'), self.assertRaises(CommandError):
            simulation.run('healthy')
        with patch.dict(os.environ, {'CRM_SIMULATION_DATABASE': 'wrong_test_database'}), self.assertRaises(CommandError):
            simulation.run('healthy')
        self.assertEqual(Contact.objects.count(), 0)

    def test_nonempty_crm_is_never_used_as_a_fixture(self):
        Contact.objects.create(email='retained@example.test', source='manual')
        with patch.dict(os.environ, {'CRM_SIMULATION_DATABASE': str(connection.settings_dict['NAME'])}), self.assertRaises(CommandError):
            simulation.run('healthy')
        self.assertEqual(Contact.objects.count(), 1)


def scenario_test(name):
    def test(self):
        output = io.StringIO()
        with patch.dict(os.environ, {'CRM_SIMULATION_DATABASE': str(connection.settings_dict['NAME'])}):
            call_command('crm_simulate_send', scenario=name, recipients=5000, days=8, stdout=output)
        result = json.loads(output.getvalue())
        self.assertEqual((result['scenario'], result['status'], result['rows_persisted']), (name, 'passed', 0))
        self.assertNotIn('@', output.getvalue())
        self.assertFalse(any(model.objects.exists() for model in (Blast, Contact, EmailEvent, SendingStream)))
    return test


for _scenario in simulation.SCENARIOS:
    setattr(CampaignSimulationDatabaseTests, 'test_scenario_' + _scenario, scenario_test(_scenario))
