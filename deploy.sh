#!/bin/bash
set -euo pipefail


# Configuration
DROPLET_IP="209.38.85.60"
# GitHub-hosted runners do not have the developer workstation's `mlai-droplet`
# SSH alias. Keep local overrides supported, but default to the resolvable host
# already added to known_hosts by the deployment workflow.
DEPLOY_SSH_TARGET="${DEPLOY_SSH_TARGET:-root@$DROPLET_IP}"
PROJECT_DIR="/root/mlai-backend"
APP_RELEASE="${APP_RELEASE:-$(git rev-parse --short=12 HEAD 2>/dev/null || date +%Y%m%d%H%M)}"
APP_RELEASE_SHORT="${APP_RELEASE:0:12}"
MEETING_ROOM_BOOKING_ENABLED="${MEETING_ROOM_BOOKING_ENABLED:-false}"
OFFICE_MANAGER_ENABLED="${OFFICE_MANAGER_ENABLED:-false}"
OFFICE_MANAGER_TIMEZONE="${OFFICE_MANAGER_TIMEZONE:-Australia/Melbourne}"
COMMUNITY_BRIDGE_PRODUCTION_ENABLED="${COMMUNITY_BRIDGE_PRODUCTION_ENABLED:-false}"
ORG_MEMORY_PRODUCTION_DEPLOY_ENABLED="${ORG_MEMORY_PRODUCTION_DEPLOY_ENABLED:-false}"
ORG_MEMORY_PRODUCTION_PUBLIC_CHANNEL_ADMIN_SCOPE_APPROVED="${ORG_MEMORY_PRODUCTION_PUBLIC_CHANNEL_ADMIN_SCOPE_APPROVED:-false}"
LINEAR_CHANNEL_ISSUE_MAX_COMMENTS="${LINEAR_CHANNEL_ISSUE_MAX_COMMENTS:-250}"

case "$MEETING_ROOM_BOOKING_ENABLED" in
    true|TRUE|True|1|yes|YES|Yes|on|ON|On) MEETING_ROOM_BOOKING_ENABLED=true ;;
    false|FALSE|False|0|no|NO|No|off|OFF|Off|"") MEETING_ROOM_BOOKING_ENABLED=false ;;
    *)
        echo "❌ MEETING_ROOM_BOOKING_ENABLED must be true or false."
        exit 1
        ;;
esac
export MEETING_ROOM_BOOKING_ENABLED

case "$OFFICE_MANAGER_ENABLED" in
    true|TRUE|True|1|yes|YES|Yes|on|ON|On) OFFICE_MANAGER_ENABLED=true ;;
    false|FALSE|False|0|no|NO|No|off|OFF|Off|"") OFFICE_MANAGER_ENABLED=false ;;
    *)
        echo "❌ OFFICE_MANAGER_ENABLED must be true or false."
        exit 1
        ;;
esac
export OFFICE_MANAGER_ENABLED

case "$COMMUNITY_BRIDGE_PRODUCTION_ENABLED" in
    true|TRUE|True|1|yes|YES|Yes|on|ON|On) COMMUNITY_BRIDGE_PRODUCTION_ENABLED=true ;;
    false|FALSE|False|0|no|NO|No|off|OFF|Off|"") COMMUNITY_BRIDGE_PRODUCTION_ENABLED=false ;;
    *)
        echo "❌ COMMUNITY_BRIDGE_PRODUCTION_ENABLED must be true or false."
        exit 1
        ;;
esac
export COMMUNITY_BRIDGE_PRODUCTION_ENABLED

case "$ORG_MEMORY_PRODUCTION_DEPLOY_ENABLED" in
    true|TRUE|True|1|yes|YES|Yes|on|ON|On) ORG_MEMORY_PRODUCTION_DEPLOY_ENABLED=true ;;
    false|FALSE|False|0|no|NO|No|off|OFF|Off|"") ORG_MEMORY_PRODUCTION_DEPLOY_ENABLED=false ;;
    *)
        echo "❌ ORG_MEMORY_PRODUCTION_DEPLOY_ENABLED must be true or false."
        exit 1
        ;;
esac
export ORG_MEMORY_PRODUCTION_DEPLOY_ENABLED

case "$ORG_MEMORY_PRODUCTION_PUBLIC_CHANNEL_ADMIN_SCOPE_APPROVED" in
    true|TRUE|True|1|yes|YES|Yes|on|ON|On) ORG_MEMORY_PRODUCTION_PUBLIC_CHANNEL_ADMIN_SCOPE_APPROVED=true ;;
    false|FALSE|False|0|no|NO|No|off|OFF|Off|"") ORG_MEMORY_PRODUCTION_PUBLIC_CHANNEL_ADMIN_SCOPE_APPROVED=false ;;
    *)
        echo "❌ ORG_MEMORY_PRODUCTION_PUBLIC_CHANNEL_ADMIN_SCOPE_APPROVED must be true or false."
        exit 1
        ;;
esac
export ORG_MEMORY_PRODUCTION_PUBLIC_CHANNEL_ADMIN_SCOPE_APPROVED

if [ -z "${REDIS_URL:-}" ]; then
    echo "❌ REDIS_URL must be supplied by the deployment secret store."
    exit 1
fi
for service_secret_name in ROO_API_KEY INTERNAL_API_KEY; do
    service_secret_value="${!service_secret_name:-}"
    if [ "${#service_secret_value}" -lt 32 ]; then
        echo "❌ ${service_secret_name} must be supplied by the deployment secret store and contain at least 32 characters."
        exit 1
    fi
done
if [ "$ROO_API_KEY" = "$INTERNAL_API_KEY" ]; then
    echo "❌ ROO_API_KEY and INTERNAL_API_KEY must be distinct trust-domain credentials."
    exit 1
fi
if [[ ! "${OFFICE_MANAGER_SLACK_BOT_TOKEN:-}" =~ ^xoxb-[A-Za-z0-9-]+$ ]]; then
    echo "❌ OFFICE_MANAGER_SLACK_BOT_TOKEN must be retained for durable Office Manager recovery."
    exit 1
fi
if [[ ! "${OFFICE_MANAGER_SLACK_CHANNEL_ID:-}" =~ ^C[A-Z0-9]+$ ]]; then
    echo "❌ OFFICE_MANAGER_SLACK_CHANNEL_ID must be retained for durable Office Manager recovery."
    exit 1
fi
if [ "$OFFICE_MANAGER_TIMEZONE" != "Australia/Melbourne" ]; then
    echo "❌ OFFICE_MANAGER_TIMEZONE must match the durable Office Manager contract: Australia/Melbourne."
    exit 1
fi
for chat_secret_name in \
    COMMUNITY_CHAT_EMAIL_CODE_PEPPER \
    COMMUNITY_CHAT_EMAIL_CODE_DELIVERY_SECRET \
    COMMUNITY_CHAT_ADAPTER_TOKEN; do
    chat_secret_value="${!chat_secret_name:-}"
    if [ "${#chat_secret_value}" -lt 32 ]; then
        echo "❌ ${chat_secret_name} must be supplied by the deployment secret store and contain at least 32 characters."
        exit 1
    fi
done
if [ "$COMMUNITY_CHAT_EMAIL_CODE_PEPPER" = "$COMMUNITY_CHAT_EMAIL_CODE_DELIVERY_SECRET" ] \
    || [ "$COMMUNITY_CHAT_EMAIL_CODE_PEPPER" = "$COMMUNITY_CHAT_ADAPTER_TOKEN" ] \
    || [ "$COMMUNITY_CHAT_EMAIL_CODE_DELIVERY_SECRET" = "$COMMUNITY_CHAT_ADAPTER_TOKEN" ]; then
    echo "❌ MLAI Chat email and membership-adapter secrets must be independent."
    exit 1
fi
if [ -z "${COMMUNITY_CHAT_ADAPTER_URL:-}" ]; then
    echo "❌ COMMUNITY_CHAT_ADAPTER_URL must be supplied by the deployment configuration."
    exit 1
fi
python3 - <<'PY'
import ipaddress
import os
from urllib.parse import urlparse

parsed = urlparse(os.environ["COMMUNITY_CHAT_ADAPTER_URL"])
if parsed.scheme != "http" or parsed.username or parsed.password or parsed.query or parsed.fragment:
    raise SystemExit("COMMUNITY_CHAT_ADAPTER_URL must be a credential-free private HTTP URL")
if parsed.path not in {"", "/"} or parsed.port != 3100:
    raise SystemExit("COMMUNITY_CHAT_ADAPTER_URL must use the private membership adapter port 3100")
address = ipaddress.ip_address(parsed.hostname or "")
if not (address.is_private or address.is_loopback):
    raise SystemExit("COMMUNITY_CHAT_ADAPTER_URL must use a private or loopback IP address")
PY
bridge_present=0
if [ "$COMMUNITY_BRIDGE_PRODUCTION_ENABLED" = "true" ]; then
    bridge_values=(
        "${SLACK_BRIDGE_BOT_TOKEN:-}"
        "${SLACK_BRIDGE_SIGNING_SECRET:-}"
        "${SLACK_BRIDGE_BOT_USER_ID:-}"
        "${SLACK_BRIDGE_WORKSPACE_ID:-}"
        "${SLACK_BRIDGE_CHANNEL_ID:-}"
        "${SLACK_BRIDGE_CHANNEL_NAME:-}"
        "${BUZZ_BRIDGE_ADAPTER_URL:-}"
        "${BUZZ_BRIDGE_ADAPTER_TOKEN:-}"
        "${BUZZ_BRIDGE_CALLBACK_SECRET:-}"
        "${BUZZ_BRIDGE_DESTINATION_WORKSPACE_ID:-}"
        "${BUZZ_BRIDGE_DESTINATION_CHANNEL_ID:-}"
        "${BUZZ_BRIDGE_DESTINATION_CHANNEL_NAME:-}"
    )
    for bridge_value in "${bridge_values[@]}"; do
        [ -n "$bridge_value" ] && bridge_present=$((bridge_present + 1))
    done
    if [ "$bridge_present" -ne "${#bridge_values[@]}" ]; then
        echo "❌ Slack and Buzz bridge settings must be fully configured when the production bridge is enabled."
        exit 1
    fi
    if [ "${#SLACK_BRIDGE_SIGNING_SECRET}" -lt 32 ] \
        || [ "${#BUZZ_BRIDGE_ADAPTER_TOKEN}" -lt 32 ] \
        || [ "${#BUZZ_BRIDGE_CALLBACK_SECRET}" -lt 32 ]; then
        echo "❌ Slack signing, bridge adapter, and bridge callback secrets must each contain at least 32 characters."
        exit 1
    fi
    python3 scripts/validate_community_bridge_adapter_url.py "$BUZZ_BRIDGE_ADAPTER_URL"
    python3 - <<'PY'
import os
import re

if not re.fullmatch(r"T[A-Z0-9]+", os.environ["SLACK_BRIDGE_WORKSPACE_ID"]):
    raise SystemExit("SLACK_BRIDGE_WORKSPACE_ID must be a Slack workspace ID")
if not re.fullmatch(r"C[A-Z0-9]+", os.environ["SLACK_BRIDGE_CHANNEL_ID"]):
    raise SystemExit("SLACK_BRIDGE_CHANNEL_ID must be a public Slack channel ID")
if not re.fullmatch(r"[a-z0-9_-]{1,80}", os.environ["SLACK_BRIDGE_CHANNEL_NAME"]):
    raise SystemExit("SLACK_BRIDGE_CHANNEL_NAME must be a valid public channel name")
if os.environ["BUZZ_BRIDGE_DESTINATION_WORKSPACE_ID"] != "chat.mlai.au":
    raise SystemExit("BUZZ_BRIDGE_DESTINATION_WORKSPACE_ID must be chat.mlai.au")
if not re.fullmatch(
    r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}",
    os.environ["BUZZ_BRIDGE_DESTINATION_CHANNEL_ID"],
):
    raise SystemExit("BUZZ_BRIDGE_DESTINATION_CHANNEL_ID must be a lowercase UUID")
if not re.fullmatch(r"[a-z0-9_-]{1,80}", os.environ["BUZZ_BRIDGE_DESTINATION_CHANNEL_NAME"]):
    raise SystemExit("BUZZ_BRIDGE_DESTINATION_CHANNEL_NAME must be a valid channel name")
PY
else
    echo "ℹ️ Community bridge production activation is disabled; staged bridge settings will not be installed."
fi
if [ -z "${CONNECTOR_CREDENTIAL_KEYS:-}" ]; then
    echo "❌ CONNECTOR_CREDENTIAL_KEYS must be supplied by the deployment secret store."
    exit 1
fi
if [ -z "${CONNECTOR_CREDENTIAL_ACTIVE_KEY_ID:-}" ]; then
    echo "❌ CONNECTOR_CREDENTIAL_ACTIVE_KEY_ID must be supplied by the deployment secret store."
    exit 1
fi
if [[ ! "$CONNECTOR_CREDENTIAL_ACTIVE_KEY_ID" =~ ^[A-Za-z0-9_-]{1,32}$ ]]; then
    echo "❌ CONNECTOR_CREDENTIAL_ACTIVE_KEY_ID has an invalid format."
    exit 1
fi
python3 - <<'PY'
import json
import os

keys = json.loads(os.environ["CONNECTOR_CREDENTIAL_KEYS"])
active = os.environ["CONNECTOR_CREDENTIAL_ACTIVE_KEY_ID"]
if not isinstance(keys, dict) or not keys or active not in keys:
    raise SystemExit("Connector credential keyring secrets are inconsistent")
PY
if [[ "$REDIS_URL" != redis://* && "$REDIS_URL" != rediss://* ]]; then
    echo "❌ REDIS_URL must use the redis:// or rediss:// scheme."
    exit 1
fi
if [ -z "${ROO_SIM_PATIENT_KEY:-}" ]; then
    echo "❌ ROO_SIM_PATIENT_KEY must be supplied by the deployment secret store."
    exit 1
fi
if [ "${#ROO_SIM_PATIENT_KEY}" -lt 32 ]; then
    echo "❌ ROO_SIM_PATIENT_KEY must contain at least 32 characters."
    exit 1
fi
if [ -z "${VICTOR_AI_ROO_SIGNING_SECRET:-}" ]; then
    echo "❌ VICTOR_AI_ROO_SIGNING_SECRET must be supplied by the deployment secret store."
    exit 1
fi
if [ "${#VICTOR_AI_ROO_SIGNING_SECRET}" -lt 32 ]; then
    echo "❌ VICTOR_AI_ROO_SIGNING_SECRET must contain at least 32 characters."
    exit 1
fi
if [ "$VICTOR_AI_ROO_SIGNING_SECRET" = "$ROO_SIM_PATIENT_KEY" ]; then
    echo "❌ Victor AI and simulated-patient credentials must be distinct."
    exit 1
fi
python3 scripts/validate_linear_channel_issue_deploy_config.py
if [ "$ORG_MEMORY_PRODUCTION_DEPLOY_ENABLED" = "true" ]; then
    if [[ ! "${ORG_MEMORY_PILOT_ALLOWLIST_KEY_VERSION:-}" =~ ^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$ ]]; then
        echo "❌ ORG_MEMORY_PILOT_ALLOWLIST_KEY_VERSION must be supplied in the required format."
        exit 1
    fi
    if [ -z "${ORG_MEMORY_PILOT_ALLOWLIST_HMAC_KEY:-}" ] || [ "${#ORG_MEMORY_PILOT_ALLOWLIST_HMAC_KEY}" -lt 32 ]; then
        echo "❌ ORG_MEMORY_PILOT_ALLOWLIST_HMAC_KEY must contain at least 32 characters."
        exit 1
    fi
    if [ -z "${ORG_MEMORY_PRODUCTION_APPROVAL_MANIFEST:-}" ]; then
        echo "❌ ORG_MEMORY_PRODUCTION_APPROVAL_MANIFEST must be supplied by the deployment secret store."
        exit 1
    fi
    if [ -z "${ORG_MEMORY_PRODUCTION_STAGE_OPERATOR_EMAIL:-}" ] || [ -z "${ORG_MEMORY_PRODUCTION_ACTIVATION_OPERATOR_EMAIL:-}" ]; then
        echo "❌ Both Admin Brain production operator emails must be supplied by the deployment secret store."
        exit 1
    fi
    if [ "$ORG_MEMORY_PRODUCTION_STAGE_OPERATOR_EMAIL" = "$ORG_MEMORY_PRODUCTION_ACTIVATION_OPERATOR_EMAIL" ]; then
        echo "❌ Admin Brain production staging and activation operators must be distinct."
        exit 1
    fi
    approval_resolver_args=()
    if [ "$ORG_MEMORY_PRODUCTION_PUBLIC_CHANNEL_ADMIN_SCOPE_APPROVED" = "true" ]; then
        approval_resolver_args+=(--approve-public-admin-scope)
    fi
    ORG_MEMORY_PRODUCTION_APPROVAL_MANIFEST="$(
        printf '%s' "$ORG_MEMORY_PRODUCTION_APPROVAL_MANIFEST" \
            | python3 scripts/resolve_org_memory_production_approval.py "${approval_resolver_args[@]}"
    )"
    export ORG_MEMORY_PRODUCTION_APPROVAL_MANIFEST
    unset approval_resolver_args
    python3 - <<'PY'
import json
import os

manifest = json.loads(os.environ["ORG_MEMORY_PRODUCTION_APPROVAL_MANIFEST"])
if not isinstance(manifest, dict):
    raise SystemExit("Admin Brain production approval must be a JSON object")
if manifest.get("organization_domain") != "mlai.au":
    raise SystemExit("Admin Brain production approval must target mlai.au")
PY
fi
echo "🚀 Deploying release $APP_RELEASE to $DEPLOY_SSH_TARGET ($DROPLET_IP)..."

# 1. Sync files to the server
echo "📦 Syncing files..."
rsync -avz --delete --exclude 'venv' --exclude '.git' --exclude '__pycache__' --exclude '.env' . "$DEPLOY_SSH_TARGET:$PROJECT_DIR"

install_remote_env_secret() {
    local key="$1"
    local value="$2"
    printf '%s' "$value" \
        | ssh "$DEPLOY_SSH_TARGET" "$PROJECT_DIR/scripts/upsert_env_secret_from_stdin.sh $key"
}

echo "🔐 Updating MLAI Chat credentials (values redacted)..."
install_remote_env_secret COMMUNITY_CHAT_EMAIL_CODE_PEPPER "$COMMUNITY_CHAT_EMAIL_CODE_PEPPER"
install_remote_env_secret COMMUNITY_CHAT_EMAIL_CODE_DELIVERY_SECRET "$COMMUNITY_CHAT_EMAIL_CODE_DELIVERY_SECRET"
install_remote_env_secret COMMUNITY_CHAT_ADAPTER_TOKEN "$COMMUNITY_CHAT_ADAPTER_TOKEN"
echo "🔐 Updating Linear channel issue credential (value redacted)..."
install_remote_env_secret LINEAR_API_KEY "$LINEAR_API_KEY"
echo "🔐 Updating distinct Roo and internal service credentials (values redacted)..."
install_remote_env_secret ROO_API_KEY "$ROO_API_KEY"
install_remote_env_secret INTERNAL_API_KEY "$INTERNAL_API_KEY"
echo "🔐 Updating Public Roo Office Manager Slack credential (value redacted)..."
install_remote_env_secret OFFICE_MANAGER_SLACK_BOT_TOKEN "$OFFICE_MANAGER_SLACK_BOT_TOKEN"
case "${LINEAR_CHANNEL_ISSUE_WRITES_ENABLED:-false}" in
    1|[Tt][Rr][Uu][Ee]|[Yy][Ee][Ss]|[Oo][Nn])
        linear_channel_writes_enabled_normalized="true"
        ;;
    *)
        linear_channel_writes_enabled_normalized="false"
        ;;
esac
if [ "$bridge_present" -gt 0 ]; then
    install_remote_env_secret SLACK_BRIDGE_BOT_TOKEN "$SLACK_BRIDGE_BOT_TOKEN"
    install_remote_env_secret SLACK_BRIDGE_SIGNING_SECRET "$SLACK_BRIDGE_SIGNING_SECRET"
    install_remote_env_secret BUZZ_BRIDGE_ADAPTER_TOKEN "$BUZZ_BRIDGE_ADAPTER_TOKEN"
    install_remote_env_secret BUZZ_BRIDGE_CALLBACK_SECRET "$BUZZ_BRIDGE_CALLBACK_SECRET"
fi

install_remote_env_value() {
    local key="$1"
    local value="$2"
    printf '%s' "$value" \
        | ssh "$DEPLOY_SSH_TARGET" "$PROJECT_DIR/scripts/upsert_env_value_from_stdin.sh $key"
}

echo "🔧 Updating Linear channel issue reader configuration..."
install_remote_env_value LINEAR_MEETING_REQUIRED_TEAM_KEYS "$LINEAR_MEETING_REQUIRED_TEAM_KEYS"
install_remote_env_value LINEAR_CHANNEL_ISSUE_BINDINGS_JSON "$LINEAR_CHANNEL_ISSUE_BINDINGS_JSON"
install_remote_env_value LINEAR_CHANNEL_ISSUE_MAX_COMMENTS "$LINEAR_CHANNEL_ISSUE_MAX_COMMENTS"
if [ -n "${OFFICE_MANAGER_SLACK_CHANNEL_ID:-}" ]; then
    echo "🔧 Updating Office Manager Slack channel and timezone..."
    install_remote_env_value OFFICE_MANAGER_SLACK_CHANNEL_ID "$OFFICE_MANAGER_SLACK_CHANNEL_ID"
    install_remote_env_value OFFICE_MANAGER_TIMEZONE "$OFFICE_MANAGER_TIMEZONE"
fi
install_remote_env_value LINEAR_CHANNEL_ISSUE_WRITES_ENABLED "$linear_channel_writes_enabled_normalized"

# Send the credential over SSH stdin rather than a command-line argument. The
# remote shell updates .env using builtins, so the value is neither echoed nor
# exposed in a child-process argv. This happens before any service restart.
echo "🔐 Updating Roo service credential (value redacted)..."
printf '%s' "$ROO_SIM_PATIENT_KEY" | ssh "$DEPLOY_SSH_TARGET" '
    set -euo pipefail
    project_dir="/root/mlai-backend"
    mkdir -p "$project_dir"
    cd "$project_dir"
    umask 077
    secret=$(cat)
    if [ -z "$secret" ]; then
        echo "Missing ROO_SIM_PATIENT_KEY payload" >&2
        exit 1
    fi
    tmp=$(mktemp .env.roo-key.XXXXXX)
    if [ -f .env ]; then
        grep -v "^ROO_SIM_PATIENT_KEY=" .env > "$tmp" || true
    fi
    printf "ROO_SIM_PATIENT_KEY=%s\n" "$secret" >> "$tmp"
    chmod 600 "$tmp"
    mv "$tmp" .env
'

# Keep the Victor AI request-signing credential synchronized with Roo and
# enable only the authenticated service endpoints that use it. The credential
# travels over SSH stdin and is never printed or exposed in a process argv.
echo "🔐 Updating Victor AI Roo signing credential (value redacted)..."
printf '%s' "$VICTOR_AI_ROO_SIGNING_SECRET" | ssh "$DEPLOY_SSH_TARGET" '
    set -euo pipefail
    project_dir="/root/mlai-backend"
    mkdir -p "$project_dir"
    cd "$project_dir"
    umask 077
    secret=$(cat)
    if [ "${#secret}" -lt 32 ]; then
        echo "Invalid VICTOR_AI_ROO_SIGNING_SECRET payload" >&2
        exit 1
    fi
    tmp=$(mktemp .env.victor-roo.XXXXXX)
    if [ -f .env ]; then
        grep -Ev "^(VICTOR_AI_ROO_SIGNING_SECRET|VICTOR_AI_ROO_ENABLED)=" .env > "$tmp" || true
    fi
    printf "VICTOR_AI_ROO_SIGNING_SECRET=%s\n" "$secret" >> "$tmp"
    printf "VICTOR_AI_ROO_ENABLED=true\n" >> "$tmp"
    chmod 600 "$tmp"
    mv "$tmp" .env
'

# Keep the managed Redis credential in the same secret store and transport it
# over SSH stdin. This avoids relying on a manually edited production .env and
# keeps the value out of shell arguments and deployment output.
echo "🔐 Updating shared Redis credential (value redacted)..."
printf '%s' "$REDIS_URL" | ssh "$DEPLOY_SSH_TARGET" '
    set -euo pipefail
    project_dir="/root/mlai-backend"
    mkdir -p "$project_dir"
    cd "$project_dir"
    umask 077
    secret=$(cat)
    case "$secret" in
        redis://*|rediss://*) ;;
        *) echo "Invalid REDIS_URL payload" >&2; exit 1 ;;
    esac
    tmp=$(mktemp .env.redis-url.XXXXXX)
    if [ -f .env ]; then
        grep -v "^REDIS_URL=" .env > "$tmp" || true
    fi
    printf "REDIS_URL=%s\n" "$secret" >> "$tmp"
    chmod 600 "$tmp"
    mv "$tmp" .env
'

# The Xero OAuth scope list is pipeline-managed (repository variable
# XERO_OAUTH_SCOPES) so scope upgrades — e.g. granting accounting.invoices +
# accounting.attachments for the reconciliation agent's draft bills — survive
# redeploys instead of living in hand-edited .env lines that the next deploy
# races against. Empty/unset keeps whatever the droplet already has.
if [ -n "${XERO_OAUTH_SCOPES:-}" ]; then
    echo "🔧 Updating Xero OAuth scope list..."
    printf '%s' "$XERO_OAUTH_SCOPES" | ssh "$DEPLOY_SSH_TARGET" '
        set -euo pipefail
        project_dir="/root/mlai-backend"
        mkdir -p "$project_dir"
        cd "$project_dir"
        umask 077
        scopes=$(cat)
        case "$scopes" in
            *offline_access*) ;;
            *) echo "XERO_OAUTH_SCOPES payload must include offline_access" >&2; exit 1 ;;
        esac
        tmp=$(mktemp .env.xero-scopes.XXXXXX)
        if [ -f .env ]; then
            grep -v "^XERO_OAUTH_SCOPES=" .env > "$tmp" || true
        fi
        printf "XERO_OAUTH_SCOPES=%s\n" "$scopes" >> "$tmp"
        chmod 600 "$tmp"
        mv "$tmp" .env
    '
fi

# Install the versioned connector keyring atomically before Django system
# checks or migrations can write connector credentials with the active key.
# Values are carried over SSH stdin and are never printed or placed in argv.
echo "🔐 Updating connector credential keyring (values redacted)..."
{
    printf '%s\n' "$CONNECTOR_CREDENTIAL_ACTIVE_KEY_ID"
    printf '%s\n' "$CONNECTOR_CREDENTIAL_KEYS"
} | ssh "$DEPLOY_SSH_TARGET" '
    set -euo pipefail
    project_dir="/root/mlai-backend"
    mkdir -p "$project_dir"
    cd "$project_dir"
    umask 077
    IFS= read -r active_key_id
    IFS= read -r keyring
    if [ -z "$active_key_id" ] || [ -z "$keyring" ]; then
        echo "Missing connector credential keyring payload" >&2
        exit 1
    fi
    tmp=$(mktemp .env.connector-keys.XXXXXX)
    if [ -f .env ]; then
        grep -Ev "^(CONNECTOR_CREDENTIAL_KEYS|CONNECTOR_CREDENTIAL_ACTIVE_KEY_ID)=" .env > "$tmp" || true
    fi
    printf "CONNECTOR_CREDENTIAL_KEYS=%s\n" "$keyring" >> "$tmp"
    printf "CONNECTOR_CREDENTIAL_ACTIVE_KEY_ID=%s\n" "$active_key_id" >> "$tmp"
    chmod 600 "$tmp"
    mv "$tmp" .env
'

# Install the production pilot HMAC and its explicit rotation version without
# exposing either value in argv or logs. The release gate fails closed if this
# key does not match the active approval-bound deployment row.
if [ "$ORG_MEMORY_PRODUCTION_DEPLOY_ENABLED" = "true" ]; then
    echo "🔐 Updating Admin Brain production allowlist key (value redacted)..."
    {
        printf '%s\n' "$ORG_MEMORY_PILOT_ALLOWLIST_KEY_VERSION"
        printf '%s\n' "$ORG_MEMORY_PILOT_ALLOWLIST_HMAC_KEY"
    } | ssh "$DEPLOY_SSH_TARGET" '
    set -euo pipefail
    project_dir="/root/mlai-backend"
    mkdir -p "$project_dir"
    cd "$project_dir"
    umask 077
    IFS= read -r key_version
    IFS= read -r hmac_key
    if ! printf "%s" "$key_version" | grep -Eq "^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$"; then
        echo "Invalid Admin Brain allowlist key version" >&2
        exit 1
    fi
    if [ "${#hmac_key}" -lt 32 ]; then
        echo "Invalid Admin Brain allowlist HMAC key" >&2
        exit 1
    fi
    tmp=$(mktemp .env.org-memory-key.XXXXXX)
    if [ -f .env ]; then
        grep -Ev "^(ORG_MEMORY_PILOT_ALLOWLIST_KEY_VERSION|ORG_MEMORY_PILOT_ALLOWLIST_HMAC_KEY)=" .env > "$tmp" || true
    fi
    printf "ORG_MEMORY_PILOT_ALLOWLIST_KEY_VERSION=%s\n" "$key_version" >> "$tmp"
    printf "ORG_MEMORY_PILOT_ALLOWLIST_HMAC_KEY=%s\n" "$hmac_key" >> "$tmp"
    chmod 600 "$tmp"
    mv "$tmp" .env
'

    # The restricted approval and two independent operator identities live outside
    # the checkout. They are replaced atomically on every reviewed Admin Brain release.
    echo "🔐 Updating Admin Brain production approval (content redacted)..."
    printf '%s' "$ORG_MEMORY_PRODUCTION_APPROVAL_MANIFEST" | ssh "$DEPLOY_SSH_TARGET" '
    set -euo pipefail
    operations_dir="/root/mlai-backend-operations"
    mkdir -p "$operations_dir"
    umask 077
    tmp=$(mktemp "$operations_dir/.pilot-approval.XXXXXX")
    cat > "$tmp"
    python3 - "$tmp" <<'"'"'PY'"'"'
import json
import pathlib
import sys

path = pathlib.Path(sys.argv[1])
manifest = json.loads(path.read_text(encoding="utf-8"))
if not isinstance(manifest, dict) or manifest.get("organization_domain") != "mlai.au":
    raise SystemExit("Invalid Admin Brain production approval")
PY
    chmod 600 "$tmp"
    mv "$tmp" "$operations_dir/pilot-approval.json"
'

    echo "🔐 Updating Admin Brain production operators (values redacted)..."
    {
        printf '%s\n' "$ORG_MEMORY_PRODUCTION_STAGE_OPERATOR_EMAIL"
        printf '%s\n' "$ORG_MEMORY_PRODUCTION_ACTIVATION_OPERATOR_EMAIL"
    } | ssh "$DEPLOY_SSH_TARGET" '
    set -euo pipefail
    operations_dir="/root/mlai-backend-operations"
    mkdir -p "$operations_dir"
    umask 077
    IFS= read -r stage_operator
    IFS= read -r activation_operator
    if [ -z "$stage_operator" ] || [ -z "$activation_operator" ] || [ "$stage_operator" = "$activation_operator" ]; then
        echo "Invalid Admin Brain production operators" >&2
        exit 1
    fi
    tmp=$(mktemp "$operations_dir/.pilot-operators.XXXXXX")
    printf "%s\n%s\n" "$stage_operator" "$activation_operator" > "$tmp"
    chmod 600 "$tmp"
    mv "$tmp" "$operations_dir/pilot-operators"
'
else
    echo "ℹ️ Admin Brain production activation is disabled; leaving its protected approval material unchanged."
fi

# 2. Run setup commands on the server
echo "🔧 Configuring server..."
ssh "$DEPLOY_SSH_TARGET" <<EOF
    set -euo pipefail

    upsert_env_value() {
        local key="\$1"
        local value="\$2"
        if grep -q "^\${key}=" .env; then
            sed -i "s|^\${key}=.*|\${key}=\${value}|" .env
        else
            echo "\${key}=\${value}" >> .env
        fi
    }

    require_env_value() {
        local key="\$1"
        local message="\$2"
        if ! grep -Eq "^\${key}=.+" .env; then
            echo "❌ Missing required \${key} in $PROJECT_DIR/.env"
            echo "   \${message}"
            exit 1
        fi
    }

    env_has_value() {
        local key="\$1"
        grep -Eq "^\${key}=.+" .env
    }

    read_env_value() {
        local key="\$1"
        local line
        line=\$(grep -m1 "^\${key}=" .env || true)
        printf '%s' "\${line#*=}"
    }

    print_redacted_env_status() {
        echo "🔐 Required production env status (values redacted):"
        for key in "\$@"; do
            if env_has_value "\$key"; then
                echo "   \${key}=present"
            else
                echo "   \${key}=missing"
            fi
        done
    }

    # Install Docker if not exists
    if ! command -v docker &> /dev/null; then
        echo "Installing Docker..."
        curl -fsSL https://get.docker.com -o get-docker.sh
        sh get-docker.sh
    fi

    cd $PROJECT_DIR
    meeting_room_booking_enabled="$MEETING_ROOM_BOOKING_ENABLED"
    office_manager_enabled="$OFFICE_MANAGER_ENABLED"
    community_bridge_production_enabled="$COMMUNITY_BRIDGE_PRODUCTION_ENABLED"
    org_memory_production_deploy_enabled="$ORG_MEMORY_PRODUCTION_DEPLOY_ENABLED"

    compose_run_web() {
        docker compose run -T --rm --no-deps web "\$@" </dev/null
    }

    # Create .env if it doesn't exist
    if [ ! -f .env ]; then
        echo "Creating .env file..."
        cp .env.example .env
    fi

    # Update .env with production values (Run on every deploy)
    upsert_env_value DEBUG "False"
    upsert_env_value VICTOR_AI_ROO_ENABLED "true"
    upsert_env_value ALLOWED_HOSTS "api.mlai.au,209.38.85.60,10.126.0.2,localhost,127.0.0.1,esafety.localhost"
    # Plane owns admin.mlai.au after cutover, so it must not be trusted as a
    # credentialed browser origin for the MLAI API. Rollback routes to ops;
    # MLAI Chat remains a separately required credentialed origin.
    upsert_env_value CORS_ALLOWED_ORIGINS "https://mlai.au,https://www.mlai.au,https://victorai.win,https://www.victorai.win,https://ops.mlai.au,https://chat.mlai.au"
    upsert_env_value CSRF_TRUSTED_ORIGINS "https://mlai.au,https://www.mlai.au,https://api.mlai.au,https://ops.mlai.au,https://chat.mlai.au"
    upsert_env_value DEFAULT_BACKEND_URL "https://api.mlai.au"
    upsert_env_value DEFAULT_FRONTEND_URL "https://mlai.au"
    upsert_env_value COMMUNITY_CHAT_API_AUDIENCE "https://api.mlai.au"
    upsert_env_value COMMUNITY_CHAT_FRONTEND_URL "https://chat.mlai.au"
    upsert_env_value COMMUNITY_CHAT_RELAY_URL "wss://chat.mlai.au"
    upsert_env_value COMMUNITY_CHAT_ADAPTER_URL "$COMMUNITY_CHAT_ADAPTER_URL"
    upsert_env_value COMMUNITY_CHAT_EMAIL_CODE_AUTH_ENABLED "true"
    upsert_env_value COMMUNITY_CHAT_PASSWORD_AUTH_ENABLED "false"
    upsert_env_value COMMUNITY_CHAT_DEVICE_AUTH_ENABLED "true"
    upsert_env_value CUSTOMERIO_COMMUNITY_CHAT_CODE_MESSAGE_ID "mlai_chat_sign_in_code"
    upsert_env_value COMMUNITY_CHAT_ALLOWED_ORIGINS "https://chat.mlai.au,tauri://localhost,http://tauri.localhost,mlaichat://callback"
    upsert_env_value MEETING_ROOM_BOOKING_ENABLED "\$meeting_room_booking_enabled"
    # Stage every release with creation and new claims disabled. The reviewed
    # target value is installed only after schema and companion checks pass.
    # Retraction repair continues in the scheduler while this is false.
    upsert_env_value OFFICE_MANAGER_ENABLED "false"
    upsert_env_value COMMUNITY_BRIDGE_PRODUCTION_ENABLED "\$community_bridge_production_enabled"
    if [ "$bridge_present" -gt 0 ]; then
        upsert_env_value SLACK_BRIDGE_BOT_USER_ID "$SLACK_BRIDGE_BOT_USER_ID"
        upsert_env_value BUZZ_BRIDGE_ADAPTER_URL "$BUZZ_BRIDGE_ADAPTER_URL"
    fi
    upsert_env_value MEDHACK_URL "https://mlai.au"
    upsert_env_value ESAFETY_URL "https://mlai.au"
    upsert_env_value VIBE_RAISING_URL "https://mlai.au"
    upsert_env_value FOUNDER_TOOLS_URL "https://mlai.au"
    upsert_env_value GOOGLE_OAUTH_REDIRECT_URI "https://api.mlai.au/integrations/callback/google"
    upsert_env_value GITHUB_OAUTH_REDIRECT_URI "https://api.mlai.au/integrations/callback/github"
    upsert_env_value STRIPE_OAUTH_REDIRECT_URI "https://api.mlai.au/integrations/callback/stripe"
    upsert_env_value XERO_OAUTH_REDIRECT_URI "https://api.mlai.au/integrations/callback/xero"
    # Replace any malformed legacy settings-expression line with the literal
    # Stripe API version expected by the production runtime.
    sed -i '/^[[:space:]]*STRIPE_API_VERSION[[:space:]]*=/d' .env
    upsert_env_value STRIPE_API_VERSION "2026-02-25.clover"
    upsert_env_value ROO_POINTS_TERMS_VERSION "roo-points-terms-2026-05-04"
    upsert_env_value ROO_POINTS_PRIVACY_VERSION "privacy-2026-05-04"
    upsert_env_value ROO_POINTS_TERMS_ACCEPTANCE_TEXT "I understand that Roo Points are not money, have no cash value, are not refundable except where required by law, and cannot be transferred or sold."
    # Preserve reporting access and the granular write scopes required by the
    # explicit payout approval workflow and tracking-option creation.
    # Overridable via the XERO_OAUTH_SCOPES repository variable (interpolated
    # below at heredoc construction). The default grants the reconciliation
    # agent's write path: granular accounting.invoices + accounting.payments
    # (draft bills / bill payments) and accounting.attachments (source PDFs).
    upsert_env_value XERO_OAUTH_SCOPES "${XERO_OAUTH_SCOPES:-offline_access accounting.invoices accounting.invoices.read accounting.payments accounting.payments.read accounting.settings.read accounting.settings accounting.contacts.read accounting.reports.balancesheet.read accounting.reports.profitandloss.read accounting.banktransactions accounting.attachments}"
    upsert_env_value RECONCILIATION_DEFAULT_DOMAIN "mlai.au"
    upsert_env_value RECONCILIATION_SCHEDULER_ENABLED "true"
    upsert_env_value NOTION_OAUTH_REDIRECT_URI "https://api.mlai.au/integrations/callback/notion"
    upsert_env_value GOOGLE_DRIVE_OAUTH_REDIRECT_URI "https://api.mlai.au/integrations/callback/google-drive"
    upsert_env_value GOOGLE_ANALYTICS_OAUTH_REDIRECT_URI "https://api.mlai.au/integrations/callback/google-analytics"
    upsert_env_value FT_SLACK_OAUTH_REDIRECT_URI "https://api.mlai.au/integrations/callback/slack"
    upsert_env_value SLACK_OAUTH_REDIRECT_URI "https://api.mlai.au/integrations/callback/slack"
    # Keep Slack's daily digest to three genuinely featured jobs. Other matches
    # remain available on the public daily jobs page.
    upsert_env_value JOBS_TOP_PICK_LIMIT "3"
    upsert_env_value APP_RELEASE "$APP_RELEASE"
    upsert_env_value HEALTH_HACK_AI_BUDGET_MODE "enforce"
    # Keep the atomic worst-case reservation aligned with Roo's enforced model
    # request limits, including on hosts whose .env predates these defaults.
    upsert_env_value HEALTH_HACK_AI_MAX_PROMPT_TOKENS "12000"
    upsert_env_value HEALTH_HACK_AI_MAX_COMPLETION_TOKENS "2000"
    # Admin Brain retrieval remains fail-closed unless the separately governed
    # production rollout is explicitly enabled. Every mutation path remains
    # hard-disabled in either state. Even when enabled, the query API is
    # switched back off below if the staging readiness gate reports that the
    # pilot is still awaiting its first retrievable evidence.
    if [ "\$org_memory_production_deploy_enabled" = "true" ]; then
        upsert_env_value ORG_MEMORY_QUERY_API_ENABLED "true"
    else
        upsert_env_value ORG_MEMORY_QUERY_API_ENABLED "false"
    fi
    upsert_env_value ORG_MEMORY_PILOT_ORGANIZATION_DOMAIN "mlai.au"
    # Version pins are deployment-managed so semantic reprocessing cannot be
    # accidentally suppressed by a stale value in the host's long-lived .env.
    upsert_env_value ORG_MEMORY_EXTRACTOR_VERSION "org-memory-extractor-v5"
    upsert_env_value ORG_MEMORY_EXTRACTION_SCHEMA_VERSION "org-memory-extraction-schema-v2"
    upsert_env_value ORG_MEMORY_EXTRACTION_PROMPT_VERSION "org-memory-extraction-prompt-v2"
    upsert_env_value ORG_MEMORY_SELECTOR_VERSION "org-memory-rules-selector-v2"
    upsert_env_value ORG_MEMORY_ANSWER_SCHEMA_VERSION "org-memory-answer-schema-v2"
    upsert_env_value ORG_MEMORY_ANSWER_PROMPT_VERSION "org-memory-answer-prompt-v3"
    # Google Drive is the first reviewed production ingestion provider. Its
    # checked-in manifest, per-organisation approval, and per-source approval
    # remain independent fail-closed gates beneath this deployment allowlist.
    upsert_env_value ORG_MEMORY_ENABLED_PROVIDERS "google_drive"
    upsert_env_value ORG_MEMORY_PUBLICATION_ENABLED "false"
    upsert_env_value ORG_MEMORY_ACTIONS_ENABLED "false"
    upsert_env_value ORG_MEMORY_ACTION_LINEAR_EXECUTION_ENABLED "false"
    upsert_env_value ORG_MEMORY_SELECTOR_EXPORT_ENABLED "false"
    # Web concurrency: gunicorn sync-worker count (read by scripts/start-web.sh).
    # Sized to droplet RAM (~250MB/worker). 16 fits the 8GB/4vCPU droplet with headroom.
    upsert_env_value GUNICORN_WORKERS "16"
    print_redacted_env_status CONTENT_FACTORY_URL GITHUB_APP_ID GITHUB_APP_PRIVATE_KEY VALLEY_HARNESS_URL REDIS_URL ROO_SERVICE_URL ROO_SIM_PATIENT_KEY HEALTH_HACK_API_KEY ROO_API_KEY INTERNAL_API_KEY OFFICE_MANAGER_SLACK_BOT_TOKEN OFFICE_MANAGER_SLACK_CHANNEL_ID OFFICE_MANAGER_TIMEZONE VICTOR_AI_ROO_SIGNING_SECRET VICTOR_AI_ROO_ENABLED UMAMI_BASE_URL CONTENT_ANALYTICS_HOST_URL COMMUNITY_CHAT_ADAPTER_URL COMMUNITY_CHAT_ADAPTER_TOKEN COMMUNITY_CHAT_EMAIL_CODE_PEPPER COMMUNITY_CHAT_EMAIL_CODE_DELIVERY_SECRET CUSTOMERIO_API_KEY CUSTOMERIO_COMMUNITY_CHAT_CODE_MESSAGE_ID
    require_env_value CONTENT_FACTORY_URL "Set CONTENT_FACTORY_URL to http://<content-factory-private-ip>:8000 for the cross-droplet Content Factory deployment."
    require_env_value GITHUB_APP_ID "Set GITHUB_APP_ID to the MLAI Tools GitHub App id so Content Factory can receive installation tokens."
    require_env_value GITHUB_APP_PRIVATE_KEY "Set GITHUB_APP_PRIVATE_KEY to the MLAI Tools GitHub App private key with escaped newlines."
    require_env_value VALLEY_HARNESS_URL "Set VALLEY_HARNESS_URL to http://<valley-private-ip>:8080 for the cross-droplet Valley deployment."
    require_env_value REDIS_URL "Set REDIS_URL to the managed Redis/Valkey connection string. Production AI guards refuse process-local cache state."
    if ! grep -Eq '^(VALLEY_HARNESS_API_KEY|INTERNAL_API_KEY|ROO_API_KEY|MLAI_API_KEY)=.+' .env; then
        echo "WARNING: no Valley service API key is configured; Vibe Raising email draft runs will not reach Valley."
    fi
    require_env_value ROO_SERVICE_URL "Set ROO_SERVICE_URL to Roo's private VPC base URL."
    require_env_value ROO_SIM_PATIENT_KEY "Set the GitHub Actions ROO_SIM_PATIENT_KEY repository secret to the same dedicated value configured on Roo."
    require_env_value HEALTH_HACK_API_KEY "Set HEALTH_HACK_API_KEY to the dedicated Cloudflare Worker credential."
    require_env_value ROO_API_KEY "Set ROO_API_KEY to the separate credential Roo uses to record diagnosis verdicts."
    require_env_value INTERNAL_API_KEY "Set INTERNAL_API_KEY to a credential that is distinct from Roo's mutation credential."
    require_env_value VICTOR_AI_ROO_SIGNING_SECRET "Set the GitHub Actions VICTOR_AI_ROO_SIGNING_SECRET repository secret to the same dedicated value configured on Roo."
    health_hack_key=\$(read_env_value HEALTH_HACK_API_KEY)
    roo_sim_key=\$(read_env_value ROO_SIM_PATIENT_KEY)
    roo_api_key=\$(read_env_value ROO_API_KEY)
    internal_api_key=\$(read_env_value INTERNAL_API_KEY)
    victor_ai_roo_secret=\$(read_env_value VICTOR_AI_ROO_SIGNING_SECRET)
    if [ "\${#health_hack_key}" -lt 32 ] || [ "\${#roo_sim_key}" -lt 32 ] || [ "\${#roo_api_key}" -lt 32 ] || [ "\${#internal_api_key}" -lt 32 ] || [ "\${#victor_ai_roo_secret}" -lt 32 ]; then
        echo "❌ HEALTH_HACK_API_KEY, ROO_SIM_PATIENT_KEY, ROO_API_KEY, INTERNAL_API_KEY, and VICTOR_AI_ROO_SIGNING_SECRET must each contain at least 32 characters."
        exit 1
    fi
    if [ "\$health_hack_key" = "\$roo_sim_key" ] || [ "\$health_hack_key" = "\$roo_api_key" ] || [ "\$health_hack_key" = "\$victor_ai_roo_secret" ] || [ "\$roo_sim_key" = "\$roo_api_key" ] || [ "\$roo_sim_key" = "\$victor_ai_roo_secret" ] || [ "\$roo_api_key" = "\$victor_ai_roo_secret" ] || [ "\$roo_api_key" = "\$internal_api_key" ]; then
        echo "❌ Roo, internal, Health Hack, simulated-patient, and Victor AI credentials must preserve their documented trust-domain boundaries."
        exit 1
    fi
    unset health_hack_key roo_sim_key roo_api_key internal_api_key victor_ai_roo_secret

    require_env_value OFFICE_MANAGER_SLACK_BOT_TOKEN "Retain the Public Roo bot token for durable Office Manager recovery."
    require_env_value OFFICE_MANAGER_SLACK_CHANNEL_ID "Retain the coworking channel for durable Office Manager recovery."
    require_env_value OFFICE_MANAGER_TIMEZONE "Retain the Office Manager timezone for durable recovery."
        office_manager_timezone=\$(read_env_value OFFICE_MANAGER_TIMEZONE)
        if [ "\$office_manager_timezone" != "Australia/Melbourne" ]; then
            echo "❌ OFFICE_MANAGER_TIMEZONE must be Australia/Melbourne."
            exit 1
        fi
        unset office_manager_timezone

    all_runtime_writer_services=(web scheduler memory-worker memory-scheduler community-email-worker bridge-worker bridge-reconciler bridge-retention analytics-sync)
    runtime_services=(web scheduler memory-worker memory-scheduler community-email-worker)
    if [ "\$community_bridge_production_enabled" = "true" ] \
        && env_has_value SLACK_BRIDGE_BOT_TOKEN \
        && { env_has_value DISCORD_BRIDGE_BOT_TOKEN \
            || { env_has_value BUZZ_BRIDGE_ADAPTER_URL \
                && env_has_value BUZZ_BRIDGE_ADAPTER_TOKEN \
                && env_has_value BUZZ_BRIDGE_CALLBACK_SECRET; }; }; then
        runtime_services+=(bridge-worker bridge-reconciler bridge-retention)
        bridge_worker_enabled=1
    else
        bridge_worker_enabled=0
        echo "ℹ️ Skipping bridge-worker startup because Slack plus a destination adapter are not fully configured."
    fi

    # The Umami data plane (ops/content-analytics) is a separate Compose
    # project; analytics-sync only aggregates from it, so it starts only once
    # the full backend analytics contract is configured in .env.
    if env_has_value UMAMI_BASE_URL \
        && { env_has_value UMAMI_API_TOKEN || { env_has_value UMAMI_USERNAME && env_has_value UMAMI_PASSWORD; }; } \
        && env_has_value CONTENT_ANALYTICS_TRACKER_SCRIPT_URL \
        && env_has_value CONTENT_ANALYTICS_HOST_URL; then
        runtime_services+=(analytics-sync)
        analytics_sync_enabled=1
    else
        analytics_sync_enabled=0
        echo "ℹ️ Skipping analytics-sync startup because the Umami analytics contract is not fully configured."
    fi

    docker network inspect mlai-shared >/dev/null 2>&1 || docker network create mlai-shared

    # Preserve the exact currently running image IDs before Compose retags the
    # service images during the build. This lets the ERR trap restore the last
    # known-good runtime even after new containers have been created.
    rollback_manifest=\$(mktemp)
    rollback_tags=()
    running_writer_services=()
    for service in "\${all_runtime_writer_services[@]}"; do
        container_id=\$(docker compose ps -q "\$service" || true)
        if [ -n "\$container_id" ]; then
            running_writer_services+=("\$service")
            image_id=\$(docker inspect --format '{{.Image}}' "\$container_id")
            image_ref=\$(docker inspect --format '{{.Config.Image}}' "\$container_id")
            rollback_tag="mlai-backend-rollback-\${service}:$APP_RELEASE_SHORT"
            docker image tag "\$image_id" "\$rollback_tag"
            printf '%s|%s|%s|%s\n' "\$service" "\$image_id" "\$image_ref" "\$rollback_tag" >> "\$rollback_manifest"
            rollback_tags+=("\$rollback_tag")
        fi
    done

    echo "🐘 Starting database..."
    docker compose up -d db

    echo "🏗️ Building runtime images: \${runtime_services[*]}..."
    docker compose build "\${runtime_services[@]}"

    # Every pre-migration gate (Redis security state, production URLs and
    # service connectivity, memory provider governance, PostgreSQL vector
    # support, GitHub App credentials) plus the migration plan, in one
    # container. They used to be six separate compose-run invocations that
    # spent ~35s on cold Django starts to do a few seconds of work.
    # (No backticks in this heredoc: it is unquoted, so they would be
    # command-substituted on the deploying runner.)
    echo "🧪 Running deployment preflight..."
    compose_run_web python manage.py deploy_preflight

    echo "🧬 Auditing historical Roo migration identities before schema changes..."
    office_manager_pre_attestation=/root/mlai-backend-operations/office-manager-migration-pre-attestation.json
    office_manager_post_attestation=/root/mlai-backend-operations/office-manager-migration-post-attestation.json
    run_office_manager_migration_audit() {
        local office_manager_attestation="\$1"
        if [ -s "\$office_manager_attestation" ]; then
            docker compose run -T --rm --no-deps \
                -v "\$office_manager_attestation:/run/office-manager-migration-attestation.json:ro" \
                web python manage.py audit_office_manager_migrations \
                --attestation-file /run/office-manager-migration-attestation.json \
                --configured-office-manager-channel \
                "\$(read_env_value OFFICE_MANAGER_SLACK_CHANNEL_ID)" \
                </dev/null
        else
            compose_run_web python manage.py audit_office_manager_migrations \
                --configured-office-manager-channel \
                "\$(read_env_value OFFICE_MANAGER_SLACK_CHANNEL_ID)"
        fi
    }
    run_office_manager_migration_audit "\$office_manager_pre_attestation"

    if [ "true" = "true" ]; then
        echo "🧪 Verifying the actual Public Roo Slack app and coworking channel..."
        office_manager_slack_token=\$(read_env_value OFFICE_MANAGER_SLACK_BOT_TOKEN)
        office_manager_channel_id=\$(read_env_value OFFICE_MANAGER_SLACK_CHANNEL_ID)
        slack_auth_headers=\$(mktemp)
        slack_auth_body=\$(curl -fsS --max-time 10 \
            --dump-header "\$slack_auth_headers" \
            -H "Authorization: Bearer \$office_manager_slack_token" \
            https://slack.com/api/auth.test)
        printf '%s' "\$slack_auth_body" | python3 -c '
import json
import sys

payload = json.load(sys.stdin)
if (
    payload.get("ok") is not True
    or not payload.get("team_id")
    or not payload.get("bot_id")
):
    reason = payload.get("error", "unknown")
    raise SystemExit(f"Public Roo Slack auth.test failed: {reason}")
'
        office_manager_slack_team_id=\$(printf '%s' "\$slack_auth_body" | python3 -c '
import json
import sys
print(json.load(sys.stdin)["team_id"])
')
        office_manager_slack_bot_id=\$(printf '%s' "\$slack_auth_body" | python3 -c '
import json
import sys
print(json.load(sys.stdin)["bot_id"])
')
        python3 -c '
import sys

required = {
    "channels:history",
    "channels:read",
    "chat:write",
    "im:history",
    "im:write",
    "users:read",
    "users:read.email",
}
scopes = set()
with open(sys.argv[1], encoding="utf-8", errors="replace") as handle:
    for line in handle:
        name, separator, value = line.partition(":")
        if separator and name.strip().lower() == "x-oauth-scopes":
            scopes = {scope.strip() for scope in value.split(",") if scope.strip()}
missing = sorted(required - scopes)
if missing:
    raise SystemExit(
        "Public Roo Slack token is missing required Office Manager scopes: "
        + ", ".join(missing)
    )
' "\$slack_auth_headers"
        rm -f "\$slack_auth_headers"
        slack_channel_body=\$(curl -fsS --max-time 10 \
            -H "Authorization: Bearer \$office_manager_slack_token" \
            --get --data-urlencode "channel=\$office_manager_channel_id" \
            https://slack.com/api/conversations.info)
        printf '%s' "\$slack_channel_body" | python3 -c '
import json
import sys

payload = json.load(sys.stdin)
channel = payload.get("channel") or {}
if payload.get("ok") is not True:
    raise SystemExit("Public Roo cannot inspect the configured Office Manager channel")
if channel.get("id") != sys.argv[1]:
    raise SystemExit("Slack returned a different Office Manager channel")
if channel.get("is_archived") is True or channel.get("is_channel") is not True:
    raise SystemExit("Office Manager channel must be an active public channel")
if channel.get("is_member") is not True:
    raise SystemExit(
        "Public Roo must already be a member of the Office Manager channel"
    )
' "\$office_manager_channel_id"

        slack_history_body=\$(curl -fsS --max-time 10 \
            -H "Authorization: Bearer \$office_manager_slack_token" \
            --get --data-urlencode "channel=\$office_manager_channel_id" \
            --data-urlencode "limit=1" \
            https://slack.com/api/conversations.history)
        printf '%s' "\$slack_history_body" | python3 -c '
import json
import sys

payload = json.load(sys.stdin)
if payload.get("ok") is not True:
    reason = payload.get("error", "unknown")
    raise SystemExit(
        f"Public Roo cannot inspect Office Manager message history: {reason}"
    )
'

        # Recovery work is intentionally drained while new Office Manager
        # claims are disabled. Bind the backend token to the exact Public Roo
        # app on every deploy so disabled-mode recovery cannot post through a
        # different valid Slack bot.
        echo "🧪 Verifying the live Public Roo Office Manager companion contract..."
        roo_service_url=\$(read_env_value ROO_SERVICE_URL)
        roo_health_body=\$(curl -fsS --max-time 10 \
            "\${roo_service_url%/}/healthz/ready")
        printf '%s' "\$roo_health_body" | python3 -c '
import json
import sys
from urllib.parse import urlparse

payload = json.load(sys.stdin)
contract = payload.get("office_manager") or {}
if payload.get("status") != "ok" or payload.get("surface") != "public":
    raise SystemExit("Office Manager companion is not a ready Public Roo service")
office_manager_enabled = sys.argv[3] == "true"
if office_manager_enabled and contract.get("actions_enabled") is not True:
    raise SystemExit("Public Roo Office Manager actions must be enabled before backend scheduling")
if contract.get("timezone") != "Australia/Melbourne":
    raise SystemExit("Public Roo and backend Office Manager timezones do not match")
slack_identity = contract.get("slack_identity") or {}
if (
    slack_identity.get("team_id") != sys.argv[1]
    or slack_identity.get("bot_id") != sys.argv[2]
):
    raise SystemExit(
        "Backend and Public Roo Office Manager Slack app identities do not match"
    )
backend_base_url = str(contract.get("backend_base_url") or "")
parsed = urlparse(backend_base_url)
port = parsed.port
if port is None:
    port = 443 if parsed.scheme == "https" else 80
allowed_authorities = {
    ("https", "api.mlai.au", 443),
    ("http", "10.126.0.2", 80),
    ("http", "10.126.0.2", 8000),
}
if (parsed.scheme, parsed.hostname, port) not in allowed_authorities:
    raise SystemExit("Public Roo Office Manager backend URL targets an unexpected service")
if parsed.path not in {"", "/"}:
    raise SystemExit(
        "Public Roo Office Manager backend URL must be a root origin without an /api/v1 path"
    )
if contract.get("claim_path") != "/api/v1/points/coworking/office-manager/claim/":
    raise SystemExit("Public Roo Office Manager claim URL path does not match the backend contract")
backend_contract = contract.get("backend_contract") or {}
expected_backend_contract = {
    "status": "ok",
    "contract": "office-manager-v1",
    "credential_scope": "strict_roo",
    "claim_generation_supported": True,
    "claim_generation_required": True,
    "timezone": "Australia/Melbourne",
}
if any(
    backend_contract.get(key) != value
    for key, value in expected_backend_contract.items()
) or not isinstance(backend_contract.get("enabled"), bool):
    raise SystemExit("Public Roo reported an inconsistent Office Manager backend contract")
if parsed.username or parsed.password or parsed.query or parsed.fragment:
    raise SystemExit("Public Roo Office Manager claim URL must not contain credentials or parameters")
' "\$office_manager_slack_team_id" "\$office_manager_slack_bot_id" "\$office_manager_enabled"
        unset office_manager_slack_token office_manager_channel_id
        unset office_manager_slack_team_id office_manager_slack_bot_id
        unset slack_auth_body slack_channel_body slack_history_body
        unset roo_service_url roo_health_body
    fi

    previous_runtime_container_ids=()
    previous_scheduler_container_id=""
    previous_scheduler_image_id=""
    previous_scheduler_tick_mtime=0
    for service in "\${all_runtime_writer_services[@]}"; do
        service_container_id=\$(docker compose ps -q "\$service" || true)
        if [ -n "\$service_container_id" ]; then
            previous_runtime_container_ids+=("\$service_container_id")
            if [ "\$service" = "scheduler" ]; then
                previous_scheduler_container_id="\$service_container_id"
                previous_scheduler_image_id=\$(
                    docker inspect --format '{{.Image}}' "\$service_container_id"
                )
                previous_scheduler_tick_mtime=\$(
                    docker exec "\$service_container_id" sh -c \
                        'stat -c %Y /tmp/mlai-scheduled-discovery.ok 2>/dev/null || printf 0' \
                        2>/dev/null || printf 0
                )
            fi
        fi
    done
    unset service service_container_id

    verify_scheduler_recovery_tick() {
        local expected_container_id="\$1"
        local expected_image_id="\$2"
        local previous_tick_mtime="\$3"
        local scheduler_container_id=""
        local scheduler_image_id=""
        local scheduler_running=""
        local scheduler_health=""
        local scheduler_tick_mtime=0
        local attempt

        for attempt in \$(seq 1 24); do
            scheduler_container_id=\$(docker compose ps -q scheduler || true)
            if [ -n "\$scheduler_container_id" ]; then
                scheduler_image_id=\$(
                    docker inspect --format '{{.Image}}' "\$scheduler_container_id" \
                        2>/dev/null || true
                )
                scheduler_running=\$(
                    docker inspect --format '{{.State.Running}}' "\$scheduler_container_id" \
                        2>/dev/null || true
                )
                scheduler_health=\$(
                    docker inspect --format \
                        '{{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}' \
                        "\$scheduler_container_id" 2>/dev/null || true
                )
                scheduler_tick_mtime=\$(
                    docker exec "\$scheduler_container_id" sh -c \
                        'stat -c %Y /tmp/mlai-scheduled-discovery.ok 2>/dev/null || printf 0' \
                        2>/dev/null || printf 0
                )
                if { [ -z "\$expected_container_id" ] \
                        || [ "\$scheduler_container_id" = "\$expected_container_id" ]; } \
                    && { [ -z "\$expected_image_id" ] \
                        || [ "\$scheduler_image_id" = "\$expected_image_id" ]; } \
                    && [ "\$scheduler_running" = "true" ] \
                    && [ "\$scheduler_health" = "healthy" ] \
                    && [ "\$scheduler_tick_mtime" -gt "\$previous_tick_mtime" ]; then
                    echo "✅ Scheduler recovery produced a fresh successful tick."
                    return 0
                fi
            fi
            sleep 5
        done

        echo "❌ Scheduler recovery did not produce a fresh successful tick." >&2
        if [ -n "\$scheduler_container_id" ]; then
            docker inspect --format \
                'container={{.Id}} image={{.Image}} running={{.State.Running}} health={{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}' \
                "\$scheduler_container_id" 2>/dev/null || true
        fi
        docker compose logs --tail 80 scheduler || true
        return 1
    }

    runtime_restore_attempted=0
    new_runtime_replacement_started=0
    migration_started=0
    schema_transition_started=0
    schema_transition_completed=0
    restore_runtime_on_error() {
        if [ "\$runtime_restore_attempted" = "1" ]; then
            return
        fi
        runtime_restore_attempted=1
        trap - ERR
        set +e

        if [ "\$migration_started" = "1" ]; then
            if [ "\$schema_transition_completed" != "1" ]; then
                # Django commits migrations individually. A failing migrate can
                # therefore leave a prefix applied while the new binary still
                # requires later columns. Neither the previous nor the new runtime
                # is proven compatible with that intermediate schema. Keep every
                # writer stopped and make the failed deployment the operator alert.
                echo "❌ CRITICAL: migration transition is incomplete; runtime services remain stopped for audited schema repair."
                echo "⚠️ Deployment failed after schema advancement began; keeping all runtime writers safely disabled."
                docker compose stop "\${all_runtime_writer_services[@]}" || true
                return
            fi
        fi

        echo "⚠️ Deployment failed after runtime services were paused; staging Office Manager disabled and selecting fail-closed recovery."
        upsert_env_value OFFICE_MANAGER_ENABLED "false" || true
        if [ "\$new_runtime_replacement_started" != "1" ] \
            && [ "\$migration_started" != "1" ]; then
            # These stopped containers still reference the last known-good images
            # and carry the environment that was validated with that release. Do
            # not replace them with the just-built image: a pre/post-migration
            # failure can leave that image waiting forever on migrate --check.
            echo "⚠️ Deployment failed before schema advancement; restoring the last known-good runtime images."
            restored_services=()
            while IFS='|' read -r service image_id image_ref rollback_tag; do
                [ -n "\$service" ] || continue
                docker image tag "\$image_id" "\$image_ref"
                restored_services+=("\$service")
            done < "\$rollback_manifest"
            if [ "\${#restored_services[@]}" -gt 0 ]; then
                docker compose up -d --force-recreate "\${restored_services[@]}"
            elif [ "\${#previous_runtime_container_ids[@]}" -gt 0 ]; then
                docker start "\${previous_runtime_container_ids[@]}" >/dev/null || true
            else
                echo "⚠️ No prior runtime containers were recorded; leaving services stopped for operator recovery."
            fi
            if [ -n "\$previous_scheduler_container_id" ]; then
                verify_scheduler_recovery_tick \
                    "" \
                    "\$previous_scheduler_image_id" \
                    "\$previous_scheduler_tick_mtime" || true
            else
                echo "⚠️ No prior scheduler container was available to restore." >&2
            fi
            return
        fi

        # Once the full migration graph has been checked (or replacement has
        # begun), the new image is safe to recreate with the staged-off feature
        # flag. Still require a fresh scheduler tick after recovery.
        docker compose up -d --force-recreate "\${runtime_services[@]}" || true
        verify_scheduler_recovery_tick "" "" 0 || true
    }

    echo "⏸️ Pausing all runtime writers before DB migrations..."
    docker compose stop "\${all_runtime_writer_services[@]}" || true
    trap restore_runtime_on_error ERR
    trap 'deployment_status=\$?; if [ "\$deployment_status" != "0" ]; then restore_runtime_on_error; fi' EXIT

    echo "🗄️ Running migrations..."
    # From this point a failed migrate may still have committed earlier
    # append-only migrations. Never restore either binary until the complete
    # migration graph is proven; an intermediate schema is operator-repair-only.
    migration_started=1
    schema_transition_started=1
    compose_run_web python manage.py migrate --noinput
    compose_run_web python manage.py migrate --check --noinput
    schema_transition_completed=1

    echo "🧬 Re-auditing Office Manager provenance after migrations..."
    if ! run_office_manager_migration_audit "\$office_manager_post_attestation"; then
        echo "❌ Post-migration Office Manager data requires operator reconciliation." >&2
        # The nullable quarantine is understood only by the new image. Keep the
        # feature off and start that image so an older binary cannot reverse an
        # allocation whose provenance 0037 marked unknown.
        upsert_env_value OFFICE_MANAGER_ENABLED "false"
        docker compose up -d --force-recreate "\${runtime_services[@]}"
        verify_scheduler_recovery_tick "" "" 0
        runtime_restore_attempted=1
        false
    fi

    # Migration readiness, vector installation, memory index rebuild, startup
    # update schema, Vibe Raising upload routes, Firebase Storage CORS and the
    # coworking booking guard, in one container. These were seven separate
    # compose-run invocations, each paying a cold Django start while web was
    # stopped — so the boot overhead was production downtime.
    echo "✅ Running post-migration deployment checks..."
    compose_run_web python manage.py deploy_postmigrate

    if [ "$bridge_present" -gt 0 ]; then
        echo "🌉 Upserting the reviewed Slack to MLAI Chat channel mapping..."
        compose_run_web python manage.py upsert_community_bridge_channel \
            --slack-workspace-id "$SLACK_BRIDGE_WORKSPACE_ID" \
            --slack-channel-id "$SLACK_BRIDGE_CHANNEL_ID" \
            --slack-channel-name "$SLACK_BRIDGE_CHANNEL_NAME" \
            --destination-platform buzz \
            --destination-workspace-id "$BUZZ_BRIDGE_DESTINATION_WORKSPACE_ID" \
            --destination-channel-id "$BUZZ_BRIDGE_DESTINATION_CHANNEL_ID" \
            --destination-channel-name "$BUZZ_BRIDGE_DESTINATION_CHANNEL_NAME"
    fi

    if [ "\$org_memory_production_deploy_enabled" = "true" ]; then
        approval_manifest="/root/mlai-backend-operations/pilot-approval.json"
        approval_manifest_container="/run/org-memory-pilot-approval.json"
        operators_file="/root/mlai-backend-operations/pilot-operators"
        test -s "\$approval_manifest"
        test -s "\$operators_file"
        stage_operator=\$(sed -n '1p' "\$operators_file")
        activation_operator=\$(sed -n '2p' "\$operators_file")
        test -n "\$stage_operator"
        test -n "\$activation_operator"
        test "\$stage_operator" != "\$activation_operator"
        approval_hash=\$(python3 - "\$approval_manifest" <<'PY'
import hashlib
import json
import pathlib
import sys

manifest = json.loads(pathlib.Path(sys.argv[1]).read_text(encoding="utf-8"))
canonical = json.dumps(
    manifest,
    ensure_ascii=True,
    allow_nan=False,
    sort_keys=True,
    separators=(",", ":"),
)
print(hashlib.sha256(canonical.encode("utf-8")).hexdigest())
PY
)
        allowlist_key_version=\$(read_env_value ORG_MEMORY_PILOT_ALLOWLIST_KEY_VERSION)
        approval_hash_short=\${approval_hash:0:32}
        stage_idempotency_key="production-\${allowlist_key_version}-\${approval_hash_short}-stage"
        activation_idempotency_key="production-\${allowlist_key_version}-\${approval_hash_short}-activate"

        compose_run_web_with_approval() {
            docker compose run -T --rm --no-deps \
                -v "\$approval_manifest:\$approval_manifest_container:ro" \
                web "\$@" </dev/null
        }

        echo "🧹 Recovering work interrupted by the stopped deployment worker..."
        compose_run_web python manage.py recover_org_memory_stopped_worker_work \
            --organization-domain mlai.au \
            --operator-email "\$stage_operator" \
            --apply

        # One-shot repairs for the versionless source-access-restored, the
        # consolidation outer-join lock, and the timezone-less claim datetime
        # dead letters were removed here. They drained on the deploy that
        # shipped them and have reported "candidates": 0 on every deploy since,
        # so they only added cold-start time to the window where web is
        # stopped. Their management commands are still available to run by hand
        # if a matching backlog ever reappears.

        echo "🧹 Cancelling queued extraction work for superseded targets..."
        compose_run_web python manage.py cancel_org_memory_superseded_extraction_work \
            --organization-domain mlai.au \
            --provider google_drive \
            --operator-email "\$stage_operator" \
            --apply

        echo "🧹 Cancelling queued consolidation work from superseded extraction targets..."
        compose_run_web python manage.py cancel_org_memory_superseded_consolidation_work \
            --organization-domain mlai.au \
            --provider google_drive \
            --operator-email "\$stage_operator" \
            --apply

        # Five per-version extraction dead-letter reconciliations (schema-v1,
        # then extractor-v1 through v4) were removed here. The deployed
        # extractor is v5 and each of these has reported "candidates": 0 on
        # every deploy since its own, so they repaired backlogs that no longer
        # exist. When the extractor version is next bumped, add a single
        # reconciliation for the version being superseded and delete it again
        # once it has drained.

        echo "🧠 Scheduling the reviewed extractor-v5 target for current Drive evidence..."
        compose_run_web python manage.py schedule_org_memory_reextraction \
            --organization-domain mlai.au \
            --provider google_drive \
            --limit 1000 \
            --apply

        echo "✅ Applying the reviewed strong-grounding activation policy..."
        compose_run_web python manage.py reconcile_org_memory_auto_activation \
            --organization-domain mlai.au \
            --provider google_drive \
            --operator-email "\$activation_operator" \
            --limit 5000 \
            --apply

        echo "🩺 Refreshing daily memory health after bounded recovery..."
        compose_run_web python manage.py refresh_org_memory_daily_reconciliation \
            --organization-domain mlai.au \
            --operator-email "\$stage_operator" \
            --apply

        echo "🔐 Applying the reviewed Admin Brain production binding..."
        stage_stdout=\$(mktemp)
        if compose_run_web_with_approval python manage.py stage_org_memory_pilot \
            --organization-domain mlai.au \
            --approval-manifest "\$approval_manifest_container" \
            --operator-email "\$stage_operator" \
            --idempotency-key "\$stage_idempotency_key" \
            --environment production \
            --apply > "\$stage_stdout"; then
            staging_applied=1
        else
            staging_applied=0
        fi
        cat "\$stage_stdout"
        if [ "\$staging_applied" = "1" ]; then
            compose_run_web_with_approval python manage.py activate_org_memory_pilot \
                --organization-domain mlai.au \
                --approval-manifest "\$approval_manifest_container" \
                --operator-email "\$activation_operator" \
                --idempotency-key "\$activation_idempotency_key" \
                --environment production \
                --apply

            echo "🔐 Verifying enforced, active, non-shadow Admin Brain production..."
            compose_run_web python manage.py check_org_memory_pilot_release_gate \
                --organization-domain mlai.au \
                --require-active
            compose_run_web python manage.py report_org_memory_pilot_deployment \
                --organization-domain mlai.au \
                --fail-if-ineffective
            compose_run_web_with_approval python manage.py check_org_memory_pilot_access_matrix \
                --organization-domain mlai.au \
                --approval-manifest "\$approval_manifest_container"

            echo "🔄 Requesting the reviewed Drive parser-v2/extraction-v2 reprocess..."
            compose_run_web python manage.py request_org_memory_reprocess \
                --organization-domain mlai.au \
                --provider google_drive \
                --configuration-id cd4483b5-d6c1-48f6-8268-2b0acd824e12 \
                --idempotency-key committee-drive-parser-v2-extraction-v2 \
                --operator-email "\$activation_operator" \
                --apply
        elif python3 scripts/org_memory_staging_skip.py "\$stage_stdout"; then
            # Evidence ingestion and restoration run in the memory worker,
            # which only receives new code when a deploy completes. While the
            # pilot's only readiness blocker is missing retrievable evidence,
            # skip the binding instead of hard-failing so the deploy is not
            # deadlocked against the very ingestion it ships. Retrieval stays
            # fail-closed on two independent layers: no staged or active
            # deployment binding exists, and the query API flag is switched
            # back off before the runtime services start.
            echo "⚠️ Admin Brain pilot staging skipped: the pilot has no retrievable evidence yet."
            echo "⚠️ Keeping the Admin Brain query API disabled; retrieval remains fail-closed."
            echo "⚠️ The next deploy after ingestion restores evidence will stage and activate the binding."
            upsert_env_value ORG_MEMORY_QUERY_API_ENABLED "false"
        else
            echo "❌ Admin Brain production staging failed; aborting the deploy."
            rm -f "\$stage_stdout"
            # Fail via a command (not exit) so the ERR trap restores web.
            false
        fi
        rm -f "\$stage_stdout"
        unset stage_operator activation_operator approval_hash approval_hash_short
    else
        echo "ℹ️ Skipping Admin Brain production staging and activation; query API remains disabled."
    fi

    echo "🚩 Applying the reviewed Office Manager activation state..."
    upsert_env_value OFFICE_MANAGER_ENABLED "\$office_manager_enabled"

    echo "🌐 Starting runtime services: \${runtime_services[*]}..."
    new_runtime_replacement_started=1
    docker compose up -d --force-recreate "\${runtime_services[@]}"

    if [ "\$bridge_worker_enabled" != "1" ]; then
        echo "🧹 Stopping disabled community bridge services..."
        docker compose stop bridge-worker bridge-reconciler bridge-retention || true
        docker compose rm -f bridge-worker bridge-reconciler bridge-retention || true
    fi

    if [ "\$analytics_sync_enabled" != "1" ]; then
        echo "🧹 Stopping disabled analytics-sync service..."
        docker compose stop analytics-sync || true
        docker compose rm -f analytics-sync || true
    fi

    echo "🔁 Verifying the running web container picked up APP_RELEASE..."
    running_release=\$(docker compose exec -T web sh -lc 'printf "%s" "\$APP_RELEASE"' </dev/null)
    if [ "\$running_release" != "$APP_RELEASE" ]; then
        echo "Expected running web container APP_RELEASE=$APP_RELEASE but found \$running_release"
        # Fail through a command so the ERR trap keeps forward-only schemas
        # paired with no writers until an operator completes recovery.
        false
    fi

    echo "🩺 Verifying external health release..."
    release_ok=0
    for attempt in \$(seq 1 12); do
        health_body=\$(curl -fsS https://api.mlai.au/healthz/ready || true)
        if printf '%s\n' "\$health_body" | grep -F "\"release\": \"$APP_RELEASE_SHORT\"" >/dev/null; then
            release_ok=1
            break
        fi
        sleep 5
    done
    if [ "\$release_ok" != "1" ]; then
        echo "Expected /healthz/ready to report release $APP_RELEASE_SHORT for $APP_RELEASE"
        echo "\$health_body"
        false
    fi

    echo "🩺 Verifying the required scheduler reached a healthy successful tick..."
    scheduler_ok=0
    for attempt in \$(seq 1 24); do
        scheduler_container_id=\$(docker compose ps -q scheduler)
        if [ -n "\$scheduler_container_id" ]; then
            scheduler_health=\$(docker inspect --format \
                '{{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}' \
                "\$scheduler_container_id" 2>/dev/null || true)
            scheduler_running=\$(docker inspect --format '{{.State.Running}}' \
                "\$scheduler_container_id" 2>/dev/null || true)
            if [ "\$scheduler_running" = "true" ] && [ "\$scheduler_health" = "healthy" ]; then
                scheduler_ok=1
                break
            fi
        fi
        sleep 5
    done
    if [ "\$scheduler_ok" != "1" ]; then
        echo "Required scheduler did not report a recent successful tick."
        docker compose ps scheduler || true
        docker compose logs --tail 80 scheduler || true
        false
    fi

    echo "🔐 Verifying the live Office Manager endpoint enforces the strict Roo credential..."
        office_manager_roo_key=\$(read_env_value ROO_API_KEY)
        office_manager_internal_key=\$(read_env_value INTERNAL_API_KEY)
        office_manager_preflight_url=https://api.mlai.au/api/v1/points/coworking/office-manager/preflight/
        office_manager_preflight_body=\$(curl -fsS --max-time 10 \
            -H "X-API-Key: \$office_manager_roo_key" \
            "\$office_manager_preflight_url")
        internal_status=\$(curl -sS -o /dev/null -w '%{http_code}' --max-time 10 \
            -H "X-API-Key: \$office_manager_internal_key" \
            "\$office_manager_preflight_url")
        missing_status=\$(curl -sS -o /dev/null -w '%{http_code}' --max-time 10 \
            "\$office_manager_preflight_url")
        if [ "\$internal_status" != "401" ] || [ "\$missing_status" != "401" ]; then
            echo "Office Manager auth smoke failed (internal=\$internal_status missing=\$missing_status)."
            false
        fi
        printf '%s' "\$office_manager_preflight_body" | python3 -c '
import json
import sys

payload = json.load(sys.stdin)
expected = {
    "status": "ok",
    "contract": "office-manager-v1",
    "credential_scope": "strict_roo",
    "claim_generation_supported": True,
    "claim_generation_required": True,
    "timezone": "Australia/Melbourne",
}
expected_enabled = sys.argv[1] == "true"
if (
    any(payload.get(key) != value for key, value in expected.items())
    or payload.get("enabled") is not expected_enabled
    or set(payload) != {*expected, "enabled"}
):
    raise SystemExit("Live Office Manager preflight returned the wrong contract")
' "\$office_manager_enabled"
        unset office_manager_roo_key office_manager_internal_key
        unset office_manager_preflight_url office_manager_preflight_body
        unset internal_status missing_status

    if [ "\$meeting_room_booking_enabled" = "true" ]; then
        echo "🏢 Verifying the enabled meeting-room API and active room catalogue..."
        meeting_room_api_key=\$(read_env_value ROO_API_KEY)
        meeting_rooms_body=\$(curl -fsS --max-time 10 \
            -H "X-API-Key: \$meeting_room_api_key" \
            https://api.mlai.au/api/v1/points/meeting-rooms/rooms/)
        printf '%s' "\$meeting_rooms_body" | python3 -c '
import json
import sys

payload = json.load(sys.stdin)
slugs = {
    str(room.get("slug") or "")
    for room in payload.get("rooms", [])
    if isinstance(room, dict)
}
expected = {"small-meeting-room", "big-meeting-room"}
if slugs != expected:
    raise SystemExit(f"Expected active meeting rooms {sorted(expected)}, got {sorted(slugs)}")
'
        unset meeting_room_api_key meeting_rooms_body
    fi

    echo "🛠️ Verifying external Django admin assets..."
    admin_login_ok=0
    for attempt in \$(seq 1 12); do
        if curl -fsS \
            -o /dev/null \
            'https://api.mlai.au/admin/login/?next=%2Fadmin%2F'; then
            admin_login_ok=1
            break
        fi
        sleep 5
    done
    if [ "\$admin_login_ok" != "1" ]; then
        echo "Expected Django admin login page to return HTTP 200"
        false
    fi

    admin_css_path=\$(
        docker compose exec -T web python manage.py shell -c \
            'from django.contrib.staticfiles.storage import staticfiles_storage; print(staticfiles_storage.url("admin/css/base.css"))' \
            </dev/null | tail -n 1
    )
    case "\$admin_css_path" in
        /*) ;;
        *) admin_css_path="/\$admin_css_path" ;;
    esac
    if ! curl -fsS -o /dev/null "https://api.mlai.au\$admin_css_path"; then
        echo "Expected Django admin stylesheet to be reachable at \$admin_css_path"
        false
    fi

    echo "🌐 Verifying external Vibe Raising video upload CORS preflight..."
    preflight_headers=\$(mktemp)
    cors_ok=0
    for attempt in \$(seq 1 12); do
        : > "\$preflight_headers"
        if curl -fsS -X OPTIONS \
            -H 'Origin: https://mlai.au' \
            -H 'Access-Control-Request-Method: POST' \
            -H 'Access-Control-Request-Headers: content-type,x-csrftoken,x-request-id' \
            -D "\$preflight_headers" \
            -o /dev/null \
            https://api.mlai.au/api/v1/vibe-raising/uploads/video/session/ &&
            grep -i '^access-control-allow-origin: https://mlai.au' "\$preflight_headers" >/dev/null; then
            cors_ok=1
            break
        fi
        sleep 5
    done
    if [ "\$cors_ok" != "1" ]; then
        echo "Expected video upload session preflight to return CORS headers"
        cat "\$preflight_headers"
        rm -f "\$preflight_headers"
        false
    fi
    rm -f "\$preflight_headers"
    # All release and functional checks passed; rollback images are no longer
    # needed. Keep the ERR trap active until this exact point.
    trap - ERR EXIT
    rm -f "\$rollback_manifest"
    for rollback_tag in "\${rollback_tags[@]}"; do
        docker image rm "\$rollback_tag" >/dev/null 2>&1 || true
    done
EOF

echo "✅ Deployment complete! Check http://$DROPLET_IP or https://api.mlai.au"
