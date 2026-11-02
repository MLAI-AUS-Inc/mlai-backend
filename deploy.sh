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
COMMUNITY_BRIDGE_PRODUCTION_ENABLED="${COMMUNITY_BRIDGE_PRODUCTION_ENABLED:-false}"
ORG_MEMORY_PRODUCTION_DEPLOY_ENABLED="${ORG_MEMORY_PRODUCTION_DEPLOY_ENABLED:-false}"
ORG_MEMORY_PRODUCTION_PUBLIC_CHANNEL_ADMIN_SCOPE_APPROVED="${ORG_MEMORY_PRODUCTION_PUBLIC_CHANNEL_ADMIN_SCOPE_APPROVED:-false}"
LINEAR_CHANNEL_ISSUE_MAX_COMMENTS="${LINEAR_CHANNEL_ISSUE_MAX_COMMENTS:-250}"
COMMUNITY_BRIDGE_DEPENDENCY_VERDICTS_ENABLED="${COMMUNITY_BRIDGE_DEPENDENCY_VERDICTS_ENABLED:-false}"
MESSAGE_SYNC_ENABLED="${MESSAGE_SYNC_ENABLED:-false}"
MESSAGE_SYNC_QUIET_HEAD_BACKOFF_ENABLED="${MESSAGE_SYNC_QUIET_HEAD_BACKOFF_ENABLED:-false}"
COMMUNITY_CHAT_MEMBER_ACCOUNTS_ENABLED="${COMMUNITY_CHAT_MEMBER_ACCOUNTS_ENABLED:-false}"
COMMUNITY_CHAT_INBOX_ERASURE_ENABLED="${COMMUNITY_CHAT_INBOX_ERASURE_ENABLED:-false}"
MESSAGE_SYNC_TARGETED_READ_POLLING="${MESSAGE_SYNC_TARGETED_READ_POLLING:-false}"
MESSAGE_SYNC_RECENT_HISTORY_ENABLED="${MESSAGE_SYNC_RECENT_HISTORY_ENABLED:-false}"
SLACK_MEMBER_CHANNEL_ALLOWED_IDS="${SLACK_MEMBER_CHANNEL_ALLOWED_IDS:-}"
MESSAGE_SYNC_OPEN_PRIORITY_ENABLED="${MESSAGE_SYNC_OPEN_PRIORITY_ENABLED:-true}"
MESSAGE_SYNC_UNREAD_PREFETCH_ENABLED="${MESSAGE_SYNC_UNREAD_PREFETCH_ENABLED:-false}"
MESSAGE_SYNC_TARGETED_STAGED_ROUTING="${MESSAGE_SYNC_TARGETED_STAGED_ROUTING:-false}"
MESSAGE_SYNC_DEVICE_FANOUT_BATCH="${MESSAGE_SYNC_DEVICE_FANOUT_BATCH:-0}"
MESSAGE_SYNC_UNREAD_PREFETCH_PER_HOUR="${MESSAGE_SYNC_UNREAD_PREFETCH_PER_HOUR:-6}"
MESSAGE_SYNC_INBOX_READ_EXPORT="${MESSAGE_SYNC_INBOX_READ_EXPORT:-false}"
MESSAGE_SYNC_INBOX_CURSOR_PUSH="${MESSAGE_SYNC_INBOX_CURSOR_PUSH:-false}"
MESSAGE_SYNC_INBOX_MENTIONS="${MESSAGE_SYNC_INBOX_MENTIONS:-false}"
MESSAGE_SYNC_RELAY_READ_COUNTS="${MESSAGE_SYNC_RELAY_READ_COUNTS:-false}"
export COMMUNITY_CHAT_MEMBER_ACCOUNTS_ENABLED COMMUNITY_CHAT_INBOX_ERASURE_ENABLED
export MESSAGE_SYNC_TARGETED_READ_POLLING MESSAGE_SYNC_INBOX_READ_EXPORT
export MESSAGE_SYNC_INBOX_CURSOR_PUSH MESSAGE_SYNC_INBOX_MENTIONS MESSAGE_SYNC_RELAY_READ_COUNTS
python3 scripts/validate_inbox_deploy_config.py
SLACK_OWNER_INVENTORY_ENABLED="${SLACK_OWNER_INVENTORY_ENABLED:-false}"
COMMUNITY_CHAT_PASSWORD_AUTH_ENABLED="${COMMUNITY_CHAT_PASSWORD_AUTH_ENABLED:-false}"
MLAI_MCP_ENABLED="${MLAI_MCP_ENABLED:-false}"
VALLEY_MCP_UNIFIED_ALIAS_ENABLED="${VALLEY_MCP_UNIFIED_ALIAS_ENABLED:-false}"
MLAI_MCP_COMMUNITY_ENABLED="${MLAI_MCP_COMMUNITY_ENABLED:-false}"
MLAI_MCP_COMMUNITY_RELAY_URL="${MLAI_MCP_COMMUNITY_RELAY_URL:-}"
MLAI_MCP_COMMUNITY_SECRET="${MLAI_MCP_COMMUNITY_SECRET:-}"
MLAI_MCP_CLIENT_INSTALL_URLS="${MLAI_MCP_CLIENT_INSTALL_URLS:-}"
export MLAI_MCP_ENABLED MLAI_MCP_COMMUNITY_ENABLED MLAI_MCP_COMMUNITY_RELAY_URL
export MLAI_MCP_COMMUNITY_SECRET MLAI_MCP_CLIENT_INSTALL_URLS
export VALLEY_MCP_UNIFIED_ALIAS_ENABLED
VALLEY_MCP_ENABLED="${VALLEY_MCP_ENABLED:-false}"
VALLEY_MCP_PUBLIC_BASE_URL="${VALLEY_MCP_PUBLIC_BASE_URL:-https://api.mlai.au}"
WEBSITE_CONNECTION_WRITE_MODE="${WEBSITE_CONNECTION_WRITE_MODE:-disabled}"
WEBSITE_CONNECTION_CANARY_DOMAINS="${WEBSITE_CONNECTION_CANARY_DOMAINS:-}"
APPROVED_MIGRATION_PLAN_SHA256="${APPROVED_MIGRATION_PLAN_SHA256:-}"
export WEBSITE_CONNECTION_WRITE_MODE WEBSITE_CONNECTION_CANARY_DOMAINS APPROVED_MIGRATION_PLAN_SHA256
python3 scripts/validate_website_deploy_config.py
# Empty optional values retain the host's existing startup gate/ownership proof.
COMMUNITY_CHAT_STARTUP_UPDATES_ENABLED="${COMMUNITY_CHAT_STARTUP_UPDATES_ENABLED:-}"
VALLEY_MCP_DOMAIN_VERIFICATION_TOKEN="${VALLEY_MCP_DOMAIN_VERIFICATION_TOKEN:-}"
export VALLEY_MCP_ENABLED VALLEY_MCP_PUBLIC_BASE_URL
export COMMUNITY_CHAT_STARTUP_UPDATES_ENABLED VALLEY_MCP_DOMAIN_VERIFICATION_TOKEN
python3 scripts/validate_valley_mcp_deploy_config.py
# CRM switches and delivery settings. Empty values keep the host's own, so a
# release never switches the CRM off by omission.
CRM_DEPLOY_SETTINGS="COMMUNITY_CHAT_CRM_ENABLED CRM_LUMA_SYNC_ENABLED CRM_SENDING_ENABLED CRM_SMS_ENABLED CRM_WHATSAPP_ENABLED CRM_WARMUP_ENABLED LISTMONK_BASE_URL LISTMONK_API_USER TWILIO_MESSAGING_SERVICE_SID CRM_RESEND_MONITORED_SENDERS CRM_WARMUP_RUN_ID"
for crm_setting in $CRM_DEPLOY_SETTINGS; do
    printf -v "$crm_setting" '%s' "${!crm_setting:-}"
    export "$crm_setting"
done
LISTMONK_API_TOKEN="${LISTMONK_API_TOKEN:-}"
python3 scripts/validate_crm_deploy_config.py
# Founder-journey Articles switches, caps, sign-in limits, article preview
# switches, run snapshot switches and the P1 founder journey switches. Empty values keep the host's own, so a release never switches a
# founder path off by omission.
ARTICLES_DEPLOY_SETTINGS="ARTICLES_FREE_SETUP_RESEARCH_ENABLED ARTICLES_FREE_FIRST_ARTICLE_ENABLED ARTICLES_FIRST_ARTICLE_GOAL_ENABLED ARTICLES_FOUNDER_JOURNEY_ALERTS_ENABLED CONTENT_FACTORY_PORTABLE_PREVIEW_REARM HOSTED_PUBLISH_REQUIRES_RENDERED_PREVIEW ARTICLES_SNAPSHOT_MERGE_BY_MEANING_ENABLED ARTICLES_RUN_EVENTS_ENABLED ARTICLES_RUN_JOB_DUAL_WRITE_ENABLED ARTICLES_RUN_JOB_SHADOW_READ_ENABLED ARTICLES_RUN_JOB_READ_ENABLED MY_STARTUP_FOUNDER_JOURNEY_V2_ENABLED MY_STARTUP_FOUNDER_JOURNEY_API_ENABLED MY_STARTUP_FOUNDER_JOURNEY_OPT_IN_ENABLED COMMUNITY_CHAT_ONE_STEP_JOIN_ENABLED COMMUNITY_CHAT_INVITE_TOKENS_ENABLED MY_STARTUP_PENDING_MEMBER_ACCESS_ENABLED COMMUNITY_CHAT_REVIEW_CLEARED_EMAIL_ENABLED COMMUNITY_CHAT_DEVICE_AUTH_V2_ENABLED MY_STARTUP_ONE_FIELD_START_ENABLED MY_STARTUP_NO_WEBSITE_ARTICLES_ENABLED ARTICLES_FIRST_ARTICLE_CARD_ENABLED ARTICLES_COPY_READY_BEFORE_VERIFICATION_ENABLED ARTICLES_DRAFT_READY_NOTIFICATIONS_ENABLED ARTICLES_COPY_WITHOUT_PREVIEW_ENABLED ARTICLES_DRAFT_TOOLS_ENABLED ARTICLES_LIVE_URL_VERIFICATION_ENABLED MY_STARTUP_GITHUB_CALLBACK_REDIRECTS_ENABLED ARTICLES_FREE_SETUP_AUTOFILL_DAILY_CAP ARTICLES_FREE_SETUP_BASELINE_DAILY_CAP ARTICLES_FREE_FIRST_ARTICLE_DAILY_CAP ARTICLES_FREE_FIRST_ARTICLE_SPIKE_THRESHOLD CONTENT_FACTORY_INTERACTIVE_QUEUE_SLOTS ARTICLES_FOUNDER_JOURNEY_ALERTS_MAX_PER_TICK AUTH_THROTTLE_IP_CODE_REQUESTS_PER_HOUR AUTH_THROTTLE_IP_VERIFY_PER_10M AUTH_THROTTLE_IP_PASSWORD_PER_10M AUTH_THROTTLE_IP_BOOTSTRAP_AUTHENTICATED_PER_10M AUTH_THROTTLE_IP_DEVICE_AUTH_PER_10M COMMUNITY_CHAT_INVITE_TOKEN_DEFAULT_TTL_HOURS COMMUNITY_CHAT_INVITE_TOKEN_MAX_TTL_HOURS COMMUNITY_CHAT_DEVICE_AUTH_MAX_LIFETIME_SECONDS ARTICLES_FOUNDER_JOURNEY_ALERTS_SINCE ARTICLES_WORKSHOP_WINDOWS MY_STARTUP_FOUNDER_JOURNEY_V2_LAUNCHED_AT MY_STARTUP_FOUNDER_JOURNEY_V2_DISABLED_ORGANIZATIONS COMMUNITY_CHAT_INVITE_RESOLVE_RATE"
for articles_setting in $ARTICLES_DEPLOY_SETTINGS; do
    printf -v "$articles_setting" '%s' "${!articles_setting:-}"
    export "$articles_setting"
done
python3 scripts/validate_articles_deploy_config.py
MLAI_MCP_NATIVE_AGENTS_ENABLED="${MLAI_MCP_NATIVE_AGENTS_ENABLED:-false}"
AGENT_RUNTIME_ENABLED="${AGENT_RUNTIME_ENABLED:-false}"
AGENT_RUNTIME_NATIVE_ENABLED="${AGENT_RUNTIME_NATIVE_ENABLED:-false}"
AGENT_RUNTIME_NATIVE_REPLIES_ENABLED="${AGENT_RUNTIME_NATIVE_REPLIES_ENABLED:-false}"
AGENT_RUNTIME_MCP_EVENTS_ENABLED="${AGENT_RUNTIME_MCP_EVENTS_ENABLED:-false}"
AGENT_RUNTIME_NATIVE_COMMUNITIES="${AGENT_RUNTIME_NATIVE_COMMUNITIES:-}"
AGENT_RUNTIME_DOT_SETUP_URL="${AGENT_RUNTIME_DOT_SETUP_URL:-}"
AGENT_RUNTIME_NATIVE_PERSONAL_LIMIT="${AGENT_RUNTIME_NATIVE_PERSONAL_LIMIT:-1}"
AGENT_RUNTIME_NATIVE_SERVICE_PRIVATE_KEY="${AGENT_RUNTIME_NATIVE_SERVICE_PRIVATE_KEY:-}"
AGENT_RUNTIME_NATIVE_REGISTRY_KEY="${AGENT_RUNTIME_NATIVE_REGISTRY_KEY:-}"
COMMUNITY_CHAT_ROLE_SERVICE_TOKEN="${COMMUNITY_CHAT_ROLE_SERVICE_TOKEN:-}"
AGENT_RUNTIME_COMMUNITY_ENABLED="${AGENT_RUNTIME_COMMUNITY_ENABLED:-false}"
AGENT_RUNTIME_PERSONAL_CONTEXT_ENABLED="${AGENT_RUNTIME_PERSONAL_CONTEXT_ENABLED:-false}"
AGENT_RUNTIME_NATIVE_ALWAYS_ADMIT="${AGENT_RUNTIME_NATIVE_ALWAYS_ADMIT:-false}"
AGENT_RUNTIME_NATIVE_QUEUE_TTL_MINUTES="${AGENT_RUNTIME_NATIVE_QUEUE_TTL_MINUTES:-1440}"
AGENT_RUNTIME_NATIVE_ADMISSION_DAILY_CAP="${AGENT_RUNTIME_NATIVE_ADMISSION_DAILY_CAP:-200}"
AGENT_RUNTIME_NATIVE_LIMIT_TIMEZONE="${AGENT_RUNTIME_NATIVE_LIMIT_TIMEZONE:-Australia/Melbourne}"
AGENT_RUNTIME_NATIVE_BACKLOG_REPLAY="${AGENT_RUNTIME_NATIVE_BACKLOG_REPLAY:-false}"
AGENT_RUNTIME_MCP_EVENTS_LEASE_POLICY="${AGENT_RUNTIME_MCP_EVENTS_LEASE_POLICY:-24h}"
AGENT_RUNTIME_NATIVE_DURABLE_WATCH="${AGENT_RUNTIME_NATIVE_DURABLE_WATCH:-false}"
AGENT_RUNTIME_NATIVE_DENIAL_GRACE_SECONDS="${AGENT_RUNTIME_NATIVE_DENIAL_GRACE_SECONDS:-600}"
AGENT_RUNTIME_NATIVE_UNIFIED_GRACE_SECONDS="${AGENT_RUNTIME_NATIVE_UNIFIED_GRACE_SECONDS:-86400}"
AGENT_RUNTIME_NATIVE_RENEWAL_ENABLED="${AGENT_RUNTIME_NATIVE_RENEWAL_ENABLED:-false}"
MLAI_MCP_AGENT_GRANT_EXTENSION_ENABLED="${MLAI_MCP_AGENT_GRANT_EXTENSION_ENABLED:-false}"
MLAI_MCP_REFRESH_REPLAY_GRACE_SECONDS="${MLAI_MCP_REFRESH_REPLAY_GRACE_SECONDS:-0}"
export MLAI_MCP_NATIVE_AGENTS_ENABLED AGENT_RUNTIME_ENABLED AGENT_RUNTIME_NATIVE_ENABLED
export AGENT_RUNTIME_NATIVE_REPLIES_ENABLED AGENT_RUNTIME_MCP_EVENTS_ENABLED
export AGENT_RUNTIME_NATIVE_COMMUNITIES AGENT_RUNTIME_DOT_SETUP_URL AGENT_RUNTIME_NATIVE_PERSONAL_LIMIT
export AGENT_RUNTIME_NATIVE_SERVICE_PRIVATE_KEY AGENT_RUNTIME_NATIVE_REGISTRY_KEY
export COMMUNITY_CHAT_ROLE_SERVICE_TOKEN
export AGENT_RUNTIME_COMMUNITY_ENABLED
export AGENT_RUNTIME_PERSONAL_CONTEXT_ENABLED
export AGENT_RUNTIME_NATIVE_ALWAYS_ADMIT
export AGENT_RUNTIME_NATIVE_QUEUE_TTL_MINUTES
export AGENT_RUNTIME_NATIVE_ADMISSION_DAILY_CAP
export AGENT_RUNTIME_NATIVE_LIMIT_TIMEZONE
export AGENT_RUNTIME_NATIVE_BACKLOG_REPLAY
export AGENT_RUNTIME_MCP_EVENTS_LEASE_POLICY
export AGENT_RUNTIME_NATIVE_DURABLE_WATCH
export AGENT_RUNTIME_NATIVE_DENIAL_GRACE_SECONDS
export AGENT_RUNTIME_NATIVE_UNIFIED_GRACE_SECONDS
export AGENT_RUNTIME_NATIVE_RENEWAL_ENABLED
export MLAI_MCP_AGENT_GRANT_EXTENSION_ENABLED
export MLAI_MCP_REFRESH_REPLAY_GRACE_SECONDS
python3 scripts/validate_native_agent_deploy_config.py
case "$COMMUNITY_CHAT_PASSWORD_AUTH_ENABLED" in
    true|TRUE|True) COMMUNITY_CHAT_PASSWORD_AUTH_ENABLED=true ;;
    false|FALSE|False) COMMUNITY_CHAT_PASSWORD_AUTH_ENABLED=false ;;
    *) echo "COMMUNITY_CHAT_PASSWORD_AUTH_ENABLED must be true or false." >&2; exit 1 ;;
esac
case "$COMMUNITY_BRIDGE_DEPENDENCY_VERDICTS_ENABLED" in
    true|TRUE|True) COMMUNITY_BRIDGE_DEPENDENCY_VERDICTS_ENABLED=true ;;
    false|FALSE|False) COMMUNITY_BRIDGE_DEPENDENCY_VERDICTS_ENABLED=false ;;
    *) echo "COMMUNITY_BRIDGE_DEPENDENCY_VERDICTS_ENABLED must be true or false." >&2; exit 1 ;;
esac
export COMMUNITY_BRIDGE_DEPENDENCY_VERDICTS_ENABLED
case "$MESSAGE_SYNC_ENABLED" in
    true|TRUE|True) MESSAGE_SYNC_ENABLED=true ;;
    false|FALSE|False) MESSAGE_SYNC_ENABLED=false ;;
    *) echo "MESSAGE_SYNC_ENABLED must be true or false." >&2; exit 1 ;;
esac
export MESSAGE_SYNC_ENABLED
case "$MESSAGE_SYNC_QUIET_HEAD_BACKOFF_ENABLED" in
    true|TRUE|True) MESSAGE_SYNC_QUIET_HEAD_BACKOFF_ENABLED=true ;;
    false|FALSE|False) MESSAGE_SYNC_QUIET_HEAD_BACKOFF_ENABLED=false ;;
    *) echo "MESSAGE_SYNC_QUIET_HEAD_BACKOFF_ENABLED must be true or false." >&2; exit 1 ;;
esac
export MESSAGE_SYNC_QUIET_HEAD_BACKOFF_ENABLED
for responsiveness_flag in MESSAGE_SYNC_OPEN_PRIORITY_ENABLED MESSAGE_SYNC_UNREAD_PREFETCH_ENABLED MESSAGE_SYNC_RECENT_HISTORY_ENABLED MESSAGE_SYNC_TARGETED_STAGED_ROUTING; do
    case "${!responsiveness_flag}" in
        true|TRUE|True) printf -v "$responsiveness_flag" '%s' true ;;
        false|FALSE|False) printf -v "$responsiveness_flag" '%s' false ;;
        *) echo "$responsiveness_flag must be true or false." >&2; exit 1 ;;
    esac
    export "$responsiveness_flag"
done
export SLACK_MEMBER_CHANNEL_ALLOWED_IDS
if ! [[ "$MESSAGE_SYNC_UNREAD_PREFETCH_PER_HOUR" =~ ^[1-9][0-9]{0,2}$ ]] || [ "$MESSAGE_SYNC_UNREAD_PREFETCH_PER_HOUR" -gt 256 ]; then
    echo "MESSAGE_SYNC_UNREAD_PREFETCH_PER_HOUR must be an integer from 1 to 256." >&2
    exit 1
fi
export MESSAGE_SYNC_UNREAD_PREFETCH_PER_HOUR
if ! [[ "$MESSAGE_SYNC_DEVICE_FANOUT_BATCH" =~ ^(0|[1-9][0-9]?)$ ]] || [ "$MESSAGE_SYNC_DEVICE_FANOUT_BATCH" -gt 50 ]; then
    echo "MESSAGE_SYNC_DEVICE_FANOUT_BATCH must be an integer from 0 to 50." >&2
    exit 1
fi
export MESSAGE_SYNC_DEVICE_FANOUT_BATCH

case "$SLACK_OWNER_INVENTORY_ENABLED" in
    true|TRUE|True) SLACK_OWNER_INVENTORY_ENABLED=true ;;
    false|FALSE|False) SLACK_OWNER_INVENTORY_ENABLED=false ;;
    *) echo "SLACK_OWNER_INVENTORY_ENABLED must be true or false." >&2; exit 1 ;;
esac
if [ "$SLACK_OWNER_INVENTORY_ENABLED" = "true" ] && [ "$MESSAGE_SYNC_ENABLED" != "true" ]; then
    echo "SLACK_OWNER_INVENTORY_ENABLED requires MESSAGE_SYNC_ENABLED=true." >&2
    exit 1
fi
export SLACK_OWNER_INVENTORY_ENABLED

case "$MEETING_ROOM_BOOKING_ENABLED" in
    true|TRUE|True|1|yes|YES|Yes|on|ON|On) MEETING_ROOM_BOOKING_ENABLED=true ;;
    false|FALSE|False|0|no|NO|No|off|OFF|Off|"") MEETING_ROOM_BOOKING_ENABLED=false ;;
    *)
        echo "❌ MEETING_ROOM_BOOKING_ENABLED must be true or false."
        exit 1
        ;;
esac
export MEETING_ROOM_BOOKING_ENABLED

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
        "${BUZZ_BRIDGE_ADAPTER_URL:-}"
        "${BUZZ_BRIDGE_ADAPTER_TOKEN:-}"
        "${BUZZ_BRIDGE_CALLBACK_SECRET:-}"
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
python3 scripts/validate_linear_channel_issue_deploy_config.py
python3 scripts/validate_message_sync_deploy_config.py
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
verify_current_main_release() {
    local current_main_sha
    if [[ ! "$APP_RELEASE" =~ ^[0-9a-f]{40}$ ]]; then
        echo "❌ APP_RELEASE must be the full main commit SHA." >&2
        return 1
    fi
    if ! current_main_sha=$(git ls-remote https://github.com/MLAI-AUS-Inc/mlai-backend.git refs/heads/main | awk '{print $1}'); then
        echo "❌ Cannot verify the current main commit; refusing to deploy." >&2
        return 1
    fi
    if [ "$current_main_sha" != "$APP_RELEASE" ]; then
        echo "❌ Stale deployment $APP_RELEASE; current main is $current_main_sha. No host files or containers were changed." >&2
        return 1
    fi
}

# A delayed push event can start after a newer release. Reject it before rsync
# or the first credential/configuration write reaches the host.
verify_current_main_release
echo "🚀 Deploying release $APP_RELEASE to $DEPLOY_SSH_TARGET ($DROPLET_IP)..."

# 1. Sync files to the server
echo "📦 Syncing files..."
rsync -avz --delete --exclude-from scripts/deploy-excludes.txt . "$DEPLOY_SSH_TARGET:$PROJECT_DIR"

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
if [ -n "${MLAI_CHAT_ACCOUNT_KEY_SECRET:-}" ]; then
    install_remote_env_secret MLAI_CHAT_ACCOUNT_KEY_SECRET "$MLAI_CHAT_ACCOUNT_KEY_SECRET"
fi
echo "🔐 Updating Linear channel issue credential (value redacted)..."
install_remote_env_secret LINEAR_API_KEY "$LINEAR_API_KEY"
if [ -n "$LISTMONK_API_TOKEN" ]; then
    install_remote_env_secret LISTMONK_API_TOKEN "$LISTMONK_API_TOKEN"
fi
if [ -n "${CRM_RESEND_WEBHOOK_SECRET:-}" ]; then
    install_remote_env_secret CRM_RESEND_WEBHOOK_SECRET "$CRM_RESEND_WEBHOOK_SECRET"
fi
echo "🔐 Updating distinct Roo and internal service credentials (values redacted)..."
install_remote_env_secret ROO_API_KEY "$ROO_API_KEY"
install_remote_env_secret INTERNAL_API_KEY "$INTERNAL_API_KEY"
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

# Always install optional empty values too: clearing a repository variable must
# remove a previous release's canary list or exact migration approval.
install_remote_env_value WEBSITE_CONNECTION_WRITE_MODE "$WEBSITE_CONNECTION_WRITE_MODE"
install_remote_env_value WEBSITE_CONNECTION_CANARY_DOMAINS "$WEBSITE_CONNECTION_CANARY_DOMAINS"
install_remote_env_value APPROVED_MIGRATION_PLAN_SHA256 "$APPROVED_MIGRATION_PLAN_SHA256"

echo "🔧 Updating MLAI and Valley MCP configuration..."
install_remote_env_value MLAI_MCP_ENABLED "$MLAI_MCP_ENABLED"
install_remote_env_value MLAI_MCP_COMMUNITY_ENABLED "$MLAI_MCP_COMMUNITY_ENABLED"
for mcp_setting in MLAI_MCP_COMMUNITY_RELAY_URL MLAI_MCP_COMMUNITY_SECRET MLAI_MCP_CLIENT_INSTALL_URLS; do
    # Preserve paired relay credentials unless explicitly supplied for this release.
    if [ -n "${!mcp_setting}" ]; then
        install_remote_env_value "$mcp_setting" "${!mcp_setting}"
    fi
done
install_remote_env_value VALLEY_MCP_ENABLED "$VALLEY_MCP_ENABLED"
install_remote_env_value VALLEY_MCP_UNIFIED_ALIAS_ENABLED "$VALLEY_MCP_UNIFIED_ALIAS_ENABLED"
install_remote_env_value VALLEY_MCP_PUBLIC_BASE_URL "$VALLEY_MCP_PUBLIC_BASE_URL"
if [ -n "$COMMUNITY_CHAT_STARTUP_UPDATES_ENABLED" ]; then
    install_remote_env_value COMMUNITY_CHAT_STARTUP_UPDATES_ENABLED "$COMMUNITY_CHAT_STARTUP_UPDATES_ENABLED"
fi
for crm_setting in $CRM_DEPLOY_SETTINGS; do
    if [ -n "${!crm_setting}" ]; then
        install_remote_env_value "$crm_setting" "${!crm_setting}"
    fi
done
for articles_setting in $ARTICLES_DEPLOY_SETTINGS; do
    if [ -n "${!articles_setting}" ]; then
        install_remote_env_value "$articles_setting" "${!articles_setting}"
    fi
done
if [ -n "$VALLEY_MCP_DOMAIN_VERIFICATION_TOKEN" ]; then
    install_remote_env_value VALLEY_MCP_DOMAIN_VERIFICATION_TOKEN "$VALLEY_MCP_DOMAIN_VERIFICATION_TOKEN"
fi
for native_flag in MLAI_MCP_NATIVE_AGENTS_ENABLED AGENT_RUNTIME_ENABLED AGENT_RUNTIME_NATIVE_ENABLED \
    AGENT_RUNTIME_NATIVE_REPLIES_ENABLED AGENT_RUNTIME_MCP_EVENTS_ENABLED AGENT_RUNTIME_NATIVE_PERSONAL_LIMIT \
    AGENT_RUNTIME_COMMUNITY_ENABLED AGENT_RUNTIME_PERSONAL_CONTEXT_ENABLED AGENT_RUNTIME_NATIVE_ALWAYS_ADMIT \
    AGENT_RUNTIME_NATIVE_QUEUE_TTL_MINUTES \
    AGENT_RUNTIME_NATIVE_ADMISSION_DAILY_CAP \
    AGENT_RUNTIME_NATIVE_LIMIT_TIMEZONE \
    AGENT_RUNTIME_NATIVE_BACKLOG_REPLAY \
    AGENT_RUNTIME_MCP_EVENTS_LEASE_POLICY \
    AGENT_RUNTIME_NATIVE_DURABLE_WATCH \
    AGENT_RUNTIME_NATIVE_DENIAL_GRACE_SECONDS \
    AGENT_RUNTIME_NATIVE_UNIFIED_GRACE_SECONDS \
    AGENT_RUNTIME_NATIVE_RENEWAL_ENABLED \
    MLAI_MCP_AGENT_GRANT_EXTENSION_ENABLED \
    MLAI_MCP_REFRESH_REPLAY_GRACE_SECONDS; do
    install_remote_env_value "$native_flag" "${!native_flag}"
done
for native_value in AGENT_RUNTIME_NATIVE_COMMUNITIES AGENT_RUNTIME_DOT_SETUP_URL; do
    if [ -n "${!native_value}" ]; then
        install_remote_env_value "$native_value" "${!native_value}"
    fi
done
for native_secret in AGENT_RUNTIME_NATIVE_SERVICE_PRIVATE_KEY AGENT_RUNTIME_NATIVE_REGISTRY_KEY; do
    if [ -n "${!native_secret}" ]; then
        install_remote_env_secret "$native_secret" "${!native_secret}"
    fi
done
if [ -n "$COMMUNITY_CHAT_ROLE_SERVICE_TOKEN" ]; then
    install_remote_env_secret COMMUNITY_CHAT_ROLE_SERVICE_TOKEN "$COMMUNITY_CHAT_ROLE_SERVICE_TOKEN"
fi

# The app-level token travels only through SSH stdin. Never reuse an OAuth
# bot/user token: recipient expansion is a separate Slack authorization surface.
install_remote_env_value MESSAGE_SYNC_ENABLED "$MESSAGE_SYNC_ENABLED"
install_remote_env_value MESSAGE_SYNC_RECENT_HISTORY_ENABLED "$MESSAGE_SYNC_RECENT_HISTORY_ENABLED"
install_remote_env_value SLACK_MEMBER_CHANNEL_ALLOWED_IDS "$SLACK_MEMBER_CHANNEL_ALLOWED_IDS"
install_remote_env_value COMMUNITY_BRIDGE_DEPENDENCY_VERDICTS_ENABLED "$COMMUNITY_BRIDGE_DEPENDENCY_VERDICTS_ENABLED"
install_remote_env_value MESSAGE_SYNC_QUIET_HEAD_BACKOFF_ENABLED "$MESSAGE_SYNC_QUIET_HEAD_BACKOFF_ENABLED"
install_remote_env_value MESSAGE_SYNC_OPEN_PRIORITY_ENABLED "$MESSAGE_SYNC_OPEN_PRIORITY_ENABLED"
install_remote_env_value MESSAGE_SYNC_UNREAD_PREFETCH_ENABLED "$MESSAGE_SYNC_UNREAD_PREFETCH_ENABLED"
install_remote_env_value MESSAGE_SYNC_UNREAD_PREFETCH_PER_HOUR "$MESSAGE_SYNC_UNREAD_PREFETCH_PER_HOUR"
install_remote_env_value MESSAGE_SYNC_TARGETED_STAGED_ROUTING "$MESSAGE_SYNC_TARGETED_STAGED_ROUTING"
install_remote_env_value MESSAGE_SYNC_DEVICE_FANOUT_BATCH "$MESSAGE_SYNC_DEVICE_FANOUT_BATCH"
for inbox_flag_name in \
    COMMUNITY_CHAT_MEMBER_ACCOUNTS_ENABLED COMMUNITY_CHAT_INBOX_ERASURE_ENABLED \
    MESSAGE_SYNC_TARGETED_READ_POLLING MESSAGE_SYNC_INBOX_READ_EXPORT \
    MESSAGE_SYNC_INBOX_CURSOR_PUSH MESSAGE_SYNC_INBOX_MENTIONS MESSAGE_SYNC_RELAY_READ_COUNTS; do
    install_remote_env_value "$inbox_flag_name" "${!inbox_flag_name}"
done
# Keep the new inventory endpoint disabled during migration and every
# pre-activation check, even when this host was previously enabled.
install_remote_env_value SLACK_OWNER_INVENTORY_ENABLED "false"
# Install callback verification before activation so Slack can verify its URL.
if [ -n "${MESSAGE_SYNC_SLACK_USER_APP_ID:-}" ]; then
    install_remote_env_value MESSAGE_SYNC_SLACK_USER_APP_ID "$MESSAGE_SYNC_SLACK_USER_APP_ID"
    install_remote_env_secret MESSAGE_SYNC_SLACK_USER_SIGNING_SECRET "$MESSAGE_SYNC_SLACK_USER_SIGNING_SECRET"
    if [ -n "${MESSAGE_SYNC_SLACK_USER_APP_TOKEN:-}" ]; then
        install_remote_env_secret MESSAGE_SYNC_SLACK_USER_APP_TOKEN "$MESSAGE_SYNC_SLACK_USER_APP_TOKEN"
    fi
fi
if [ "$MESSAGE_SYNC_ENABLED" = "true" ]; then
    install_remote_env_secret MESSAGE_SYNC_SLACK_APP_TOKEN "$MESSAGE_SYNC_SLACK_APP_TOKEN"
    install_remote_env_value MESSAGE_SYNC_SLACK_APP_ID "$MESSAGE_SYNC_SLACK_APP_ID"
    install_remote_env_value MESSAGE_SYNC_SLACK_BOT_WORKSPACE_ID "$MESSAGE_SYNC_SLACK_BOT_WORKSPACE_ID"
    install_remote_env_value MESSAGE_SYNC_SLACK_DISTRIBUTION "${MESSAGE_SYNC_SLACK_DISTRIBUTION:-restricted}"
fi

echo "🔧 Updating Linear channel issue reader configuration..."
install_remote_env_value LINEAR_MEETING_REQUIRED_TEAM_KEYS "$LINEAR_MEETING_REQUIRED_TEAM_KEYS"
install_remote_env_value LINEAR_CHANNEL_ISSUE_BINDINGS_JSON "$LINEAR_CHANNEL_ISSUE_BINDINGS_JSON"
install_remote_env_value LINEAR_CHANNEL_ISSUE_MAX_COMMENTS "$LINEAR_CHANNEL_ISSUE_MAX_COMMENTS"
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
# SSH hands this script to the remote shell on stdin, and bash reads stdin one
# command at a time. A host command that reads stdin (Compose exec attaches it
# even with -T) would swallow the rest of the script, and the shell would then
# exit 0 having skipped every later step. On 25 Oct 2026 that skipped the
# release tail and left the migration writer pause marker behind. Bash parses
# the whole group below before running any of it, and the group's stdin is
# /dev/null, so no command can reach the script text.
remote_deploy_log=$(mktemp)
trap 'rm -f "$remote_deploy_log"' EXIT
remote_deploy_completed="Remote deployment script completed for $APP_RELEASE."
ssh "$DEPLOY_SSH_TARGET" <<EOF | tee "$remote_deploy_log"
{
    set -euo pipefail

    verify_current_main_release_on_host() {
        local current_main_sha
        if ! current_main_sha=\$(git ls-remote https://github.com/MLAI-AUS-Inc/mlai-backend.git refs/heads/main | awk '{print \$1}'); then
            echo "❌ Cannot verify current main from the host; refusing to mutate runtime." >&2
            return 1
        fi
        if [ "\$current_main_sha" != "$APP_RELEASE" ]; then
            echo "❌ Stale deployment $APP_RELEASE; current main is \$current_main_sha. Refusing to mutate runtime." >&2
            return 1
        fi
    }

    # Recheck after file/env sync and again at the transition boundary. A
    # newer push may have arrived while the runner was copying files.
    verify_current_main_release_on_host

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
    slack_owner_inventory_enabled="$SLACK_OWNER_INVENTORY_ENABLED"
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

    # Retired features have no runtime entry points or credential consumers.
    sed -i -E '/^(OFFICE_MANAGER_|VICTOR_AI_ROO_|COMMITTEE_REMUNERATION_)[A-Z0-9_]*=/d; /^CUSTOMERIO_VICTOR_REGISTRATION_TEMPLATE_ID=/d' .env

    # Update .env with production values (Run on every deploy)
    upsert_env_value DEBUG "False"
    upsert_env_value ALLOWED_HOSTS "api.mlai.au,209.38.85.60,10.126.0.2,localhost,127.0.0.1,esafety.localhost"
    # Plane owns admin.mlai.au after cutover, so it must not be trusted as a
    # credentialed browser origin for the MLAI API. Rollback routes to ops;
    # MLAI Chat remains a separately required credentialed origin.
    upsert_env_value CORS_ALLOWED_ORIGINS "https://mlai.au,https://www.mlai.au,https://ops.mlai.au,https://chat.mlai.au"
    upsert_env_value CSRF_TRUSTED_ORIGINS "https://mlai.au,https://www.mlai.au,https://api.mlai.au,https://ops.mlai.au,https://chat.mlai.au"
    upsert_env_value DEFAULT_BACKEND_URL "https://api.mlai.au"
    upsert_env_value DEFAULT_FRONTEND_URL "https://mlai.au"
    upsert_env_value COMMUNITY_CHAT_API_AUDIENCE "https://api.mlai.au"
    upsert_env_value COMMUNITY_CHAT_FRONTEND_URL "https://chat.mlai.au"
    upsert_env_value COMMUNITY_CHAT_RELAY_URL "wss://chat.mlai.au"
    upsert_env_value COMMUNITY_CHAT_ADAPTER_URL "$COMMUNITY_CHAT_ADAPTER_URL"
    upsert_env_value COMMUNITY_CHAT_EMAIL_CODE_AUTH_ENABLED "true"
    upsert_env_value COMMUNITY_CHAT_PASSWORD_AUTH_ENABLED "$COMMUNITY_CHAT_PASSWORD_AUTH_ENABLED"
    upsert_env_value COMMUNITY_CHAT_DEVICE_AUTH_ENABLED "true"
    upsert_env_value CUSTOMERIO_COMMUNITY_CHAT_CODE_MESSAGE_ID "mlai_chat_sign_in_code"
    upsert_env_value COMMUNITY_CHAT_ALLOWED_ORIGINS "https://chat.mlai.au,tauri://localhost,http://tauri.localhost,mlaichat://callback"
    upsert_env_value MEETING_ROOM_BOOKING_ENABLED "\$meeting_room_booking_enabled"
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
    # .env may retain a failed release's marker even when the old container
    # never changed. Use the serving container as rollback truth.
    previous_app_release=\$(docker compose exec -T web sh -lc 'printf "%s" "\$APP_RELEASE"' </dev/null 2>/dev/null || read_env_value APP_RELEASE)
    # The staging .env may already be false; capture the running contract so a
    # code-only rollback does not silently disable established member discovery.
    previous_owner_inventory_enabled=\$(docker compose exec -T web sh -lc 'printf "%s" "\$SLACK_OWNER_INVENTORY_ENABLED"' </dev/null 2>/dev/null || printf false)
    case "\$previous_owner_inventory_enabled" in true|false) ;; *) echo "Invalid previous owner inventory state" >&2; exit 1 ;; esac
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
    # Web concurrency: keep URL imports and request CPU within this 4-vCPU
    # droplet's capacity. The previous 16 sync workers triggered simultaneous
    # cold imports and 30-second worker timeouts. Firebase initializes a
    # Firestore gRPC client during route import, so warming must be post-fork.
    upsert_env_value GUNICORN_WORKERS "4"
    upsert_env_value GUNICORN_TIMEOUT "90"
    print_redacted_env_status CONTENT_FACTORY_URL GITHUB_APP_ID GITHUB_APP_PRIVATE_KEY VALLEY_HARNESS_URL REDIS_URL ROO_SERVICE_URL ROO_SIM_PATIENT_KEY HEALTH_HACK_API_KEY ROO_API_KEY INTERNAL_API_KEY UMAMI_BASE_URL CONTENT_ANALYTICS_HOST_URL COMMUNITY_CHAT_ADAPTER_URL COMMUNITY_CHAT_ADAPTER_TOKEN COMMUNITY_CHAT_EMAIL_CODE_PEPPER COMMUNITY_CHAT_EMAIL_CODE_DELIVERY_SECRET CUSTOMERIO_API_KEY CUSTOMERIO_COMMUNITY_CHAT_CODE_MESSAGE_ID
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
    health_hack_key=\$(read_env_value HEALTH_HACK_API_KEY)
    roo_sim_key=\$(read_env_value ROO_SIM_PATIENT_KEY)
    roo_api_key=\$(read_env_value ROO_API_KEY)
    internal_api_key=\$(read_env_value INTERNAL_API_KEY)
    if [ "\${#health_hack_key}" -lt 32 ] || [ "\${#roo_sim_key}" -lt 32 ] || [ "\${#roo_api_key}" -lt 32 ] || [ "\${#internal_api_key}" -lt 32 ]; then
        echo "❌ HEALTH_HACK_API_KEY, ROO_SIM_PATIENT_KEY, ROO_API_KEY and INTERNAL_API_KEY must each contain at least 32 characters."
        exit 1
    fi
    if [ "\$health_hack_key" = "\$roo_sim_key" ] || [ "\$health_hack_key" = "\$roo_api_key" ] || [ "\$roo_sim_key" = "\$roo_api_key" ] || [ "\$roo_api_key" = "\$internal_api_key" ]; then
        echo "❌ Roo, internal, Health Hack and simulated-patient credentials must preserve their documented trust-domain boundaries."
        exit 1
    fi
    unset health_hack_key roo_sim_key roo_api_key internal_api_key

    # A removed Compose service may still exist from an older deployment.
    # Stop only the retired payout service before any schema/write transition.
    retired_committee_container_ids=\$(docker ps --all --quiet \
        --filter label=com.docker.compose.project=mlai-backend \
        --filter label=com.docker.compose.service=committee-remuneration)
    if [ -n "\$retired_committee_container_ids" ]; then
        docker stop \$retired_committee_container_ids
        docker rm \$retired_committee_container_ids
    fi
    unset retired_committee_container_ids

    source scripts/runtime-services.sh
    # The reconciler remains independent from issuance so uncertain coupon
    # writes continue to drain when new quotes are switched off.
    if [ "\$(read_env_value ROO_EVENT_DISCOUNT_WORKER_ENABLED)" = "true" ]; then
        runtime_services+=("\${event_discount_runtime_services[@]}")
    elif [ "\$(read_env_value ROO_EVENT_DISCOUNTS_ENABLED)" = "true" ]; then
        echo "Event discount issuance requires its supervised reconciliation worker."
        exit 1
    else
        docker compose stop event-discount-worker || true
        docker compose rm -f event-discount-worker || true
    fi
    if [ "\$(read_env_value AGENT_RUNTIME_COMMUNITY_ENABLED)" = "true" ]; then
        runtime_services+=("\${community_agent_runtime_services[@]}")
    else
        docker compose stop community-agent-worker || true
        docker compose rm -f community-agent-worker || true
    fi
    if [ "\$(read_env_value MLAI_MCP_NATIVE_AGENTS_ENABLED)" = "true" ]; then
        runtime_services+=("\${native_agent_runtime_services[@]}")
        native_agent_worker_enabled=1
    else
        native_agent_worker_enabled=0
    fi
    if [ "\$community_bridge_production_enabled" = "true" ] \
        && env_has_value SLACK_BRIDGE_BOT_TOKEN \
        && { env_has_value DISCORD_BRIDGE_BOT_TOKEN \
            || { env_has_value BUZZ_BRIDGE_ADAPTER_URL \
                && env_has_value BUZZ_BRIDGE_ADAPTER_TOKEN \
                && env_has_value BUZZ_BRIDGE_CALLBACK_SECRET; }; }; then
        runtime_services+=("\${bridge_runtime_services[@]}")
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
        runtime_services+=("\${analytics_runtime_services[@]}")
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

    # The database imports the long-lived .env, including APP_RELEASE and
    # deployment-managed feature flags. A normal Compose up of db treats those changes
    # as a config change and recreates Postgres during every code release.
    # Start a missing/stopped database, but never replace a running database
    # as a side effect of deploying application code.
    echo "🐘 Ensuring database is running without replacement..."
    docker compose up -d --no-recreate db

    echo "🏗️ Building shared backend and Jobs browser images..."
    verify_current_main_release_on_host
    docker compose build web jobs-worker

    # Every pre-migration gate (Redis security state, production URLs and
    # service connectivity, memory provider governance, PostgreSQL vector
    # support, GitHub App credentials) plus the migration plan, in one
    # container. They used to be six separate compose-run invocations that
    # spent ~35s on cold Django starts to do a few seconds of work.
    # (No backticks in this heredoc: it is unquoted, so they would be
    # command-substituted on the deploying runner.)
    echo "🧪 Running deployment preflight..."
    compose_run_web python manage.py deploy_preflight
    compose_run_web python scripts/validate_native_agent_deploy_config.py --provider

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

    web_proxy_config=/etc/nginx/conf.d/mlai-backend-api.conf
    web_proxy_script=ops/backend-api/switch-web-upstream.sh
    web_proxy_preexisting=0
    web_proxy_staged=0
    web_proxy_switch_attempted=0
    web_proxy_candidate_verified=0
    web_direct_stopped=0
    web_candidate_started=0
    web_candidate_drain_workers=""
    if [ -e "\$web_proxy_config" ]; then
        if ! grep -qx '# managed-mlai-backend-api target=web' "\$web_proxy_config"; then
            echo "❌ API proxy is not routing to the normal web slot; refusing to replace a potentially serving candidate." >&2
            exit 1
        fi
        web_proxy_preexisting=1
    fi
    if [ -n "\$(docker compose ps -a -q web-candidate)" ]; then
        echo "❌ A candidate web container remains from an earlier release; inspect its Nginx drain state before replacing it." >&2
        exit 1
    fi

    nginx_worker_snapshot() {
        local master_pid
        master_pid=\$(cat /run/nginx.pid) || return 1
        if ! [[ "\$master_pid" =~ ^[0-9]+$ ]] || ! kill -0 "\$master_pid" 2>/dev/null; then
            echo "❌ Cannot identify the running Nginx master; refusing to drain a web slot." >&2
            return 1
        fi
        if ! pgrep -P "\$master_pid"; then
            echo "❌ Cannot identify Nginx workers; refusing to replace a web slot." >&2
            return 1
        fi
    }

    wait_for_nginx_workers_to_drain() {
        local workers="\$1"
        local pid still_running attempt
        [ -n "\$workers" ] || return 0
        for attempt in \$(seq 1 120); do
            still_running=0
            for pid in \$workers; do
                if kill -0 "\$pid" 2>/dev/null; then
                    still_running=1
                    break
                fi
            done
            [ "\$still_running" = 0 ] && return 0
            sleep 1
        done
        echo "❌ Old Nginx workers still have in-flight requests after 120 seconds." >&2
        return 1
    }

    wait_for_origin_web_health() {
        local port="\$1" expected_release="\$2" expected_slot="\${3:-}"
        local max_attempts="\${4:-45}" curl_timeout="\${5:-5}" retry_delay="\${6:-2}"
        local headers body attempt
        headers=\$(mktemp)
        for attempt in \$(seq 1 "\$max_attempts"); do
            body=\$(curl -fsS --max-time "\$curl_timeout" -D "\$headers" \
                -H 'Host: api.mlai.au' -H 'X-Forwarded-Proto: https' \
                "http://127.0.0.1:\$port/healthz/ready" 2>/dev/null || true)
            if printf '%s\n' "\$body" | python3 -c \
                'import json,sys; data=json.load(sys.stdin); sys.exit(0 if data.get("status") == "ok" and data.get("release") == sys.argv[1] else 1)' \
                "\${expected_release:0:12}" 2>/dev/null \
                && { [ -z "\$expected_slot" ] \
                    || grep -iF "X-MLAI-Origin-Web-Slot: \$expected_slot" "\$headers" >/dev/null; }; then
                rm -f "\$headers"
                return 0
            fi
            sleep "\$retry_delay"
        done
        rm -f "\$headers"
        echo "❌ Web slot \${expected_slot:-\$port} did not report release \${expected_release:0:12}." >&2
        return 1
    }

    runtime_restore_attempted=0
    web_only_rollback=0
    runtime_pause_started=0
    new_runtime_replacement_started=0
    migration_started=0
    schema_transition_started=0
    schema_transition_completed=0
    writer_pause_sentinel=/run/mlai-backend-writers-paused
    describe_writer_pause_sentinel() {
        local paused_release paused_at
        paused_release=\$(head -n 1 "\$writer_pause_sentinel" 2>/dev/null || true)
        paused_at=\$(date -u -r "\$writer_pause_sentinel" '+%Y-%m-%d %H:%M:%S UTC' 2>/dev/null || true)
        printf '%s (release %s, written %s)' "\$writer_pause_sentinel" "\${paused_release:-unknown}" "\${paused_at:-at an unknown time}"
    }
    writer_watchdog_units_to_restore=()
    pause_host_writer_watchdogs() {
        local units unit exec_start active_state watchdog_processes watchdog_status
        # The host may still run a watchdog from /srv rather than the checkout
        # being deployed to /root. Discover its systemd unit by ExecStart, not
        # by a guessed unit name, and stop it before pausing any writer.
        if ! units=\$(systemctl list-units --all --type=service --plain --no-legend | awk '{print (\$1 == "●" ? \$2 : \$1)}'); then
            echo "❌ Cannot inventory host watchdog units; migration has not started." >&2
            return 1
        fi
        for unit in \$units; do
            if ! exec_start=\$(systemctl show --property=ExecStart --value "\$unit"); then
                echo "❌ Cannot inspect host service \$unit; migration has not started." >&2
                return 1
            fi
            case "\$exec_start" in
                *docker-health-watchdog.sh*) ;;
                *) continue ;;
            esac
            if ! active_state=\$(systemctl show --property=ActiveState --value "\$unit"); then
                echo "❌ Cannot inspect watchdog state for \$unit; migration has not started." >&2
                return 1
            fi
            case "\$active_state" in
                inactive|failed) ;;
                *) writer_watchdog_units_to_restore+=("\$unit") ;;
            esac
            if ! systemctl stop "\$unit"; then
                echo "❌ Could not stop host watchdog \$unit; migration has not started." >&2
                return 1
            fi
            if [ "\$(systemctl show --property=ActiveState --value "\$unit")" != "inactive" ]; then
                echo "❌ Host watchdog \$unit is still active; migration has not started." >&2
                return 1
            fi
        done
        if ! command -v pgrep >/dev/null 2>&1; then
            echo "❌ Cannot check for unmanaged host watchdog processes; migration has not started." >&2
            return 1
        fi
        watchdog_processes=\$(pgrep -af '[d]ocker-health-watchdog.sh') && watchdog_status=0 || watchdog_status=\$?
        if [ "\$watchdog_status" -gt 1 ] || [ -n "\$watchdog_processes" ]; then
            echo "❌ A host watchdog remains running or cannot be inspected; migration has not started." >&2
            return 1
        fi
    }
    restore_host_writer_watchdogs() {
        local unit
        # Bash 3.2 treats expansion of an empty array as unbound under set -u.
        if [ "\${#writer_watchdog_units_to_restore[@]}" -eq 0 ]; then
            return 0
        fi
        for unit in "\${writer_watchdog_units_to_restore[@]}"; do
            systemctl start "\$unit" || return 1
            if [ "\$(systemctl show --property=ActiveState --value "\$unit")" != "active" ]; then
                echo "❌ Host watchdog \$unit did not resume." >&2
                return 1
            fi
        done
        writer_watchdog_units_to_restore=()
    }
    pause_runtime_writers_for_migration() {
        local service container_ids container_id running
        if [ -e "\$writer_pause_sentinel" ]; then
            echo "❌ A prior migration writer pause is still active; operator review is required." >&2
            echo "   \$(describe_writer_pause_sentinel)" >&2
            return 1
        fi
        # The host watchdog must see the pause before any Compose service is
        # stopped, or it could restart web during a long schema transition.
        if ! printf '%s\n' "$APP_RELEASE" > "\$writer_pause_sentinel"; then
            rm -f "\$writer_pause_sentinel" || true
            return 1
        fi
        if ! pause_host_writer_watchdogs; then
            # No Compose writer has been touched. Keep the previous runtime
            # online and undo any watchdog stops that completed before failure.
            if restore_host_writer_watchdogs; then
                rm -f "\$writer_pause_sentinel" || true
            fi
            return 1
        fi
        runtime_pause_started=1
        if ! docker compose stop "\${all_runtime_writer_services[@]}"; then
            echo "❌ Could not stop every runtime writer; migration has not started." >&2
            return 1
        fi
        for service in "\${all_runtime_writer_services[@]}"; do
            if ! container_ids=\$(docker compose ps --all --quiet "\$service"); then
                echo "❌ Could not inspect stopped writer service \$service; migration has not started." >&2
                return 1
            fi
            for container_id in \$container_ids; do
                if ! running=\$(docker inspect --format '{{.State.Running}}' "\$container_id"); then
                    echo "❌ Could not inspect writer container \$container_id; migration has not started." >&2
                    return 1
                fi
                if [ "\$running" != "false" ]; then
                    echo "❌ Writer service \$service is still running (\$container_id); migration has not started." >&2
                    return 1
                fi
            done
        done
    }
    restore_runtime_on_error() {
        if [ "\$runtime_restore_attempted" = "1" ]; then
            return
        fi
        runtime_restore_attempted=1
        trap - ERR
        set +e

        if [ "\${migrations_pending:-1}" = "0" ] && [ "\$web_candidate_started" = "1" ]; then
            if [ "\$web_proxy_preexisting" != "1" ] \
                && [ "\$web_proxy_candidate_verified" = "1" ]; then
                # Once first adoption successfully serves the candidate, keep
                # Nginx on port 80 during rollback and recreate old web on 8001.
                if [ "\$new_runtime_replacement_started" != "1" ]; then
                    web_only_rollback=1
                fi
                new_runtime_replacement_started=1
            fi
            if [ "\$new_runtime_replacement_started" != "1" ]; then
                echo "⚠️ Restoring the original web route before runtime replacement."
                if [ "\$web_proxy_preexisting" = "1" ] && [ "\$web_proxy_switch_attempted" = "1" ]; then
                    drain_workers=\$(nginx_worker_snapshot) || return
                    bash "\$web_proxy_script" switch web || return
                    wait_for_origin_web_health 80 "\$previous_app_release" web || return
                    wait_for_nginx_workers_to_drain "\$drain_workers" || return
                elif [ "\$web_proxy_preexisting" != "1" ] && [ "\$web_proxy_staged" = "1" ]; then
                    drain_workers=\$(nginx_worker_snapshot) || return
                    bash "\$web_proxy_script" remove || return
                    if [ "\$web_direct_stopped" = "1" ]; then
                        old_web_running=\$(docker inspect --format '{{.State.Running}}' "\$old_direct_web_container_id" 2>/dev/null || true)
                        if [ "\$old_web_running" != "true" ]; then
                            direct_restored=0
                            for attempt in \$(seq 1 60); do
                                if docker start "\$old_direct_web_container_id" >/dev/null 2>&1; then
                                    direct_restored=1
                                    break
                                fi
                                sleep 0.25
                            done
                            [ "\$direct_restored" = "1" ] || { echo "❌ Old direct web could not reclaim port 80." >&2; return; }
                        fi
                        wait_for_origin_web_health 80 "\$previous_app_release" || return
                    fi
                    wait_for_nginx_workers_to_drain "\$drain_workers" || return
                fi
                docker compose stop web-candidate || true
                docker compose rm -f web-candidate || true
                if [ -n "\$previous_app_release" ]; then
                    upsert_env_value APP_RELEASE "\$previous_app_release"
                fi
                return
            fi

            # A replacement web may be unhealthy. Serve from the still-healthy
            # candidate while the old image is recreated on the normal slot.
            wait_for_origin_web_health 8002 "$APP_RELEASE" "" 8 3 1 || return
            if ! grep -qx '# managed-mlai-backend-api target=candidate' "\$web_proxy_config"; then
                drain_workers=\$(nginx_worker_snapshot) || return
                bash "\$web_proxy_script" switch candidate || return
                wait_for_origin_web_health 80 "$APP_RELEASE" candidate || return
                wait_for_nginx_workers_to_drain "\$drain_workers" || return
            fi
        fi

        if [ "\$runtime_pause_started" != "1" ] \
            && [ "\$new_runtime_replacement_started" != "1" ]; then
            echo "⚠️ Deployment failed before runtime replacement; existing services remain running."
            if [ "\${migrations_pending:-1}" = "0" ] && [ -n "\$previous_app_release" ]; then
                upsert_env_value APP_RELEASE "\$previous_app_release"
            fi
            return
        fi

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
                # Keep the watchdog pause until an operator repairs the schema.
                return
            fi
        fi

        # Incomplete schema transitions returned above with all writers stopped.
        # A compatible old-image rollback restores its actual enabled state;
        # completed schema transitions use the reviewed new runtime setting.
        recovery_inventory_enabled="\${slack_owner_inventory_enabled:-false}"
        if [ "\$migration_started" != "1" ]; then
            recovery_inventory_enabled="\${previous_owner_inventory_enabled:-false}"
        fi
        upsert_env_value SLACK_OWNER_INVENTORY_ENABLED "\$recovery_inventory_enabled" || return
        if [ "\$migration_started" != "1" ]; then
            # No schema changed, so the previous image is the last known-good
            # release even if a replacement container has already started.
            # Restore its recorded image tag before recreating services.
            echo "⚠️ Deployment failed before schema advancement; restoring the last known-good runtime images."
            if [ -n "\$previous_app_release" ]; then
                upsert_env_value APP_RELEASE "\$previous_app_release"
            fi
            restored_services=()
            rollback_override=\$(mktemp)
            printf 'services:\n' > "\$rollback_override"
            while IFS='|' read -r service image_id image_ref rollback_tag; do
                [ -n "\$service" ] || continue
                if [ "\$web_only_rollback" = "1" ] && [ "\$service" != "web" ]; then
                    continue
                fi
                docker image tag "\$image_id" "\$image_ref"
                restored_services+=("\$service")
                printf '  %s:\n    image: %s\n' "\$service" "\$rollback_tag" >> "\$rollback_override"
            done < "\$rollback_manifest"
            if [ "\$new_runtime_replacement_started" = "1" ] && [ "\$web_only_rollback" != "1" ]; then
                # Writers introduced by this release have no previous image to
                # restore. Stop them so a failed code-only rollout cannot leave
                # new workers consuming jobs alongside the restored scheduler.
                # The handoff candidate stays alive until old web is verified.
                for service in "\${runtime_services[@]}"; do
                    had_previous_image=0
                    for restored_service in "\${restored_services[@]}"; do
                        if [ "\$service" = "\$restored_service" ]; then
                            had_previous_image=1
                            break
                        fi
                    done
                    if [ "\$had_previous_image" = "0" ]; then
                        docker compose stop "\$service" || return
                    fi
                done
            fi
            if [ "\${#restored_services[@]}" -gt 0 ]; then
                if ! docker compose -f docker-compose.yml -f "\$rollback_override" up -d --no-deps --force-recreate "\${restored_services[@]}"; then
                    rm -f "\$rollback_override"
                    echo "❌ Previous runtime could not be restored; writer watchdog remains paused." >&2
                    return
                fi
            elif [ "\${#previous_runtime_container_ids[@]}" -gt 0 ]; then
                if ! docker start "\${previous_runtime_container_ids[@]}" >/dev/null; then
                    echo "❌ Previous runtime could not be restarted; writer watchdog remains paused." >&2
                    return
                fi
            else
                echo "⚠️ No prior runtime containers were recorded; leaving services stopped for operator recovery."
                return
            fi
            rm -f "\$rollback_override"
            if [ "\$web_candidate_started" = "1" ]; then
                wait_for_origin_web_health 8001 "\$previous_app_release" || return
                drain_workers=\$(nginx_worker_snapshot) || return
                bash "\$web_proxy_script" switch web || return
                wait_for_origin_web_health 80 "\$previous_app_release" web || return
                if wait_for_nginx_workers_to_drain "\$drain_workers"; then
                    docker compose stop web-candidate || true
                    docker compose rm -f web-candidate || true
                else
                    echo "⚠️ Keeping the candidate running until its in-flight requests drain." >&2
                fi
            fi
            if [ "\$web_only_rollback" = "1" ]; then
                echo "✅ Existing workers were not replaced during the failed proxy adoption."
            elif [ -n "\$previous_scheduler_container_id" ]; then
                verify_scheduler_recovery_tick \
                    "" \
                    "\$previous_scheduler_image_id" \
                    "\$previous_scheduler_tick_mtime" || true
            else
                echo "⚠️ No prior scheduler container was available to restore." >&2
            fi
            if restore_host_writer_watchdogs; then
                rm -f "\$writer_pause_sentinel" || echo "❌ Clear the writer pause sentinel manually after recovery." >&2
            else
                echo "❌ Host watchdog recovery is incomplete; writer pause sentinel remains." >&2
            fi
            return
        fi

        # Once a migration began, the old binary may be incompatible with the
        # schema. Recreate the new image with staged-off features and require
        # a fresh scheduler tick after recovery.
        if docker compose up -d --no-deps --force-recreate "\${runtime_services[@]}" \
            && verify_scheduler_recovery_tick "" "" 0; then
            if restore_host_writer_watchdogs; then
                rm -f "\$writer_pause_sentinel" || echo "❌ Clear the writer pause sentinel manually after recovery." >&2
            else
                echo "❌ Host watchdog recovery is incomplete; writer pause sentinel remains." >&2
            fi
        else
            echo "❌ New runtime recovery is incomplete; writer watchdog remains paused." >&2
        fi
    }

    # A code-only release has no schema transition. Keep the current web and
    # workers serving while the new image runs the remaining deployment gates.
    # --check returns 0 only when Django sees no pending migrations. A pending
    # plan requires its own explicit approval before any runtime is paused.
    # A database or migration-loader failure also leaves the current runtime
    # untouched.
    echo "🔎 Checking pending migrations before pausing runtime services..."
    migrations_pending=1
    if compose_run_web python manage.py migrate --check --noinput; then
        migrations_pending=0
        echo "✅ No pending migrations; current runtime stays online during deployment checks."
    else
        migration_plan=\$(compose_run_web python manage.py migrate --plan --noinput | python3 scripts/validate_website_deploy_config.py --migration-plan)
        printf '%s\n' "\$migration_plan"
        approved_plan_sha256=\$(read_env_value APPROVED_MIGRATION_PLAN_SHA256)
        actual_plan_sha256=\$(printf '%s' "\$migration_plan" | sha256sum | cut -d ' ' -f 1)
        if [ -z "\$approved_plan_sha256" ] || [ "\$approved_plan_sha256" != "\$actual_plan_sha256" ]; then
            echo "❌ Pending migration plan has no matching, specific approval; current runtime remains online." >&2
            false
        fi
    fi
    if [ "\$migrations_pending" = "1" ] && [ "\$web_proxy_preexisting" != "1" ]; then
        echo "❌ First adoption of the API proxy requires a code-only release. A reviewed schema migration needs a separate rollout." >&2
        false
    fi

    # Recovery disables errexit while attempting each restoration step. Always
    # preserve the original failure and stop; recovery is not a successful deploy.
    trap 'deployment_status=\$?; restore_runtime_on_error; exit "\$deployment_status"' ERR
    trap 'deployment_status=\$?; if [ "\$deployment_status" != "0" ]; then restore_runtime_on_error; fi' EXIT
    if [ "\$migrations_pending" = "1" ]; then
        verify_current_main_release_on_host
        echo "⏸️ Pausing all runtime writers before DB migrations..."
        pause_runtime_writers_for_migration
        echo "🗄️ Running migrations..."
        # From this point a failed migrate may still have committed earlier
        # append-only migrations. Never restore either binary until the complete
        # migration graph is proven; an intermediate schema is operator-repair-only.
        migration_started=1
        schema_transition_started=1
        compose_run_web python manage.py migrate --noinput
        compose_run_web python manage.py migrate --check --noinput
        schema_transition_completed=1
    else
        # A code-only release never pauses writers, so a marker here was left
        # by an earlier migration deploy. Surface it now instead of when the
        # next migration deploy refuses to start.
        if [ -e "\$writer_pause_sentinel" ]; then
            echo "::warning title=Stale migration writer pause::\$(describe_writer_pause_sentinel) is still present. Host watchdogs stay stood down, and the next migration deploy will stop until an operator reviews and removes it."
        fi
        # Confirm the migration graph still has no pending work before the new
        # binary replaces live services. This cannot apply a migration.
        compose_run_web python manage.py migrate --check --noinput
    fi

    # Read-only migration, vector, startup schema, upload route and coworking
    # concurrency checks share one Django process. Rebuilds and external
    # storage provisioning are explicit maintenance commands.
    echo "✅ Running post-migration deployment checks..."
    compose_run_web python manage.py deploy_postmigrate


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
            echo "⚠️ Use Activate Admin Brain after its readiness checks recover."
            upsert_env_value ORG_MEMORY_QUERY_API_ENABLED "false"
        else
            echo "⚠️ Admin Brain production staging failed; continuing the app deploy with retrieval disabled."
            echo "⚠️ Resolve the readiness blockers, then use Activate Admin Brain."
            upsert_env_value ORG_MEMORY_QUERY_API_ENABLED "false"
        fi
        rm -f "\$stage_stdout"
        unset stage_operator activation_operator approval_hash approval_hash_short
    else
        echo "ℹ️ Skipping Admin Brain production staging and activation; query API remains disabled."
    fi

    echo "🚩 Applying the reviewed Slack owner inventory activation state..."
    upsert_env_value SLACK_OWNER_INVENTORY_ENABLED "\$slack_owner_inventory_enabled"

    echo "🌐 Starting runtime services: \${runtime_services[*]}..."
    if [ "\$migrations_pending" != "1" ]; then
        verify_current_main_release_on_host
        if ! systemctl is-active --quiet nginx || ! nginx -t; then
            echo "❌ Host Nginx is not ready for a safe API handoff." >&2
            false
        fi
        if [ "\$web_proxy_preexisting" = "1" ]; then
            wait_for_origin_web_health 80 "\$previous_app_release" web
        else
            wait_for_origin_web_health 80 "\$previous_app_release"
        fi
        web_candidate_started=1
        docker compose up -d --no-deps --force-recreate web-candidate
        wait_for_origin_web_health 8002 "$APP_RELEASE"
        verify_current_main_release_on_host

        if [ "\$web_proxy_preexisting" = "1" ]; then
            old_nginx_workers=\$(nginx_worker_snapshot)
            web_proxy_switch_attempted=1
            bash "\$web_proxy_script" switch candidate
            wait_for_origin_web_health 80 "$APP_RELEASE" candidate 8 3 1
            web_proxy_candidate_verified=1
            # Old Nginx workers finish requests against web after a reload.
            # Replacing web before they exit would still drop those requests.
            wait_for_nginx_workers_to_drain "\$old_nginx_workers"
        else
            # Validate a shadow Nginx config while Docker still owns port 80.
            # Install the live include only after that port has been freed.
            bash "\$web_proxy_script" validate candidate
            web_proxy_staged=1
            old_direct_web_container_id=\$(docker compose ps -q web)
            [ -n "\$old_direct_web_container_id" ] || false
            web_direct_stopped=1
            docker compose stop web
            web_proxy_switch_attempted=1
            bash "\$web_proxy_script" switch candidate
            wait_for_origin_web_health 80 "$APP_RELEASE" candidate 5 2 1
            web_proxy_candidate_verified=1
        fi
    fi
    if [ "\$migrations_pending" != "1" ]; then
        verify_current_main_release_on_host
    fi
    new_runtime_replacement_started=1
    docker compose up -d --no-deps --force-recreate "\${runtime_services[@]}"
    if [ "\$migrations_pending" != "1" ]; then
        wait_for_origin_web_health 8001 "$APP_RELEASE"
        verify_current_main_release_on_host
        web_candidate_drain_workers=\$(nginx_worker_snapshot)
        bash "\$web_proxy_script" switch web
        wait_for_origin_web_health 80 "$APP_RELEASE" web 8 3 1
    else
        wait_for_origin_web_health 8001 "$APP_RELEASE"
        wait_for_origin_web_health 80 "$APP_RELEASE" web
    fi

    if [ "\$bridge_worker_enabled" != "1" ]; then
        echo "🧹 Stopping disabled community bridge services..."
        docker compose stop bridge-worker bridge-retention || true
        docker compose rm -f bridge-worker bridge-retention || true
    fi

    if [ "\$analytics_sync_enabled" != "1" ]; then
        echo "🧹 Stopping disabled analytics-sync service..."
        docker compose stop analytics-sync || true
        docker compose rm -f analytics-sync || true
    fi
    if [ "\$native_agent_worker_enabled" != "1" ]; then
        echo "🧹 Stopping disabled personal-agent delivery worker..."
        docker compose stop native-agent-worker || true
        docker compose rm -f native-agent-worker || true
    fi

    echo "🔁 Verifying the running web container picked up APP_RELEASE..."
    running_release=\$(docker compose exec -T web sh -lc 'printf "%s" "\$APP_RELEASE"' </dev/null)
    if [ "\$running_release" != "$APP_RELEASE" ]; then
        echo "Expected running web container APP_RELEASE=$APP_RELEASE but found \$running_release"
        # Fail through a command so the ERR trap keeps forward-only schemas
        # paired with no writers until an operator completes recovery.
        false
    fi

    echo "🔁 Verifying the running web container picked up the Slack owner inventory flag..."
    running_inventory_enabled=\$(docker compose exec -T web sh -lc 'printf "%s" "\$SLACK_OWNER_INVENTORY_ENABLED"' </dev/null)
    if [ "\$running_inventory_enabled" != "$SLACK_OWNER_INVENTORY_ENABLED" ]; then
        echo "Expected running web container SLACK_OWNER_INVENTORY_ENABLED=$SLACK_OWNER_INVENTORY_ENABLED but found \$running_inventory_enabled"
        false
    fi

    if [ "$MESSAGE_SYNC_ENABLED" = "true" ]; then
        if [ "$SLACK_OWNER_INVENTORY_ENABLED" = "true" ]; then
            echo "🔁 Verifying the bridge worker picked up the Slack owner inventory flag..."
            running_worker_inventory_enabled=\$(docker compose exec -T bridge-worker sh -lc 'printf "%s" "\$SLACK_OWNER_INVENTORY_ENABLED"' </dev/null)
            if [ "\$running_worker_inventory_enabled" != "true" ]; then
                echo "Expected bridge worker SLACK_OWNER_INVENTORY_ENABLED=true but found \$running_worker_inventory_enabled"
                false
            fi
        fi
        echo "Verifying durable sync progress in the new bridge worker..."
        sync_ready=0
        for attempt in \$(seq 1 18); do
            if docker compose exec -T bridge-worker python manage.py message_sync_status --check --local-worker </dev/null >/dev/null 2>&1; then
                sync_ready=1
                break
            fi
            sleep 5
        done
        if [ "\$sync_ready" != "1" ]; then
            echo "New bridge worker has missing or stale sync lanes."
            false
        fi
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

    if [ "\$bridge_worker_enabled" = "1" ]; then
        echo "🌉 Verifying the current member sync worker..."
        # Compose exec still attaches stdin with -T; preserve the SSH script.
        docker compose exec -T bridge-worker python manage.py message_sync_status --check --local-worker </dev/null
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
    # Release checks have committed the new web slot. Housekeeping cannot
    # safely invoke image rollback after the candidate has been stopped.
    if [ "\$migrations_pending" = "1" ]; then
        restore_host_writer_watchdogs
        rm -f "\$writer_pause_sentinel"
    fi
    trap - ERR EXIT
    if [ "\$web_candidate_started" = "1" ]; then
        if wait_for_nginx_workers_to_drain "\$web_candidate_drain_workers"; then
            if docker compose stop web-candidate; then
                docker compose rm -f web-candidate || echo "⚠️ Candidate removal needs operator cleanup." >&2
            else
                echo "⚠️ Candidate stop needs operator cleanup." >&2
            fi
        else
            echo "⚠️ Candidate kept alive for in-flight requests; the next deployment will wait for operator cleanup." >&2
        fi
    fi
    # Rollback images are no longer needed after this verified release.
    rm -f "\$rollback_manifest"
    for rollback_tag in "\${rollback_tags[@]}"; do
        docker image rm "\$rollback_tag" >/dev/null 2>&1 || true
    done
    echo "$remote_deploy_completed"
} </dev/null
EOF

# A remote shell that stops early still exits 0, and the release check below
# passes as soon as the new web slot serves. Neither proves the release checks,
# candidate cleanup or writer pause cleanup ran, so require the final line too.
if ! grep -qxF "$remote_deploy_completed" "$remote_deploy_log"; then
    echo "❌ The remote deployment script ended before its final step; later release checks and cleanup did not run." >&2
    exit 1
fi

# Independently verify the release outside SSH so an early remote exit can
# never turn a skipped migration/restart into a successful deployment.
curl -fsS https://api.mlai.au/healthz/ready | python3 -c '
import json, sys
payload = json.load(sys.stdin)
if payload.get("status") != "ok" or payload.get("release") != sys.argv[1]:
    raise SystemExit("Production release verification failed")
' "$APP_RELEASE_SHORT"

echo "✅ Deployment complete! Check http://$DROPLET_IP or https://api.mlai.au"
