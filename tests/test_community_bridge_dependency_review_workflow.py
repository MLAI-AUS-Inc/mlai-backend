"""Dependency review has one fixed selector and cannot mutate production."""
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1]


class DependencyReviewWorkflowTests(unittest.TestCase):
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
