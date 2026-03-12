#!/usr/bin/env bash

set -euo pipefail

local_live_db_repo_root() {
  cd "$(dirname "${BASH_SOURCE[0]}")/../.." >/dev/null 2>&1
  pwd
}

content_factory_repo_root() {
  cd "$(local_live_db_repo_root)/../content-factory" >/dev/null 2>&1
  pwd
}

inspection_file() {
  printf '%s/.live-db-inspection.json\n' "$(local_live_db_repo_root)"
}

control_socket_path() {
  printf '%s/.local-live-db-ssh.sock\n' "$(local_live_db_repo_root)"
}

remote_bridge_name() {
  python3 - <<'PY'
import getpass
import os
import re

user = os.environ.get("USER") or getpass.getuser() or "user"
safe = re.sub(r"[^A-Za-z0-9_.-]+", "-", user).strip("-") or "user"
print(f"mlai-live-db-bridge-{safe}")
PY
}

require_file() {
  local path="$1"
  if [[ ! -f "$path" ]]; then
    printf 'Missing required file: %s\n' "$path" >&2
    exit 1
  fi
}

json_field() {
  local path="$1"
  local dotted_key="$2"
  python3 - "$path" "$dotted_key" <<'PY'
import json
import sys

path = sys.argv[1]
key = sys.argv[2]

with open(path, encoding="utf-8") as handle:
    value = json.load(handle)

for part in key.split("."):
    if isinstance(value, dict):
        value = value.get(part)
    else:
        value = None
        break

if value is None:
    sys.exit(1)

if isinstance(value, bool):
    print("true" if value else "false")
elif isinstance(value, (dict, list)):
    print(json.dumps(value))
else:
    print(value)
PY
}

env_field() {
  local path="$1"
  local key="$2"
  python3 - "$path" "$key" <<'PY'
import sys

path = sys.argv[1]
key = sys.argv[2]

with open(path, encoding="utf-8") as handle:
    for raw_line in handle:
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        current_key, value = line.split("=", 1)
        current_key = current_key.strip()
        if current_key == key:
            print(value.strip())
            break
PY
}

port_is_listening() {
  local port="$1"
  python3 - "$port" <<'PY'
import socket
import sys

port = int(sys.argv[1])

with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
    sock.settimeout(0.5)
    result = sock.connect_ex(("127.0.0.1", port))

sys.exit(0 if result == 0 else 1)
PY
}
