#!/usr/bin/env python3
"""Poll the approved staging branch and deploy ordinary backend code inside the isolated runtime."""
import argparse
import contextlib
import fcntl
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time

REPOSITORY='git@github.com:MLAI-AUS-Inc/mlai-backend.git'
REF='refs/heads/staging'
HOST='ubuntu-s-4vcpu-8gb-syd1'
CONFIG=Path('/etc/mlai-backend-staging-auto-deploy/config.json')
SHA=re.compile(r'[0-9a-f]{40}')
OPERATIONAL=['ops','scripts','Dockerfile','.dockerignore','.gitattributes','.gitmodules','requirements.txt','requirements-engine.txt','requirements.lock','manage.py','mlai/settings.py','mlai/settings_staging_chat.py','mlai/settings_staging_local.py','mlai/staging_integrations.py','mlai/wsgi.py','mlai/asgi.py','core/authentication.py','core/refresh_sessions.py','core/password_auth.py','community_chat/account_sessions.py','community_chat/tokens.py','community_chat/bootstrap.py','community_chat/federation.py','community_chat/authentication.py','community_chat/staging_password.py','community_chat/staging_views.py','community_chat/staging_urls.py','mlai/urls_staging_chat.py']

class Failure(Exception):
    """An attempt failed without making its candidate the successful release."""

class Incompatible(Failure):
    """The source requires a reviewed manual deployment."""


def validate_target(cfg):
    if (cfg.get('repo')!=REPOSITORY or cfg.get('ref')!=REF or cfg.get('hostname')!=HOST
            or not re.fullmatch(r'[0-9a-f]{32}',cfg.get('machine_id',''))
            or not SHA.fullmatch(cfg.get('source_sha',''))):
        raise Failure('target')


def private_file(path):
    path=Path(path)
    if path.is_symlink() or not path.is_file() or path.stat().st_mode & 0o077 or path.stat().st_uid!=os.geteuid():
        raise Failure('private-file')


class Controller:
    def __init__(self,cfg,clock=time.time):
        self.cfg=cfg;self.clock=clock
        self.root=Path(cfg['state_dir']);self.profile=Path(cfg['profile']);self.env=self.profile/'.env'
        self.journal=self.root/'activation.json';self.paused=self.root/'PAUSED'
        self.cache=self.root/'cache.git';self.releases=self.root/'releases'

    def write_bytes(self,path,data):
        path=Path(path)
        if path.is_symlink():raise Failure('symlink')
        fd,name=tempfile.mkstemp(dir=path.parent)
        try:
            with os.fdopen(fd,'wb') as out:out.write(data);out.flush();os.fsync(out.fileno())
            os.replace(name,path)
            directory=os.open(path.parent,os.O_RDONLY)
            try:os.fsync(directory)
            finally:os.close(directory)
        finally:
            if os.path.exists(name):os.unlink(name)

    def write_json(self,path,data):self.write_bytes(path,(json.dumps(data,sort_keys=True)+'\n').encode())
    def load(self):
        path=self.root/'state.json';private_file(path)
        return json.loads(path.read_text())
    def save(self,state):self.write_json(self.root/'state.json',state)

    @contextlib.contextmanager
    def locked(self,blocking=False,host=True):
        path=self.root/'deploy.lock'
        if path.is_symlink():raise Failure('lock')
        fd=os.open(path,os.O_RDWR|os.O_CREAT,0o600)
        try:
            private_file(path)
            try:fcntl.flock(fd,fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB))
            except BlockingIOError:yield False
            else:
                if host:
                    with self.host_locked(blocking) as acquired:yield acquired
                else:yield True
        finally:os.close(fd)

    @contextlib.contextmanager
    def host_locked(self,blocking=False):
        path=Path(self.cfg.get('shared_lock','/var/lock/mlai-staging-deploy.lock'))
        fd=os.open(path,os.O_RDWR|os.O_CREAT|os.O_NOFOLLOW,0o600)
        try:
            private_file(path)
            try:fcntl.flock(fd,fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB))
            except BlockingIOError:yield False
            else:yield True
        finally:os.close(fd)

    def peer_recovery_pending(self):
        root=Path(self.cfg.get('peer_state','/var/lib/mlai-chat-staging-auto-deploy'))
        flag=root/'PAUSED'
        return (root/'activation.json').exists() or (flag.exists() and flag.read_text().strip() in {'rollback-failed','recovery-failed'})

    def validate(self):
        validate_target(self.cfg)
        if self.cfg.get('shared_lock','/var/lock/mlai-staging-deploy.lock')!='/var/lock/mlai-staging-deploy.lock' or self.cfg.get('peer_state','/var/lib/mlai-chat-staging-auto-deploy')!='/var/lib/mlai-chat-staging-auto-deploy':raise Failure('coordination-path')
        if os.geteuid()!=0 or socket.gethostname()!=HOST or Path('/etc/machine-id').read_text().strip()!=self.cfg['machine_id']:raise Failure('host-identity')
        if self.root!=Path('/var/lib/mlai-backend-staging-auto-deploy') or self.profile!=Path('/opt/mlai-backend-staging/ops/staging-chat'):raise Failure('paths')
        for directory in [self.root,self.releases]:
            if directory.is_symlink() or not directory.is_dir() or directory.stat().st_uid!=0 or directory.stat().st_mode & 0o077:raise Failure('private-directory')
        for path in [self.env,Path(self.cfg['ssh_key']),Path(self.cfg['known_hosts'])]:private_file(path)
        for name,digest in self.cfg['helper_hashes'].items():
            if Path(name).name!=name:raise Failure('helper-name')
            path=self.profile/name
            if path.is_symlink() or path.stat().st_uid!=0 or hashlib.sha256(path.read_bytes()).hexdigest()!=digest:raise Failure('installed-helper')
        values=self.values(self.env)
        if values['COMMUNITY_CHAT_STAGING_PASSWORD_ENABLED']!='true' or values['COMMUNITY_CHAT_FEDERATION_STAGING_ENABLED']!='false':raise Failure('runtime-mode')

    def run(self,args,timeout=120,env=None):
        # Suppress subprocess output: fetched source/build failures must not expose private env.
        try:
            process=subprocess.Popen(args,stdout=subprocess.PIPE,stderr=subprocess.PIPE,
                                     env=env,start_new_session=True)
            try:out,_=process.communicate(timeout=timeout)
            except BaseException:
                os.killpg(process.pid,signal.SIGKILL);process.communicate();raise
            if process.returncode:raise Failure('command')
            return out.decode()
        except (OSError,subprocess.TimeoutExpired) as exc:raise Failure('command') from exc

    def git(self,path,*args):return self.run(['git','-C',str(path),*args]).strip()

    def fetch(self):
        if not self.cache.exists():self.run(['git','init','--bare',str(self.cache)])
        key=Path(self.cfg['ssh_key']);hosts=Path(self.cfg['known_hosts'])
        # Fixed, validated installation paths; never interpolate moving source into SSH shell text.
        if key!=Path('/etc/mlai-backend-staging-auto-deploy/id_ed25519') or hosts!=Path('/etc/mlai-backend-staging-auto-deploy/known_hosts'):raise Failure('ssh-path')
        env=dict(os.environ,GIT_SSH_COMMAND=f'ssh -i {key} -o IdentitiesOnly=yes -o BatchMode=yes -o StrictHostKeyChecking=yes -o UserKnownHostsFile={hosts} -o ConnectTimeout=10',GIT_TERMINAL_PROMPT='0')
        self.run(['git','-C',str(self.cache),'fetch','--no-tags',self.cfg['repo'],'+'+self.cfg['ref']+':'+REF],timeout=120,env=env)
        sha=self.git(self.cache,'rev-parse',REF+'^{commit}')
        if not SHA.fullmatch(sha):raise Failure('branch-sha')
        return sha

    def checkout(self,sha):
        release=self.releases/sha;source=release/'source'
        if not release.exists():release.mkdir(mode=0o700)
        if not source.exists():
            self.run(['git','clone','--no-checkout','--shared',str(self.cache),str(source)])
            self.git(source,'checkout','--detach',sha)
        if self.git(source,'rev-parse','HEAD')!=sha or self.git(source,'status','--porcelain'):raise Failure('source-checkout')
        return source

    def compatible(self,source):
        changed=self.git(source,'diff','--name-only',self.cfg['source_sha'],'HEAD').splitlines()
        if any(path==gate or path.startswith(gate+'/') for path in changed for gate in OPERATIONAL):raise Incompatible('operational-inputs')
        if any('/migrations/' in '/'+path for path in changed):raise Incompatible('schema-inputs')
        # Entire normal URL tree and domain code stay compatible; policy helpers
        # are installed/pinned separately and never executed from this checkout.
        for name,digest in self.cfg['migration_hashes'].items():
            path=source/name
            if not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest()!=digest:raise Incompatible('approved-schema-drift')

    def values(self,path):
        private_file(path)
        spec=importlib.util.spec_from_file_location('staging_config',self.profile/'config.py')
        module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
        return module.read_config(Path(path))

    def running(self,values):
        actual=self.run(['docker','inspect','mlai-chat-staging-api-staging-api-1','--format','{{.Image}}']).strip()
        if actual!=values['STAGING_API_IMAGE_ID']:raise Failure('running-image')
        self.run(['python3',str(self.profile/'network-policy.py'),'verify'])
        metadata=self.root/'running-image.json';self.write_bytes(metadata,self.run(['docker','image','inspect',actual]).encode())
        self.run(['python3',str(self.profile/'config.py'),'check-image','--output',str(self.env),'--metadata',str(metadata)])

    def build(self,sha,source,candidate):
        tag='mlai-chat-staging-api:'+sha
        try:metadata=self.run(['docker','image','inspect',tag])
        except Failure:metadata=None
        if metadata is None:
            self.run(['docker','build','--target','runtime','--label','org.opencontainers.image.revision='+sha,'--label','au.mlai.environment=chat-staging-api','-t',tag,str(source)],timeout=2400)
            metadata=self.run(['docker','image','inspect',tag])
        parsed=json.loads(metadata)
        if len(parsed)!=1 or not re.fullmatch(r'sha256:[0-9a-f]{64}',parsed[0].get('Id','')):raise Failure('candidate-image')
        image=parsed[0]['Id']
        metadata_path=candidate.parent/'image.json';self.write_bytes(metadata_path,metadata.encode())
        self.write_bytes(candidate,self.env.read_bytes())
        self.run(['python3',str(self.profile/'config.py'),'record-build','--output',str(candidate),'--metadata',str(metadata_path),'--source-sha',sha])
        self.check_candidate(image,candidate)

    def check_candidate(self,image,candidate):
        self.run(['python3',str(self.profile/'network-policy.py'),'verify'])
        values=self.values(candidate)
        runtime=candidate.parent/'runtime.env'
        environment={'DJANGO_SETTINGS_MODULE':'mlai.settings_staging_chat','APP_ENV':'chat-staging','DEBUG':'false','ALLOWED_HOSTS':'api-staging.mlai.au,staging-api,localhost,127.0.0.1','SECRET_KEY':values['STAGING_API_SECRET_KEY'],'DATABASE_URL':'postgresql://staging_account_api:'+values['STAGING_API_DATABASE_PASSWORD']+'@postgres-api:5432/staging_account_api','REDIS_URL':'redis://:'+values['STAGING_API_REDIS_PASSWORD']+'@redis-api:6379/0','CONNECTOR_CREDENTIAL_KEYS':values['CONNECTOR_CREDENTIAL_KEYS'],'CONNECTOR_CREDENTIAL_ACTIVE_KEY_ID':values['CONNECTOR_CREDENTIAL_ACTIVE_KEY_ID'],'COMMUNITY_CHAT_FEDERATION_CLIENT_SECRET':values['COMMUNITY_CHAT_FEDERATION_CLIENT_SECRET'],'COMMUNITY_CHAT_STAGING_PASSWORD_ENABLED':'true','COMMUNITY_CHAT_FEDERATION_STAGING_ENABLED':'false','COMMUNITY_CHAT_ADAPTER_URL':'http://membership-adapter:3100','COMMUNITY_CHAT_ADAPTER_TOKEN':values['COMMUNITY_CHAT_ADAPTER_TOKEN'],'RUN_MIGRATIONS_ON_START':'0'}
        self.write_bytes(runtime,(''.join(k+'='+v+'\n' for k,v in environment.items())).encode())
        # Trusted check text; executes only inside the same enforced API network.
        check="import os,hashlib; os.environ.setdefault('DJANGO_SETTINGS_MODULE','mlai.settings_staging_chat'); import django; django.setup(); from django.core.management import call_command; call_command('check'); call_command('migrate',check=True,interactive=False); from django.urls import resolve; assert resolve('/api/v1/auth/me/').func.view_class.__name__=='CurrentUserView'; from django.conf import settings; assert settings.APP_ENV=='chat-staging'; assert settings.COMMUNITY_CHAT_FEDERATION_ISSUER_ENABLED is False; from rest_framework.settings import api_settings; assert api_settings.DEFAULT_AUTHENTICATION_CLASSES[0].__module__=='core.authentication'"
        for path,digest in self.cfg['dependency_hashes'].items():
            check+='; assert hashlib.sha256(open('+repr(path)+',"rb").read()).hexdigest()=='+repr(digest)
        name='mlai-staging-api-candidate-check'
        try:
            self.run(['docker','run','--rm','--name',name,'--network','mlai-staging-api-internal','--ip','172.28.240.11','--label','au.mlai.environment=chat-staging-api','--cap-drop','ALL','--security-opt','no-new-privileges:true','--env-file',str(runtime),'--entrypoint','python',image,'-c',check],timeout=180)
        finally:
            runtime.unlink(missing_ok=True)
            try:self.run(['docker','rm','-f',name],timeout=20)
            except Failure:pass

    def install_env(self,path):
        private_file(path);self.write_bytes(self.env,Path(path).read_bytes())

    def activate(self,path):
        values=self.values(path)
        metadata=self.root/'activation-image.json';self.write_bytes(metadata,self.run(['docker','image','inspect',values['STAGING_API_IMAGE_ID']]).encode())
        self.run(['python3',str(self.profile/'config.py'),'check-image','--output',str(path),'--metadata',str(metadata)])
        self.run(['docker','compose','--project-name','mlai-chat-staging-api','--env-file',str(path),'-f',str(self.profile/'compose.yml'),'up','-d','--no-deps','--no-build','--pull','never','--wait','--wait-timeout','90','staging-api'],timeout=150)

    def health(self):
        self.running(self.values(self.env))
        status=self.run(['docker','inspect','mlai-chat-staging-api-staging-api-1','--format','{{.State.Health.Status}}']).strip()
        if status!='healthy':raise Failure('health')
        for host,path in [('chat-staging.mlai.au','/'),('api-staging.mlai.au','/healthz/live')]:
            result=self.run(['curl','--noproxy','*','--fail','--silent','--show-error','--max-time','15','--output','/dev/null','--write-out','%{http_code}','--resolve',host+':443:127.0.0.1','https://'+host+path],timeout=20).strip()
            if result!='200':raise Failure('https-health')

    def mark_failure(self,state,sha,outcome):
        state.update(desired_sha=sha,outcome=outcome,failed_sha=sha,retry_at=self.clock()+600)
        self.save(state)

    def recover(self):
        private_file(self.journal);record=json.loads(self.journal.read_text())
        # Journal paths must remain within our private storage, even after interruption.
        previous=Path(record['previous_env'])
        if previous.resolve().parent!=self.root.resolve() and self.root.resolve() not in previous.resolve().parents:raise Failure('journal-path')
        values=self.values(previous)
        if values['STAGING_API_SOURCE_SHA']!=record['previous_sha'] or values['STAGING_API_IMAGE_ID']!=record['previous_image']:raise Failure('journal-provenance')
        self.install_env(previous);self.activate(self.env);self.health()
        state=record.get('previous_state',self.load());state.update(active_sha=record['previous_sha'],active_image=record['previous_image'],outcome='recovered')
        self.save(state);self.journal.unlink()

    def tick(self):
        self.validate()
        with self.locked() as acquired:
            if not acquired:return 'busy'
            if self.journal.exists():
                try:self.recover()
                except (Failure,ValueError,OSError,KeyError):
                    self.write_bytes(self.paused,b'recovery-failed\n');return 'paused'
            if self.peer_recovery_pending():return 'peer-recovery'
            if self.paused.exists():return 'paused'
            state=self.load();sha=state.get('desired_sha','')
            try:
                sha=self.fetch();state['desired_sha']=sha
                if sha==state['active_sha']:
                    self.running(self.values(self.env));state['outcome']='unchanged';self.save(state);return 'unchanged'
                if sha==state.get('incompatible_sha'):return 'incompatible'
                if sha==state.get('failed_sha') and self.clock()<state.get('retry_at',0):return 'waiting'
                if shutil.disk_usage(self.root).free < 5*1024**3:raise Failure('disk-capacity')
                source=self.checkout(sha);self.compatible(source)
                self.running(self.values(self.env))
                release=self.releases/sha;release.mkdir(mode=0o700,exist_ok=True)
                candidate=release/'candidate.env'
                self.build(sha,source,candidate)
                values=self.values(candidate)
                if values['STAGING_API_SOURCE_SHA']!=sha:raise Failure('candidate-sha')
                if self.paused.exists():
                    state['outcome']='paused';self.save(state);return 'paused'
                if self.fetch()!=sha:
                    state['outcome']='superseded';self.save(state);return 'superseded'
                previous=release/'previous.env';self.write_bytes(previous,self.env.read_bytes())
                old=self.values(previous)
                self.write_json(self.journal,{'previous_env':str(previous),'candidate_env':str(candidate),'previous_sha':old['STAGING_API_SOURCE_SHA'],'previous_image':old['STAGING_API_IMAGE_ID'],'candidate_sha':sha,'candidate_image':values['STAGING_API_IMAGE_ID'],'previous_state':state})
                self.install_env(candidate);self.activate(self.env);self.health()
                state.update(active_sha=sha,active_image=values['STAGING_API_IMAGE_ID'],previous={'sha':old['STAGING_API_SOURCE_SHA'],'image':old['STAGING_API_IMAGE_ID'],'env':str(previous)},outcome='deployed',failed_sha='',retry_at=0,incompatible_sha='')
                self.save(state);self.journal.unlink();self.cleanup();return 'deployed'
            except Incompatible as exc:
                state.update(desired_sha=sha,incompatible_sha=sha,outcome='incompatible',reason=str(exc));self.save(state);return 'incompatible'
            except (Failure,ValueError,OSError,KeyError):
                if self.journal.exists():
                    try:self.recover()
                    except (Failure,ValueError,OSError,KeyError):
                        self.write_bytes(self.paused,b'rollback-failed\n');return 'paused'
                # A failed activation must never advance the successful image/SHA.
                self.mark_failure(self.load(),sha,'failed');return 'failed'

    def pause(self):
        self.validate()
        # Persist before waiting: an in-flight build also checks this before activation.
        self.write_bytes(self.paused,b'operator\n')
        with self.locked(blocking=True,host=False):pass
        return 'paused'

    def resume(self):
        self.validate()
        with self.locked(blocking=True):
            if self.journal.exists():self.recover()
            state=self.load();state.update(failed_sha='',retry_at=0,incompatible_sha='');self.save(state)
            self.paused.unlink(missing_ok=True)
        return 'resumed'

    def rollback(self):
        self.validate();self.write_bytes(self.paused,b'operator-rollback\n')
        with self.locked(blocking=True):
            if self.journal.exists():self.recover()
            state=self.load();previous=state.get('previous')
            if not previous:raise Failure('no-previous-release')
            current=self.root/('manual-rollback-'+state['active_sha']+'.env');self.write_bytes(current,self.env.read_bytes())
            old=self.values(Path(previous['env']));new=self.values(current)
            if old['STAGING_API_SOURCE_SHA']!=previous['sha'] or old['STAGING_API_IMAGE_ID']!=previous['image']:raise Failure('rollback-provenance')
            self.write_json(self.journal,{'previous_env':str(current),'candidate_env':previous['env'],'previous_sha':new['STAGING_API_SOURCE_SHA'],'previous_image':new['STAGING_API_IMAGE_ID'],'candidate_sha':old['STAGING_API_SOURCE_SHA'],'candidate_image':old['STAGING_API_IMAGE_ID'],'previous_state':state})
            try:
                self.install_env(Path(previous['env']));self.activate(self.env);self.health()
                state.update(active_sha=old['STAGING_API_SOURCE_SHA'],active_image=old['STAGING_API_IMAGE_ID'],previous={'sha':new['STAGING_API_SOURCE_SHA'],'image':new['STAGING_API_IMAGE_ID'],'env':str(current)},outcome='rolled-back')
                self.save(state);self.journal.unlink()
            except (Failure,ValueError,OSError,KeyError):self.recover();raise Failure('rollback')
        return 'rolled-back'

    def cleanup(self):
        if not self.releases.exists():return
        state=self.load();keep={state['active_sha'],(state.get('previous') or {}).get('sha','')}
        if self.journal.exists():return
        directories=[p for p in self.releases.iterdir() if SHA.fullmatch(p.name) and p.is_dir() and not p.is_symlink()]
        keep.update(p.name for p in sorted(directories,key=lambda p:p.stat().st_mtime,reverse=True)[:1])
        for path in directories:
            if path.name not in keep:shutil.rmtree(path)
        # Images/volumes are never pruned here; current, previous and base stay available.

    def status(self):
        state=self.load()
        return {key:state.get(key) for key in ['active_sha','active_image','desired_sha','outcome','retry_at','reason']} | {'paused':self.paused.exists(),'recovery_pending':self.journal.exists()}


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command',choices=['tick','status','pause','resume','rollback']);args=parser.parse_args()
    os.umask(0o077)
    try:
        private_file(CONFIG);controller=Controller(json.loads(CONFIG.read_text()))
        # Use only the already installed Hermit environment, never fetched activation scripts.
        if args.command=='status':print(json.dumps(controller.status(),sort_keys=True));return 0
        result=getattr(controller,args.command)()
        state=controller.status();print(json.dumps({'result':result,**state},sort_keys=True))
        return 1 if result in {'failed','paused'} and args.command=='tick' else 0
    except (Failure,ValueError,OSError,KeyError,json.JSONDecodeError):
        print('Staging auto-deploy refused or failed; inspect target, private paths and installed provenance.',file=sys.stderr);return 1

if __name__=='__main__':sys.exit(main())
