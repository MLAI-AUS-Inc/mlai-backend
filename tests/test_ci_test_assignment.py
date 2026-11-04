"""CI selection must account for executable namespace-package test labels."""
import unittest

from scripts.check_test_assignment import classify, selected_labels


class TestAssignmentLabels(unittest.TestCase):
    def test_namespace_labels_select_the_real_controller_suites(self):
        modules = {
            "ops.staging-chat.test_auto_deploy",
            "ops.staging-chat.test_network_policy",
            "ops.staging-chat.test_start_api",
        }
        labels = selected_labels("python -m unittest " + " ".join(sorted(modules)) + " -v")
        self.assertEqual(classify(modules, labels), (modules, set(), set()))
        loader = unittest.TestLoader()
        suite = loader.loadTestsFromNames(sorted(labels))
        self.assertFalse(loader.errors)
        self.assertEqual(suite.countTestCases(), 31)

    def test_continued_django_commands_keep_each_complete_module(self):
        workflow = "python manage.py test \\\n  community_chat.tests.test_staging_password \\\n  mlai.tests_staging_platform --noinput --verbosity 2\n"
        self.assertEqual(
            selected_labels(workflow),
            {"community_chat.tests.test_staging_password", "mlai.tests_staging_platform"},
        )

    def test_flags_paths_and_shell_fragments_are_not_test_labels(self):
        self.assertEqual(
            selected_labels("python -m unittest tests.test_ci_test_assignment -v ../other.py '$OTHER' --failfast"),
            {"tests.test_ci_test_assignment"},
        )
