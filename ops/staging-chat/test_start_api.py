"""Every API start (including host boot) crosses the installed isolation gate."""
from pathlib import Path
import importlib.util,unittest,contextlib
from unittest.mock import Mock,patch
class StartTests(unittest.TestCase):
    def setUp(self):
        spec=importlib.util.spec_from_file_location('start',Path(__file__).with_name('start-api.py'))
        self.m=importlib.util.module_from_spec(spec);spec.loader.exec_module(self.m)
    def test_no_docker_autorestart_and_ordered_host_service(self):
        profile=Path(__file__).parent
        source=(profile/'compose.yml').read_text().split('  postgres-api:')[0]
        self.assertIn('restart: "no"',source)
        service=(profile/'systemd/mlai-backend-staging-api.service').read_text()
        self.assertIn('After=docker.service mlai-staging-network-policy.service',service)
        self.assertIn('start-api.py',service)
    def test_policy_failure_never_activates(self):
        c=Mock();c.profile=Path('/installed');c.locked.return_value=contextlib.nullcontext(True);c.journal.exists.return_value=False
        c.run.side_effect=RuntimeError('policy unavailable')
        with self.assertRaises(RuntimeError):self.m.ensure(c)
        c.activate.assert_not_called()
    def test_verify_precedes_start_and_healthy_check(self):
        calls=[];c=Mock();c.locked.return_value=contextlib.nullcontext(True);c.journal.exists.return_value=False
        c.profile=Path('/installed');c.peer_recovery_pending.return_value=False;c.paused.exists.return_value=False
        c.run.side_effect=lambda args,**kwargs:calls.append('policy')
        c.activate.side_effect=lambda path:calls.append('start');c.health.side_effect=lambda:calls.append('health')
        self.assertEqual(self.m.ensure(c),'running');self.assertEqual(calls,['policy','start','health'])
