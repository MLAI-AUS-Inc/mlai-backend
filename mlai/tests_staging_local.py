"""Local tools cannot mint hosted credentials or take over an account."""
import io
import tempfile
from pathlib import Path
from django.test import TestCase, override_settings
from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.core.management.base import CommandError
from rest_framework.test import APIClient
from community_chat.models import CommunityMemberProfile
from community_chat.tests.test_staging_password import PILOT, NATIVE

LOCAL=dict(SETTINGS_MODULE='mlai.settings_staging_local',APP_ENV='staging-local',STAGING_LOCAL_DB_NAME='staging_points_tests')

@override_settings(**LOCAL)
class LocalTokenTests(TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.output=Path(self.temp.name)/'token'
    def issue(self,create=False):
        output=io.StringIO()
        call_command('staging_local_token',user='developer@staging.mlai.au',output=str(self.output),create=create,stdout=output)
        token=self.output.read_text().strip()
        self.assertNotIn(token,output.getvalue())
        self.assertEqual(self.output.stat().st_mode & 0o777,0o600)
        return token
    def test_explicit_fixture_private_token_and_hosted_rejection(self):
        token=self.issue(create=True)
        user=get_user_model().objects.get(email='developer@staging.mlai.au')
        self.assertFalse(user.is_staff or user.is_superuser or user.has_usable_password())
        client=APIClient();client.credentials(HTTP_AUTHORIZATION='Bearer '+token)
        with override_settings(ROOT_URLCONF='mlai.urls'):
            self.assertEqual(client.get('/api/v1/auth/me/').status_code,200)
        with override_settings(**PILOT):
            self.assertEqual(client.get('/api/v1/auth/me/',HTTP_ORIGIN=NATIVE).status_code,401)
    def test_wrong_environment_and_database_refused(self):
        for changes in ({'SETTINGS_MODULE':'mlai.settings_staging_chat'},{'APP_ENV':'production'},{'STAGING_LOCAL_DB_NAME':'staging_account_api'}):
            with override_settings(**changes),self.assertRaises(CommandError):self.issue(create=True)
        self.assertFalse(get_user_model().objects.exists())
    def test_unmarked_staff_and_overwrite_refused(self):
        user=get_user_model().objects.create_user(email='developer@staging.mlai.au')
        with self.assertRaises(CommandError):self.issue()
        CommunityMemberProfile.objects.create(user=user,status='approved',review_reasons=['synthetic_staging_local'])
        user.is_staff=True;user.save()
        with self.assertRaises(CommandError):self.issue()
        user.is_staff=False;user.save()
        self.issue()
        with self.assertRaises(CommandError):self.issue()

from django.test import SimpleTestCase
class LocalConfigurationTests(SimpleTestCase):
    def test_launcher_checks_schema_and_never_applies_it(self):
        source=Path('ops/staging-chat/local-dev.sh').read_text()
        self.assertIn("'migrate','--check','--noinput'",source)
        self.assertNotIn('RUN_MIGRATIONS_ON_START=1',source)
        self.assertFalse(any(line.strip().startswith(('source ','. ')) for line in source.splitlines()))
        self.assertIn("'127.0.0.1:8000'",source)
    def test_local_profile_rejects_hosted_database_or_weak_key(self):
        import subprocess,os,secrets
        base={k:v for k,v in os.environ.items() if k in {'PATH','HOME','LANG'}}
        base.update(APP_ENV='staging-local',DJANGO_SETTINGS_MODULE='mlai.settings_staging_local',SECRET_KEY=secrets.token_hex(48),DATABASE_URL='postgresql://local_mlai:unused@127.0.0.1:65450/local_mlai',REDIS_URL='redis://127.0.0.1:65451/0')
        code="import mlai.settings_staging_local as s; assert s.SIMPLE_JWT['ISSUER']=='mlai-staging-local'; assert s.SECRET_KEY==s.SIMPLE_JWT['SIGNING_KEY']; assert s.CORS_ALLOWED_ORIGINS==['http://localhost:5173','http://127.0.0.1:5173']"
        result=subprocess.run([__import__('sys').executable,'-c',code],env=base,capture_output=True)
        self.assertEqual(result.returncode,0,result.stderr.decode()[-1000:])
        for changes in ({'DATABASE_URL':'postgresql://user:unused@api-staging.mlai.au/staging_account_api'},{'SECRET_KEY':'weak'},{'APP_ENV':'chat-staging'}):
            result=subprocess.run([__import__('sys').executable,'-c',code],env={**base,**changes},capture_output=True)
            self.assertNotEqual(result.returncode,0)
