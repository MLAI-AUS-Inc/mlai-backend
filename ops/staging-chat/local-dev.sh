#!/bin/sh
set -eu
cd "$(dirname "$0")/../.."
case "${1:-}" in check|up|down) ;; *) echo 'Usage: local-dev.sh {check,up,down}' >&2; exit 2;; esac
# This file is separate from .env and every hosted configuration. Never source it.
env_file=.scratch/staging-local.env
if [ ! -f "$env_file" ]; then
  umask 077
  mkdir -p .scratch
  .venv/bin/python - <<'PY'
from pathlib import Path
import secrets
password=secrets.token_hex(32);redis=secrets.token_hex(32)
values=dict(APP_ENV='staging-local',DJANGO_SETTINGS_MODULE='mlai.settings_staging_local',SECRET_KEY=secrets.token_hex(48),DEBUG='false',LOCAL_DATABASE_PASSWORD=password,LOCAL_REDIS_PASSWORD=redis,DATABASE_URL=f'postgresql://local_mlai:{password}@127.0.0.1:65450/local_mlai',REDIS_URL=f'redis://:{redis}@127.0.0.1:65451/0')
p=Path('.scratch/staging-local.env')
with p.open('x') as f:f.write(''.join(f'{k}={v}\n' for k,v in values.items()))
p.chmod(0o600)
PY
fi
if [ "$1" = down ]; then docker compose --env-file "$env_file" -f ops/staging-chat/compose.local.yml down; exit; fi
if [ "$1" = up ]; then docker compose --env-file "$env_file" -f ops/staging-chat/compose.local.yml up -d; fi
# Env is parsed as data; no eval/source, no hosted private files, no migrations applied.
exec .venv/bin/python - "$1" <<'PY'
import os,stat,sys,subprocess
from pathlib import Path
p=Path('.scratch/staging-local.env')
if p.is_symlink() or p.stat().st_uid!=os.geteuid() or stat.S_IMODE(p.stat().st_mode)!=0o600:raise SystemExit('Local env must be private and operator-owned.')
values=dict(line.split('=',1) for line in p.read_text().splitlines() if line)
allowed={'APP_ENV','DJANGO_SETTINGS_MODULE','SECRET_KEY','DEBUG','LOCAL_DATABASE_PASSWORD','LOCAL_REDIS_PASSWORD','DATABASE_URL','REDIS_URL'}
if set(values)!=allowed or values['APP_ENV']!='staging-local' or values['DJANGO_SETTINGS_MODULE']!='mlai.settings_staging_local':raise SystemExit('Unexpected local configuration.')
env={key:value for key,value in os.environ.items() if key in {'PATH','HOME','LANG','TMPDIR'}};env.update(values)
# Existing schema only: an empty database stops here until explicit approval.
subprocess.run([sys.executable,'manage.py','migrate','--check','--noinput'],env=env,check=True)
subprocess.run([sys.executable,'manage.py','check'],env=env,check=True)
if sys.argv[1]=='up':os.execve(sys.executable,[sys.executable,'manage.py','runserver','127.0.0.1:8000','--noreload'],env)
PY
