import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT=Path(__file__).resolve().parents[1]
PROFILE=ROOT/'ops/staging-chat'


class StagingFederationDeploymentTests(unittest.TestCase):
    def test_initializer_preserves_credentials_and_rejects_production_input(self):
        with tempfile.TemporaryDirectory() as temp:
            directory=Path(temp)
            chat=directory/'chat.env'
            chat.write_text('CHAT_DOMAIN=chat-staging.mlai.au\nMLAI_MEMBERSHIP_ADAPTER_TOKEN='+'a'*64+'\n')
            chat.chmod(0o600)
            target=directory/'api.env'
            command=[sys.executable,str(PROFILE/'config.py'),'init','--chat-env',str(chat),'--output',str(target),'--source-sha','b'*40]
            result=subprocess.run(command,capture_output=True,text=True)
            self.assertEqual(result.returncode,0,result.stderr)
            before=target.read_bytes()
            self.assertEqual(target.stat().st_mode & 0o777,0o600)
            self.assertNotIn('a'*64,result.stdout+result.stderr)
            self.assertNotIn('BUZZ_RELAY_PRIVATE_KEY',before.decode())
            self.assertNotEqual(subprocess.run(command,capture_output=True).returncode,0)
            self.assertEqual(target.read_bytes(),before)
            chat.write_text('CHAT_DOMAIN=chat.mlai.au\nMLAI_MEMBERSHIP_ADAPTER_TOKEN='+'a'*64+'\n')
            self.assertNotEqual(subprocess.run(command[:-4]+['--output',str(directory/'other.env'),'--source-sha','b'*40],capture_output=True).returncode,0)

    def test_compose_has_private_api_database_and_no_worker_or_public_data_ports(self):
        env=dict(os.environ,STAGING_API_SOURCE_SHA='b'*40,STAGING_API_SECRET_KEY='synthetic-key',
            STAGING_API_DATABASE_PASSWORD='a'*64,STAGING_API_REDIS_PASSWORD='c'*64,
            COMMUNITY_CHAT_FEDERATION_CLIENT_SECRET='d'*64,COMMUNITY_CHAT_ADAPTER_TOKEN='e'*64)
        result=subprocess.run(['docker','compose','-f',str(PROFILE/'compose.yml'),'config','--format','json'],
            env=env,capture_output=True,text=True)
        self.assertEqual(result.returncode,0,result.stderr)
        config=json.loads(result.stdout)
        self.assertEqual(config['name'],'mlai-chat-staging-api')
        services=config['services']
        self.assertEqual(set(services),{'staging-api','postgres-api','redis-api'})
        self.assertFalse(any(service.get('ports') for service in services.values()))
        api=services['staging-api']['environment']
        self.assertIn('@postgres-api:',api['DATABASE_URL'])
        self.assertEqual(api['RUN_MIGRATIONS_ON_START'],'0')
        self.assertEqual(api['DJANGO_SETTINGS_MODULE'],'mlai.settings_staging_chat')
        self.assertEqual(api['COMMUNITY_CHAT_ADAPTER_URL'],'http://membership-adapter:3100')
        self.assertEqual(config['networks']['chat']['name'],'mlai-chat-staging_chat')
