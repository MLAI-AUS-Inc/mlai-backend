import json
import base64
import os
from pathlib import Path
import subprocess
import shutil
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
            values=dict(line.split('=',1) for line in before.decode().splitlines() if line and not line.startswith('#'))
            keys=json.loads(values['CONNECTOR_CREDENTIAL_KEYS'])
            self.assertEqual(len(base64.urlsafe_b64decode(keys[values['CONNECTOR_CREDENTIAL_ACTIVE_KEY_ID']])),32)
            self.assertNotEqual(subprocess.run(command,capture_output=True).returncode,0)
            self.assertEqual(target.read_bytes(),before)
            chat.write_text('CHAT_DOMAIN=chat.mlai.au\nMLAI_MEMBERSHIP_ADAPTER_TOKEN='+'a'*64+'\n')
            self.assertNotEqual(subprocess.run(command[:-4]+['--output',str(directory/'other.env'),'--source-sha','b'*40],capture_output=True).returncode,0)

    def test_compose_has_private_api_database_and_no_worker_or_public_data_ports(self):
        env=dict(os.environ,STAGING_API_SOURCE_SHA='b'*40,STAGING_API_SECRET_KEY='synthetic-key',
            STAGING_API_DATABASE_PASSWORD='a'*64,STAGING_API_REDIS_PASSWORD='c'*64,
            COMMUNITY_CHAT_FEDERATION_CLIENT_SECRET='d'*64,COMMUNITY_CHAT_ADAPTER_TOKEN='e'*64)
        env.update(CONNECTOR_CREDENTIAL_KEYS=json.dumps({'staging-v1':base64.urlsafe_b64encode(b'x'*32).decode()}),CONNECTOR_CREDENTIAL_ACTIVE_KEY_ID='staging-v1')
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
        self.assertTrue(services['postgres-api']['image'].startswith('pgvector/pgvector@sha256:'))
        self.assertEqual(api['CONNECTOR_CREDENTIAL_KEYS'],env['CONNECTOR_CREDENTIAL_KEYS'])
        self.assertEqual(api['CONNECTOR_CREDENTIAL_ACTIVE_KEY_ID'],'staging-v1')

    def test_keyring_upgrade_is_private_preserves_existing_values_and_refuses_partial_input(self):
        with tempfile.TemporaryDirectory() as temp:
            directory=Path(temp);chat=directory/'chat.env';target=directory/'api.env'
            chat.write_text('CHAT_DOMAIN=chat-staging.mlai.au\nMLAI_MEMBERSHIP_ADAPTER_TOKEN='+'a'*64+'\n');chat.chmod(0o600)
            command=[sys.executable,str(PROFILE/'config.py')]
            subprocess.run(command+['init','--chat-env',str(chat),'--output',str(target),'--source-sha','b'*40],check=True,capture_output=True)
            legacy={key:value for key,value in (line.split('=',1) for line in target.read_text().splitlines() if line and not line.startswith('#')) if not key.startswith('CONNECTOR_')}
            target.write_text(''.join(key+'='+value+'\n' for key,value in legacy.items()))
            target.chmod(0o400)
            upgraded=subprocess.run(command+['upgrade-keyring','--output',str(target)],capture_output=True,text=True)
            self.assertEqual(upgraded.returncode,0,upgraded.stderr)
            current=dict(line.split('=',1) for line in target.read_text().splitlines() if line and not line.startswith('#'))
            self.assertEqual({key:current[key] for key in legacy},legacy)
            self.assertEqual(target.stat().st_mode & 0o777,0o400)
            key=json.loads(current['CONNECTOR_CREDENTIAL_KEYS'])[current['CONNECTOR_CREDENTIAL_ACTIVE_KEY_ID']]
            self.assertEqual(len(base64.urlsafe_b64decode(key)),32);self.assertNotIn(key,upgraded.stdout+upgraded.stderr)
            before=target.read_bytes();subprocess.run(command+['upgrade-keyring','--output',str(target)],check=True,capture_output=True)
            self.assertEqual(target.read_bytes(),before)
            target.chmod(0o600)
            for replacement in [None,'not-json','{"staging-v1":"not-a-key"}','{"other":"'+key+'"}']:
                bad={**current}
                if replacement is None:del bad['CONNECTOR_CREDENTIAL_KEYS']
                else:bad['CONNECTOR_CREDENTIAL_KEYS']=replacement
                target.write_text(''.join(k+'='+v+'\n' for k,v in bad.items()));snapshot=target.read_bytes()
                refusal=subprocess.run(command+['upgrade-keyring','--output',str(target)],capture_output=True,text=True)
                self.assertNotEqual(refusal.returncode,0);self.assertEqual(target.read_bytes(),snapshot);self.assertNotIn(key,refusal.stdout+refusal.stderr)

    def test_production_mode_staging_profile_boots_with_only_its_private_configuration(self):
        env=dict(os.environ,DEBUG='false',APP_ENV='chat-staging',
            SECRET_KEY='synthetic-key-only-for-isolated-tests',
            DATABASE_URL='postgresql://staging_test@127.0.0.1:65439/federation_local',
            REDIS_URL='redis://127.0.0.1:65440/0',DJANGO_SETTINGS_MODULE='mlai.settings_staging_chat')
        for name in ('HEALTH_HACK_API_KEY','ROO_SIM_PATIENT_KEY','ROO_API_KEY','MLAI_API_KEY','INTERNAL_API_KEY'):
            env.pop(name,None)
        code="import django; django.setup(); from django.test import Client,override_settings; settings=override_settings(ALLOWED_HOSTS=['127.0.0.1']); settings.enable(); response=Client().get('/healthz/live',HTTP_HOST='127.0.0.1'); assert response.status_code == 200, response.status_code"
        result=subprocess.run([sys.executable,'-c',code],env=env,cwd=ROOT,capture_output=True,text=True)
        self.assertEqual(result.returncode,0,result.stderr)

    def test_retagged_api_image_is_refused_before_service_changes(self):
        with tempfile.TemporaryDirectory() as temp:
            directory=Path(temp)
            profile=directory/'ops'/'staging-chat'
            shutil.copytree(PROFILE,profile)
            chat=directory/'chat.env'
            chat.write_text('CHAT_DOMAIN=chat-staging.mlai.au\nMLAI_MEMBERSHIP_ADAPTER_TOKEN='+'a'*64+'\n')
            chat.chmod(0o600)
            config=profile/'.env'
            subprocess.run([sys.executable,str(PROFILE/'config.py'),'init','--chat-env',str(chat),'--output',str(config),'--source-sha','b'*40],check=True,capture_output=True)
            metadata=directory/'image.json'
            image={'Id':'sha256:'+'c'*64,'Config':{'Labels':{'org.opencontainers.image.revision':'b'*40,'au.mlai.environment':'chat-staging-api'}}}
            metadata.write_text(json.dumps([image]))
            record=subprocess.run([sys.executable,str(PROFILE/'config.py'),'record-build','--output',str(config),'--metadata',str(metadata),'--source-sha','b'*40],capture_output=True,text=True)
            self.assertEqual(record.returncode,0,record.stderr)
            image['Id']='sha256:'+'d'*64
            metadata.write_text(json.dumps([image]))
            binaries=directory/'bin';binaries.mkdir()
            docker=binaries/'docker'
            docker.write_text("#!/usr/bin/env python3\nimport json,os,sys\nwith open(os.environ['COMMAND_LOG'],'a') as out:out.write(json.dumps(sys.argv[1:])+'\\n')\nif sys.argv[1:3]==['image','inspect']:print(open(os.environ['IMAGE_METADATA']).read())\n")
            docker.chmod(0o700)
            trace=directory/'commands.jsonl'
            env=dict(os.environ,PATH=str(binaries)+os.pathsep+os.environ['PATH'],COMMAND_LOG=str(trace),IMAGE_METADATA=str(metadata))
            result=subprocess.run(['bash',str(profile/'run.sh'),'start'],env=env,capture_output=True,text=True)
            self.assertNotEqual(result.returncode,0)
            self.assertEqual([json.loads(line)[:2] for line in trace.read_text().splitlines()],[['image','inspect']])
