#!/usr/bin/env bash

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" >/dev/null 2>&1 && pwd)"
# shellcheck source=common.sh
source "$SCRIPT_DIR/common.sh"

SSH_TARGET="${1:-${LIVE_APP_SSH:-}}"
REMOTE_APP_DIR="${2:-${LIVE_APP_DIR:-~/mlai-backend}}"
REMOTE_ENV_FILE="${3:-${LIVE_APP_ENV_FILE:-.env}}"
REMOTE_COMPOSE_FILE="${4:-${LIVE_APP_COMPOSE_FILE:-docker-compose.yml}}"

if [[ -z "$SSH_TARGET" ]]; then
  cat >&2 <<'EOF'
Usage: scripts/local-live-db/inspect-live-db.sh <ssh-target> [remote-app-dir] [remote-env-file] [remote-compose-file]

Examples:
  scripts/local-live-db/inspect-live-db.sh root@api.mlai.au /srv/mlai-backend .env docker-compose.yml
  LIVE_APP_SSH=root@api.mlai.au scripts/local-live-db/inspect-live-db.sh
EOF
  exit 1
fi

OUT_FILE="$(inspection_file)"

REMOTE_OUTPUT="$(ssh -T "$SSH_TARGET" "python3 - '$REMOTE_APP_DIR' '$REMOTE_ENV_FILE' '$REMOTE_COMPOSE_FILE'" <<'PY'
import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit


def parse_env_file(path: Path) -> dict[str, str]:
    data: dict[str, str] = {}
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        if line.startswith("export "):
            line = line[len("export ") :]
        key, value = line.split("=", 1)
        data[key.strip()] = value.strip().strip("'").strip('"')
    return data


def compose_command() -> list[str]:
    if subprocess.run(
        ["docker", "compose", "version"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    ).returncode == 0:
        return ["docker", "compose"]
    if subprocess.run(
        ["docker-compose", "version"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    ).returncode == 0:
        return ["docker-compose"]
    raise RuntimeError("Neither docker compose nor docker-compose is available on the remote host")


app_dir = Path(sys.argv[1]).expanduser()
env_arg = sys.argv[2]
compose_file_arg = sys.argv[3]
env_path = Path(env_arg)
if not env_path.is_absolute():
    env_path = app_dir / env_path
compose_file = Path(compose_file_arg)
if not compose_file.is_absolute():
    compose_file = app_dir / compose_file

if not env_path.exists():
    raise SystemExit(f"Remote env file not found: {env_path}")
if not compose_file.exists():
    raise SystemExit(f"Remote compose file not found: {compose_file}")

env_data = parse_env_file(env_path)
database_url = env_data.get("DATABASE_URL", "")
ssl_raw = env_data.get("DATABASE_SSL_REQUIRE", "")
database_ssl_require = ssl_raw.lower() == "true" if ssl_raw else None

parsed = urlsplit(database_url) if database_url else None
db_host = parsed.hostname if parsed else None
db_port = parsed.port if parsed else None
db_name = (parsed.path or "").lstrip("/") or env_data.get("POSTGRES_DB")
db_user = parsed.username if parsed else env_data.get("POSTGRES_USER")
db_password = parsed.password if parsed else env_data.get("POSTGRES_PASSWORD")

mode = "unknown"
if db_host == "db":
    mode = "tunneled_live_db"
elif db_host:
    mode = "external_live_db"

result = {
    "inspected_at": datetime.now(timezone.utc).isoformat(),
    "remote_app_dir": str(app_dir),
    "remote_env_file": str(env_path),
    "remote_compose_file": str(compose_file),
    "mode": mode,
    "database_url": database_url,
    "database_ssl_require": database_ssl_require,
    "database_url_redacted": None,
    "postgres": {
        "host": db_host,
        "port": db_port,
        "database": db_name,
        "user": db_user,
        "password_present": bool(db_password),
        "env_postgres_db": env_data.get("POSTGRES_DB"),
        "env_postgres_user": env_data.get("POSTGRES_USER"),
        "env_postgres_password_present": bool(env_data.get("POSTGRES_PASSWORD")),
    },
    "roles": [],
    "db_container_id": None,
    "db_network": None,
}

if parsed and parsed.hostname:
    redacted_netloc = parsed.hostname
    if parsed.port:
        redacted_netloc = f"{redacted_netloc}:{parsed.port}"
    if parsed.username:
        redacted_netloc = f"{parsed.username}:***@{redacted_netloc}"
    result["database_url_redacted"] = parsed._replace(netloc=redacted_netloc).geturl()

compose = compose_command()
db_container_id = subprocess.run(
    [*compose, "-f", str(compose_file), "ps", "-q", "db"],
    cwd=app_dir,
    capture_output=True,
    text=True,
    check=False,
).stdout.strip()

if db_container_id:
    result["db_container_id"] = db_container_id
    networks_raw = subprocess.run(
        ["docker", "inspect", "-f", "{{json .NetworkSettings.Networks}}", db_container_id],
        capture_output=True,
        text=True,
        check=False,
    ).stdout.strip()
    if networks_raw:
        networks = json.loads(networks_raw)
        if networks:
            result["db_network"] = next(iter(networks))

if mode == "tunneled_live_db" and db_container_id and db_user and db_name:
    exec_cmd = ["docker", "exec", db_container_id]
    if db_password:
        exec_cmd.extend(["env", f"PGPASSWORD={db_password}"])
    exec_cmd.extend(
        [
            "psql",
            "-U",
            db_user,
            "-d",
            db_name,
            "-Atc",
            "SELECT rolname FROM pg_roles ORDER BY rolname;",
        ]
    )
    role_run = subprocess.run(exec_cmd, capture_output=True, text=True, check=False)
    if role_run.returncode == 0:
        result["roles"] = [line for line in role_run.stdout.splitlines() if line]
    else:
        result["roles_error"] = role_run.stderr.strip() or role_run.stdout.strip()

print(json.dumps(result, indent=2, sort_keys=True))
PY
)"

python3 - "$OUT_FILE" "$SSH_TARGET" "$REMOTE_OUTPUT" <<'PY'
import json
import sys

out_path = sys.argv[1]
ssh_target = sys.argv[2]
payload = json.loads(sys.argv[3])
payload["remote_host"] = ssh_target

with open(out_path, "w", encoding="utf-8") as handle:
    json.dump(payload, handle, indent=2, sort_keys=True)
    handle.write("\n")

print(f"Wrote {out_path}")
print(f"Mode: {payload.get('mode')}")
print(f"Database URL: {payload.get('database_url_redacted') or '<missing>'}")
print(f"SSL required: {payload.get('database_ssl_require')}")
if payload.get("roles"):
    print("Roles:")
    for role in payload["roles"]:
        print(f"  - {role}")
PY
