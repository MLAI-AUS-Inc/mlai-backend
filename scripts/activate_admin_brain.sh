#!/usr/bin/env bash
# Invoked only by the separately requested production activation workflow.
set -Eeuo pipefail

expected_release="${1:-}"
if [[ ! "$expected_release" =~ ^[a-f0-9]{40}$ ]]; then
    echo "Activation requires the full running release SHA." >&2
    exit 64
fi
task_project_dir="${MLAI_BACKEND_PROJECT_DIR:-/root/mlai-backend}"
task_operations_dir="${MLAI_BACKEND_OPERATIONS_DIR:-/root/mlai-backend-operations}"
cd "$task_project_dir"
compose=(docker compose -f "$task_project_dir/docker-compose.yml")
test -f .env
running_release="$("${compose[@]}" exec -T web python -c 'import os; print(os.environ.get("APP_RELEASE", ""))')"
if [[ "$running_release" != "$expected_release" ]]; then
    echo "Running release differs; activation made no changes." >&2
    exit 1
fi
# Refuse a Compose override that could apply migrations on a web restart.
"${compose[@]}" config --format json | python3 -c '
import json, sys
value = json.load(sys.stdin)["services"]["web"]["environment"].get("RUN_MIGRATIONS_ON_START")
if str(value) != "0":
    raise SystemExit("Activation requires migration-disabled web startup.")
'

set_query_api() {
    python3 - "$task_project_dir/.env" "$1" <<'PY'
import os
from pathlib import Path
import sys
import tempfile
path, value = Path(sys.argv[1]), sys.argv[2]
if value not in {"true", "false"}:
    raise SystemExit("Invalid query API state")
lines = [line for line in path.read_bytes().splitlines(keepends=True)
         if not line.startswith(b"ORG_MEMORY_QUERY_API_ENABLED=")]
raw = b"".join(lines)
if raw and not raw.endswith((b"\n", b"\r")):
    raw += b"\n"
raw += b"ORG_MEMORY_QUERY_API_ENABLED=" + value.encode() + b"\n"
descriptor, name = tempfile.mkstemp(prefix=".env.admin-brain-", dir=path.parent)
try:
    with os.fdopen(descriptor, "wb") as stream:
        stream.write(raw)
        stream.flush()
        os.fsync(stream.fileno())
    os.chmod(name, 0o600)
    os.replace(name, path)
finally:
    Path(name).unlink(missing_ok=True)
PY
}
restart_web() {
    "${compose[@]}" up -d --no-deps --no-build --pull never --force-recreate web
}
finish_activation() {
    task_exit_code=$?
    trap - EXIT
    if [ "$task_exit_code" != "0" ]; then
        set +e
        if set_query_api false && restart_web; then
            echo "Admin Brain activation failed; query API disabled. Review the failed activation checks." >&2
        else
            echo "Admin Brain activation failed; could not confirm the disabled runtime. Operator attention is required." >&2
        fi
    fi
    exit "$task_exit_code"
}
trap finish_activation EXIT
trap 'exit 1' HUP INT TERM

approval_manifest="$task_operations_dir/pilot-approval.json"
operators_file="$task_operations_dir/pilot-operators"
test -s "$approval_manifest"
test -s "$operators_file"
stage_operator="$(sed -n '1p' "$operators_file")"
activation_operator="$(sed -n '2p' "$operators_file")"
test -n "$stage_operator"
test -n "$activation_operator"
test "$stage_operator" != "$activation_operator"
allowlist_key_version="$(sed -n 's/^ORG_MEMORY_PILOT_ALLOWLIST_KEY_VERSION=//p' .env)"
[[ "$allowlist_key_version" =~ ^[A-Za-z0-9._-]{1,80}$ ]]
approval_hash="$(python3 - "$approval_manifest" <<'PY'
import hashlib, json, pathlib, sys
value = json.loads(pathlib.Path(sys.argv[1]).read_text())
print(hashlib.sha256(json.dumps(value, ensure_ascii=True, allow_nan=False,
    sort_keys=True, separators=(",", ":")).encode()).hexdigest())
PY
)"
binding_prefix="production-${allowlist_key_version}-${approval_hash:0:32}"
run_pilot_command() {
    "${compose[@]}" run -T --rm --no-deps --pull never --entrypoint python \
        -e ORG_MEMORY_QUERY_API_ENABLED=true \
        -v "$approval_manifest:/run/org-memory-pilot-approval.json:ro" \
        web manage.py "$@" </dev/null
}

# These remain strict. This workflow does not repair queues, alter governance,
# approve actors/providers, reprocess evidence, build images, or apply migrations.
run_pilot_command stage_org_memory_pilot --organization-domain mlai.au \
    --approval-manifest /run/org-memory-pilot-approval.json --operator-email "$stage_operator" \
    --idempotency-key "${binding_prefix}-stage" --environment production --apply
run_pilot_command activate_org_memory_pilot --organization-domain mlai.au \
    --approval-manifest /run/org-memory-pilot-approval.json --operator-email "$activation_operator" \
    --idempotency-key "${binding_prefix}-activate" --environment production --apply
run_pilot_command check_org_memory_pilot_release_gate --organization-domain mlai.au --require-active
run_pilot_command report_org_memory_pilot_deployment --organization-domain mlai.au --fail-if-ineffective
run_pilot_command check_org_memory_pilot_access_matrix --organization-domain mlai.au \
    --approval-manifest /run/org-memory-pilot-approval.json

set_query_api true
restart_web
"${compose[@]}" exec -T web python manage.py check_org_memory_pilot_release_gate \
    --organization-domain mlai.au --require-active
echo "Admin Brain activation verified for the running release."
