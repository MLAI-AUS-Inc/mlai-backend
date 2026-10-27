#!/usr/bin/env bash
set -euo pipefail

key="${1:-}"
case "$key" in
  AGENT_RUNTIME_NATIVE_SERVICE_PRIVATE_KEY|\
  AGENT_RUNTIME_NATIVE_REGISTRY_KEY|\
  COMMUNITY_CHAT_ROLE_SERVICE_TOKEN|\
  COMMUNITY_CHAT_EMAIL_CODE_PEPPER|\
  COMMUNITY_CHAT_EMAIL_CODE_DELIVERY_SECRET|\
  COMMUNITY_CHAT_ADAPTER_TOKEN|\
  MLAI_CHAT_ACCOUNT_KEY_SECRET|\
  SLACK_BRIDGE_BOT_TOKEN|\
  SLACK_BRIDGE_SIGNING_SECRET|\
  MESSAGE_SYNC_SLACK_APP_TOKEN|\
  MESSAGE_SYNC_SLACK_USER_APP_TOKEN|\
  MESSAGE_SYNC_SLACK_USER_SIGNING_SECRET|\
  BUZZ_BRIDGE_ADAPTER_TOKEN|\
  BUZZ_BRIDGE_CALLBACK_SECRET|\
  ROO_API_KEY|\
  INTERNAL_API_KEY|\
  LINEAR_API_KEY|\
  CRM_RESEND_WEBHOOK_SECRET|\
  LISTMONK_API_TOKEN) ;;
  *)
    echo "Unsupported production secret key" >&2
    exit 64
    ;;
esac

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$repo_root"
umask 077
secret="$(cat)"
if [[ ${#secret} -lt 32 ]]; then
  echo "Invalid secret payload for ${key}" >&2
  exit 1
fi
if [[ "$key" == "AGENT_RUNTIME_NATIVE_SERVICE_PRIVATE_KEY" || "$key" == "AGENT_RUNTIME_NATIVE_REGISTRY_KEY" || "$key" == "COMMUNITY_CHAT_ROLE_SERVICE_TOKEN" ]]; then
  printf '%s' "$secret" | python3 scripts/validate_native_agent_deploy_config.py --stdin "$key"
fi

if [[ "$key" == "MLAI_CHAT_ACCOUNT_KEY_SECRET" ]]; then
  if [[ "$secret" == *$'\n'* || "$secret" == *$'\r'* ]]; then
    echo "Account key secret must be a single line" >&2
    exit 1
  fi
  existing="$(sed -n 's/^MLAI_CHAT_ACCOUNT_KEY_SECRET=//p' .env 2>/dev/null || true)"
  if [[ -n "$existing" && "$existing" != "$secret" ]]; then
    echo "Account key secret rotation requires explicit reconciliation" >&2
    exit 1
  fi
fi

tmp="$(mktemp .env.chat-secret.XXXXXX)"
if [[ -f .env ]]; then
  grep -v "^${key}=" .env > "$tmp" || true
fi
printf '%s=%s\n' "$key" "$secret" >> "$tmp"
chmod 600 "$tmp"
mv "$tmp" .env
