"""Stage-only status is authenticated, bounded, stale-aware, and never leaks host fields."""
import json
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from django.test import SimpleTestCase,override_settings
from rest_framework.test import APIRequestFactory,force_authenticate
from roo import development

class StatusTests(SimpleTestCase):
    def test_unimplemented_status_is_explicitly_unavailable(self):
        self.assertTrue(hasattr(development,'read_staging_status'),'status reader missing')
    def records(self,d,**kw):
        for component in ('backend','frontend'):
            data=dict(component=component,active_sha='a'*40,desired_sha='a'*40,readiness='ready',outcome='unchanged',last_checked_at=1000,observed_at=1000,password='must-not-leak',reason='',**kw)
            (Path(d)/(component+'.json')).write_text(json.dumps(data))
    def test_ready_reports_are_redacted_and_bound_to_actual_api_release(self):
        with tempfile.TemporaryDirectory() as d:
            self.records(d)
            result=development.read_staging_status(Path(d),now=1100,release='a'*12)
            self.assertEqual(result['status'],'ready')
            self.assertEqual(result['components']['frontend']['release'],'a'*12)
            self.assertNotIn('must-not-leak',json.dumps(result))
            self.assertEqual(development.read_staging_status(Path(d),now=1100,release='b'*12)['status'],'checking')
    def test_missing_corrupt_stale_and_future_reports_never_claim_ready(self):
        with tempfile.TemporaryDirectory() as d:
            self.assertEqual(development.read_staging_status(Path(d),now=1100,release='a'*12)['status'],'checking')
            self.records(d)
            self.assertEqual(development.read_staging_status(Path(d),now=1700,release='a'*12)['status'],'stale')
            self.assertEqual(development.read_staging_status(Path(d),now=800,release='a'*12)['status'],'stale')
            (Path(d)/'backend.json').write_text('invalid')
            self.assertEqual(development.read_staging_status(Path(d),now=1100,release='a'*12)['status'],'checking')
    def test_paused_failed_and_pending_candidates_are_clear(self):
        with tempfile.TemporaryDirectory() as d:
            self.records(d)
            path=Path(d)/'backend.json';data=json.loads(path.read_text());data.update(outcome='incompatible',reason='schema-inputs',readiness='needs-attention');path.write_text(json.dumps(data))
            result=development.read_staging_status(Path(d),now=1100,release='a'*12)
            self.assertEqual(result['status'],'needs-attention')
            self.assertIn('review',result['message'])
    @override_settings(APP_ENV='chat-staging')
    def test_anonymous_cannot_read_and_authenticated_cannot_write(self):
        view=development.DevelopmentStatusView.as_view();request=APIRequestFactory().get('/api/v1/points/development/status/')
        self.assertEqual(view(request).status_code,401)
        request=APIRequestFactory().post('/api/v1/points/development/status/',{})
        force_authenticate(request,user=SimpleNamespace(is_authenticated=True));self.assertEqual(view(request).status_code,405)
    @override_settings(APP_ENV='production')
    def test_unavailable_in_production(self):
        request=APIRequestFactory().get('/api/v1/points/development/status/');force_authenticate(request,user=SimpleNamespace(is_authenticated=True))
        self.assertEqual(development.DevelopmentStatusView.as_view()(request).status_code,404)

    def test_nonfinite_timestamps_are_not_valid_health_evidence(self):
        with tempfile.TemporaryDirectory() as d:
            self.records(d)
            p=Path(d)/'backend.json';data=json.loads(p.read_text());data['last_checked_at']=float('nan');p.write_text(json.dumps(data))
            self.assertEqual(development.read_staging_status(Path(d),now=1100,release='a'*12)['status'],'checking')
