#!/usr/bin/env python3
"""Start/repair the pinned current API only after the installed network gate."""
import importlib.util,json,os,sys
from pathlib import Path

def ensure(controller):
    controller.validate()
    with controller.locked() as acquired:
        if not acquired:return 'busy'
        controller.run(['python3',str(controller.profile/'network-policy.py'),'verify'])
        if controller.journal.exists():controller.recover()
        if controller.peer_recovery_pending():return 'peer-recovery'
        if controller.paused.exists() and controller.paused.read_text().strip() in {'rollback-failed','recovery-failed'}:return 'paused'
        controller.activate(controller.env)
        controller.health()
        return 'running'

def main():
    os.umask(0o077)
    installed=Path('/etc/mlai-backend-staging-auto-deploy/auto-deploy.py')
    config=installed.with_name('config.json')
    if installed.is_symlink() or installed.stat().st_uid!=0 or installed.stat().st_mode & 0o022:raise SystemExit('Untrusted installed controller.')
    spec=importlib.util.spec_from_file_location('installed_backend_controller',installed)
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
    module.private_file(config)
    result=ensure(module.Controller(json.loads(config.read_text())))
    print('Staging API readiness: '+result)
    return 1 if result=='paused' else 0
if __name__=='__main__':sys.exit(main())
