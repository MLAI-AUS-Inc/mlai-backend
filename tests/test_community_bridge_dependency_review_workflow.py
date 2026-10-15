"""Dependency review has one fixed selector and cannot mutate production."""
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1]


class DependencyReviewWorkflowTests(unittest.TestCase):
    def test_workflow_yaml_is_well_formed(self):
        import yaml
        for filename in ('deploy.yml', 'community-bridge-dependency-review.yml'):
            yaml.safe_load((ROOT / '.github/workflows' / filename).read_text())

    def test_workflow_parser_dependency_is_locked_and_installed_before_checks(self):
        import yaml
        workflow = yaml.safe_load((ROOT / '.github/workflows/deploy.yml').read_text())
        steps = workflow['jobs']['checks']['steps']
        check_index = next(index for index, step in enumerate(steps)
                           if 'tests.test_community_bridge_dependency_review_workflow' in step.get('run', ''))
        install_index = next(index for index, step in enumerate(steps)
                             if '-r .github/requirements-ci.lock' in step.get('run', ''))
        self.assertLess(install_index, check_index)
        self.assertIn('--require-hashes', steps[install_index]['run'])
        self.assertIn('--only-binary=:all:', steps[install_index]['run'])
        lock_lines = (ROOT / '.github/requirements-ci.lock').read_text().lower().splitlines()
        for line in (ROOT / '.github/requirements-ci.txt').read_text().splitlines():
            requirement = line.strip()
            if requirement and not requirement.startswith('#'):
                self.assertIn('==', requirement)
                self.assertTrue(any(locked.startswith(requirement.lower() + ' ') for locked in lock_lines))

    def test_only_fixed_read_only_commands_are_available(self):
        text = (ROOT / '.github/workflows/community-bridge-dependency-review.yml').read_text()
        inputs = text.split('    inputs:\n', 1)[1].split('\npermissions:', 1)[0]
        self.assertEqual([line.strip() for line in inputs.splitlines() if line.startswith('      ') and not line.startswith('       ') and line.endswith(':')], ['command:'])
        self.assertNotIn('--apply', text)
        self.assertNotIn('expected', text)
        for line in text.splitlines():
            if 'ssh root@' in line:
                self.assertNotIn('${{ inputs.', line)
        self.assertIn('tests.test_community_bridge_dependency_review_workflow', (ROOT / '.github/workflows/deploy.yml').read_text())
