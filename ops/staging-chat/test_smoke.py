"""Exercise real smoke protocol and HTTP failures with a local fake transport."""
import importlib.util
from pathlib import Path
import tempfile
import unittest

spec=importlib.util.spec_from_file_location('smoke',Path(__file__).with_name('smoke.py'))
m=importlib.util.module_from_spec(spec) if spec else None
if Path(spec.origin).exists():spec.loader.exec_module(m)

class SmokeTests(unittest.TestCase):
    def test_signed_in_checks_refresh_and_logout_without_business_writes(self):
        calls=[]
        def request(path, body=None):
            calls.append((path,body))
            if path.endswith('/build/'):return {'environment':'chat-staging','release':'a'*12}
            return {}
        result=m.check({'email':'smoke@staging.mlai.au','password':'private','public_key':'b'*64,'installation_id':'88f58ea6-3968-4534-a27d-d7335554fd1a'},'a'*40,request)
        self.assertEqual(result,{'ok':True,'check':'signed-in','release':'a'*12})
        self.assertEqual(calls[0][1]['client_id'],'mlai-chat-web')
        self.assertEqual(calls[-1][0],'/community-chat/auth/session/logout/')
        self.assertIn(('/community-chat/auth/session/refresh/',{}),calls)
        self.assertFalse(any(body is not None and '/auth/' not in path for path,body in calls))
    def test_wrong_release_fails_and_still_logs_out(self):
        calls=[]
        def request(path,body=None):
            calls.append(path)
            return {'environment':'chat-staging','release':'b'*12}
        with self.assertRaisesRegex(m.SmokeFailure,'release'):m.check({'email':'smoke@staging.mlai.au','password':'p','public_key':'b'*64,'installation_id':'88f58ea6-3968-4534-a27d-d7335554fd1a'},'a'*40,request)
        self.assertEqual(calls[-1],'/community-chat/auth/session/logout/')
    def test_invalid_target_or_credentials_never_sends(self):
        for email in ('person@mlai.au','',None):
            with self.assertRaises(m.SmokeFailure):m.check({'email':email},'a'*40,lambda *a:self.fail('sent credentials'))
    def test_protocol_rejects_redirects_bad_cors_and_missing_cookie_flags(self):
        for status,headers in [(302,{}),(200,{}),(200,{'Access-Control-Allow-Origin':'https://evil.example'})]:
            with self.assertRaises(m.SmokeFailure):m.validate_response(status,headers)
        m.validate_response(200,{'Access-Control-Allow-Origin':'https://chat-staging.mlai.au','Access-Control-Allow-Credentials':'true'})
    def test_private_credentials_reject_symlinks_and_permissions(self):
        with tempfile.TemporaryDirectory() as d:
            p=Path(d)/'secret';p.write_text('{}');p.chmod(0o644)
            with self.assertRaises(m.SmokeFailure):m.read_credentials(p)
            p.chmod(0o600);link=Path(d)/'link';link.symlink_to(p)
            with self.assertRaises(m.SmokeFailure):m.read_credentials(link)
