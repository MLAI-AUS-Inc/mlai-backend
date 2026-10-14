"""Exercise the deployment lifecycle with real files/Git and fake build boundaries."""
import contextlib
import fcntl
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

SCRIPT=Path(__file__).with_name('auto-deploy.py')
if SCRIPT.exists():
    spec=importlib.util.spec_from_file_location('auto_deploy',SCRIPT)
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
else:module=None

class AutoDeployTests(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(module,'Deployment controller is not implemented')
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name);self.root.chmod(0o700)
        self.state=self.root/'state';self.state.mkdir(mode=0o700);(self.state/'releases').mkdir(mode=0o700)
        self.profile=self.root/'profile';self.profile.mkdir(mode=0o700)
        self.env=self.profile/'.env';self.env.write_text('old-private-config');self.env.chmod(0o600)
        capacity=patch.object(module.shutil,'disk_usage',return_value=type('Disk',(),{'free':100*1024**3})());capacity.start();self.addCleanup(capacity.stop)
        self.now=1000;self.tip='a'*40;self.built=[];self.activations=[];self.bad=set();self.move=None
        self.cfg={'state_dir':str(self.state),'profile':str(self.profile),'repo':'git@github.com:MLAI-AUS-Inc/mlai-backend.git','ref':'refs/heads/staging','hostname':'ubuntu-s-4vcpu-8gb-syd1','machine_id':'f'*32,'source_sha':'c'*40,'shared_lock':str(self.root/'shared.lock'),'peer_state':str(self.root/'peer')}
        self.c=module.Controller(self.cfg,clock=lambda:self.now)
        self.c.validate=lambda:None
        self.c.fetch=lambda:self.tip
        self.c.checkout=lambda sha:self.root
        self.c.compatible=lambda path:None
        self.c.running=lambda values:None
        self.c.health=lambda:None
        self.c.cleanup=lambda:None
        self.c.build=self.build
        self.c.activate=self.activate
        self.c.values=lambda path:self.read_values(path)
        self.c.install_env=self.install_env
        self.c.save({'active_sha':'0'*40,'active_image':'sha256:'+'0'*64,'previous':None,'outcome':'ready'})

    def read_values(self,path):
        if Path(path).read_text()=='old-private-config':
            return {'STAGING_API_SOURCE_SHA':'0'*40,'STAGING_API_IMAGE_ID':'sha256:'+'0'*64}
        return json.loads(Path(path).read_text())

    def install_env(self,path):self.env.write_bytes(Path(path).read_bytes())

    def build(self,sha,path,candidate):
        self.built.append(sha)
        if self.move:self.tip=self.move
        if sha in self.bad:raise module.Failure('build')
        candidate.write_text(json.dumps({'STAGING_API_SOURCE_SHA':sha,'STAGING_API_IMAGE_ID':'sha256:'+sha[:1]*64}));candidate.chmod(0o600)

    def activate(self,path):self.activations.append(self.read_values(path)['STAGING_API_SOURCE_SHA'])

    def test_two_successes_and_unchanged_tip_do_not_rebuild(self):
        self.assertEqual(self.c.tick(),'deployed');self.assertEqual(self.c.tick(),'unchanged')
        self.tip='b'*40;self.assertEqual(self.c.tick(),'deployed')
        state=self.c.load();self.assertEqual(state['active_sha'],'b'*40)
        self.assertEqual(state['previous']['sha'],'a'*40);self.assertEqual(self.built,['a'*40,'b'*40])
        self.assertFalse(self.c.journal.exists())

    def test_shared_lock_and_peer_recovery_prevent_new_work(self):
        with open(self.root/'shared.lock','a') as lock:
            os.chmod(self.root/'shared.lock',0o600);fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
            self.assertEqual(self.c.tick(),'busy')
        peer=self.root/'peer';peer.mkdir();(peer/'activation.json').write_text('{}')
        self.assertEqual(self.c.tick(),'peer-recovery');self.assertFalse(self.built)

    def test_lock_prevents_overlapping_attempts(self):
        with open(self.state/'deploy.lock','a') as lock:
            os.chmod(self.state/'deploy.lock',0o600)
            fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
            self.assertEqual(self.c.tick(),'busy')
        self.assertEqual(self.built,[])

    def test_changed_tip_during_build_skips_candidate_including_force_push(self):
        self.move='b'*40
        self.assertEqual(self.c.tick(),'superseded');self.assertEqual(self.activations,[])
        self.assertEqual(self.env.read_text(),'old-private-config')
        self.move=None;self.assertEqual(self.c.tick(),'deployed')
        self.assertEqual(self.c.load()['active_sha'],'b'*40)

    def test_failure_retry_delay_and_new_tip_bypasses_delay(self):
        self.bad.add('a'*40);self.assertEqual(self.c.tick(),'failed')
        self.assertEqual(self.c.tick(),'waiting');self.assertEqual(self.built,['a'*40])
        self.now=1601;self.assertEqual(self.c.tick(),'failed')
        self.tip='b'*40;self.assertEqual(self.c.tick(),'deployed')
        self.assertEqual(self.built,['a'*40,'a'*40,'b'*40])

    def test_incompatible_tip_is_skipped_until_new_tip(self):
        self.c.compatible=lambda path:(_ for _ in ()).throw(module.Incompatible('runtime'))
        self.assertEqual(self.c.tick(),'incompatible');self.assertEqual(self.c.tick(),'incompatible')
        self.assertEqual(self.built,[]);self.tip='b'*40;self.c.compatible=lambda path:None
        self.assertEqual(self.c.tick(),'deployed')

    def test_failed_activation_rolls_back_without_advancing_active_sha(self):
        def activate(path):
            sha=self.read_values(path)['STAGING_API_SOURCE_SHA'];self.activations.append(sha)
            if sha=='a'*40:raise module.Failure('activation')
        self.c.activate=activate
        self.assertEqual(self.c.tick(),'failed')
        self.assertEqual(self.activations,['a'*40,'0'*40]);self.assertEqual(self.env.read_text(),'old-private-config')
        self.assertEqual(self.c.load()['active_sha'],'0'*40);self.assertFalse(self.c.journal.exists())

    def test_failed_rollback_pauses_and_retains_journal(self):
        self.c.activate=lambda path:(_ for _ in ()).throw(module.Failure('activation'))
        self.assertEqual(self.c.tick(),'paused');self.assertTrue(self.c.paused.exists());self.assertTrue(self.c.journal.exists())
        self.assertEqual(self.c.load()['active_sha'],'0'*40)

    def test_interrupted_attempt_recovers_previous_before_polling(self):
        for installed in (False,True):
            with self.subTest(installed=installed):
                previous=self.state/'previous.env';previous.write_text('old-private-config');previous.chmod(0o600)
                candidate=self.state/'candidate.env';self.build('a'*40,self.root,candidate)
                self.c.write_json(self.c.journal,{'previous_env':str(previous),'candidate_env':str(candidate),'previous_sha':'0'*40,'previous_image':'sha256:'+'0'*64,'candidate_sha':'a'*40,'candidate_image':'sha256:'+'a'*64})
                if installed:self.install_env(candidate)
                self.c.pause();self.assertEqual(self.c.tick(),'paused')
                self.assertEqual(self.env.read_text(),'old-private-config');self.assertFalse(self.c.journal.exists())
                self.assertEqual(self.activations[-1],'0'*40)
                self.c.paused.unlink()

    def test_manual_rollback_remains_paused(self):
        self.c.tick();self.tip='b'*40;self.c.tick()
        self.assertEqual(self.c.rollback(),'rolled-back');self.assertTrue(self.c.paused.exists())
        self.assertEqual(self.c.load()['active_sha'],'a'*40);self.assertEqual(self.c.tick(),'paused')
        self.c.resume();self.assertEqual(self.c.tick(),'deployed')

    def test_health_failure_also_rolls_back(self):
        calls=[]
        def health():
            calls.append(1)
            if len(calls)==1:raise module.Failure('health')
        self.c.health=health
        self.assertEqual(self.c.tick(),'failed');self.assertEqual(self.env.read_text(),'old-private-config')
        self.assertEqual(self.c.load()['active_sha'],'0'*40)

    def test_pause_during_build_prevents_activation(self):
        build=self.c.build
        def pause_build(sha,path,candidate):
            build(sha,path,candidate);self.c.write_bytes(self.c.paused,b'operator')
        self.c.build=pause_build
        self.assertEqual(self.c.tick(),'paused');self.assertEqual(self.activations,[])
        self.assertEqual(self.env.read_text(),'old-private-config')

    def test_interruption_after_success_write_restores_prior_full_state(self):
        original=self.c.load();previous=self.state/'previous.env'
        previous.write_text('old-private-config');previous.chmod(0o600)
        candidate=self.state/'candidate.env';self.build('a'*40,self.root,candidate)
        self.c.write_json(self.c.journal,{'previous_env':str(previous),'candidate_env':str(candidate),'previous_sha':'0'*40,'previous_image':'sha256:'+'0'*64,'candidate_sha':'a'*40,'candidate_image':'sha256:'+'a'*64,'previous_state':original})
        self.c.save({'active_sha':'a'*40,'active_image':'sha256:'+'a'*64,'previous':{'sha':'0'*40,'env':str(previous)}})
        self.c.recover();self.assertIsNone(self.c.load()['previous'])
        self.assertEqual(self.c.load()['active_sha'],'0'*40)

    def test_consecutive_manual_rollbacks_keep_distinct_private_snapshots(self):
        self.c.tick();self.tip='b'*40;self.c.tick()
        self.c.rollback();self.assertEqual(self.c.load()['active_sha'],'a'*40)
        self.c.rollback();self.assertEqual(self.c.load()['active_sha'],'b'*40)
        self.assertTrue(self.c.paused.exists())

    def test_low_disk_blocks_new_build_but_allows_pause_recovery_and_rollback(self):
        low=type('Disk',(),{'free':1024})()
        with patch.object(module.shutil,'disk_usage',return_value=low):
            self.assertEqual(self.c.tick(),'failed');self.assertEqual(self.built,[])
            self.assertEqual(self.c.pause(),'paused');self.c.resume()
        self.c.tick();self.tip='b'*40;self.c.tick()
        with patch.object(module.shutil,'disk_usage',return_value=low):
            self.assertEqual(self.c.rollback(),'rolled-back')
            previous=self.state/'recover.env';previous.write_text('old-private-config');previous.chmod(0o600)
            self.c.write_json(self.c.journal,{'previous_env':str(previous),'previous_sha':'0'*40,'previous_image':'sha256:'+'0'*64})
            self.assertEqual(self.c.tick(),'paused');self.assertFalse(self.c.journal.exists())
            self.assertEqual(self.env.read_text(),'old-private-config')

    def test_wrong_ref_or_host_and_permissive_private_file_refuse(self):
        for field,value in [('ref','refs/heads/main'),('hostname','mlai-chat-prod-01'),('repo','git@github.com:other/repo.git')]:
            with self.subTest(field=field):
                with self.assertRaises(module.Failure):module.validate_target({**self.cfg,field:value})
        private=self.state/'secret';private.write_text('never-log-this');private.chmod(0o644)
        with self.assertRaises(module.Failure):module.private_file(private)
        private.chmod(0o600);module.private_file(private)
        link=self.state/'link';link.symlink_to(private)
        with self.assertRaises(module.Failure):module.private_file(link)

    def test_real_git_compatibility_rejects_operations_and_runtime_but_allows_browser(self):
        repo=self.root/'git';repo.mkdir()
        def git(*args):return subprocess.check_output(['git','-C',str(repo),*args],text=True).strip()
        git('init');git('config','user.name','Test');git('config','user.email','test@example.invalid')
        for name in ['ops/staging-chat/config.py','roo/migrations/0001.py','core/authentication.py','roo/new_function.py','mlai/urls.py']:
            path=repo/name;path.parent.mkdir(parents=True,exist_ok=True);path.write_text('original')
        git('add','.');git('commit','-m','base');base=git('rev-parse','HEAD')
        cfg={**self.cfg,'source_sha':base,'base_source_sha':base,'migration_hashes':{}}
        for name,allowed in [('roo/new_function.py',True),('mlai/urls.py',True),('ops/staging-chat/config.py',False),('roo/migrations/0001.py',False),('core/authentication.py',False)]:
            git('reset','--hard',base);(repo/name).write_text('new');git('add','.');git('commit','-m','change')
            check=module.Controller(cfg)
            if allowed:check.compatible(repo)
            else:
                with self.assertRaises(module.Incompatible):check.compatible(repo)

    def test_exact_branch_fetch_uses_readonly_key_and_pinned_hosts(self):
        cfg={**self.cfg,'ssh_key':'/etc/mlai-backend-staging-auto-deploy/id_ed25519','known_hosts':'/etc/mlai-backend-staging-auto-deploy/known_hosts'}
        check=module.Controller(cfg);check.cache.mkdir();calls=[]
        def run(args,**options):
            calls.append((args,options));return 'a'*40+'\n' if 'rev-parse' in args else ''
        check.run=run;self.assertEqual(check.fetch(),'a'*40)
        fetch=calls[0];self.assertEqual(fetch[0][-2:],['git@github.com:MLAI-AUS-Inc/mlai-backend.git','+refs/heads/staging:refs/heads/staging'])
        ssh=fetch[1]['env']['GIT_SSH_COMMAND'];self.assertIn('StrictHostKeyChecking=yes',ssh);self.assertIn('IdentitiesOnly=yes',ssh)
        self.assertNotIn('Authorization',str(fetch))

    def test_healthy_http_redirect_does_not_count_as_success(self):
        check=module.Controller(self.cfg);check.values=lambda path:{};check.running=lambda values:None
        def run(args,**options):return 'healthy' if args[0]=='docker' else '302'
        check.run=run
        with self.assertRaises(module.Failure):check.health()

    def test_existing_sha_image_is_reused_but_still_checked(self):
        metadata=json.dumps([{'Id':'sha256:'+'a'*64,'Config':{'Labels':{'org.opencontainers.image.revision':'a'*40,'au.mlai.environment':'chat-staging-api'}}}])
        check=module.Controller(self.cfg);calls=[];checked=[]
        check.check_candidate=lambda image,candidate:checked.append(image)
        def run(args,**options):
            calls.append(args)
            if args[:3]==['docker','image','inspect']:return metadata
            return ''
        check.run=run;candidate=self.state/'reuse.env';check.build('a'*40,self.root,candidate)
        self.assertTrue(candidate.exists());self.assertEqual(checked,['sha256:'+'a'*64])
        self.assertFalse(any(a[:2]==['docker','build'] for a in calls))

    def test_runtime_candidate_has_policy_before_secrets_and_schema_check_only(self):
        check=module.Controller({**self.cfg,'dependency_hashes':{}});calls=[]
        values={key:'private' for key in ['STAGING_API_SECRET_KEY','STAGING_API_DATABASE_PASSWORD','STAGING_API_REDIS_PASSWORD','CONNECTOR_CREDENTIAL_KEYS','CONNECTOR_CREDENTIAL_ACTIVE_KEY_ID','COMMUNITY_CHAT_FEDERATION_CLIENT_SECRET','COMMUNITY_CHAT_ADAPTER_TOKEN']}
        check.values=lambda p:values
        check.run=lambda args,**kwargs:calls.append(args) or ''
        candidate=self.state/'candidate.env';check.check_candidate('sha256:'+'a'*64,candidate)
        self.assertIn('network-policy.py',calls[0][1]);self.assertEqual(calls[0][-1],'verify')
        run=calls[1];self.assertEqual(run[run.index('--network')+1],'mlai-staging-api-internal')
        self.assertIn('172.28.240.11',run);self.assertIn('check=True',run[-1]);self.assertIn('dry_run=True',run[-1]);self.assertNotIn("call_command('migrate',interactive",run[-1]);self.assertFalse((self.state/'runtime.env').exists())

    def test_cleanup_preserves_current_previous_and_unowned_directories(self):
        releases=self.state/'releases';cfg={**self.cfg,'state_dir':str(self.state)}
        for i in range(5):
            p=releases/(str(i)*40);p.mkdir();(p/'source').mkdir();os.utime(p,(i,i))
        sentinel=releases/'do-not-delete';sentinel.mkdir()
        self.c.save({'active_sha':'0'*40,'active_image':'sha256:'+'0'*64,'previous':{'sha':'1'*40}})
        check=module.Controller(cfg);check.cleanup()
        self.assertTrue((releases/('0'*40)).exists());self.assertTrue((releases/('1'*40)).exists());self.assertTrue(sentinel.exists())
        self.assertEqual(len([p for p in releases.iterdir() if len(p.name)==40]),3)

if __name__=='__main__':unittest.main()
