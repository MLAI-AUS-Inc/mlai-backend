#!/usr/bin/env bash
set -euo pipefail

key="${1:-}"
case "$key" in
  COMMUNITY_CHAT_EMAIL_CODE_PEPPER|\
  COMMUNITY_CHAT_EMAIL_CODE_DELIVERY_SECRET|\
  COMMUNITY_CHAT_ADAPTER_TOKEN|\
  SLACK_BRIDGE_BOT_TOKEN|\
  SLACK_BRIDGE_SIGNING_SECRET|\
  BUZZ_BRIDGE_ADAPTER_TOKEN|\
  BUZZ_BRIDGE_CALLBACK_SECRET|\
  LINEAR_API_KEY|\
  LINEAR_WRITE_API_KEY) ;;
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

tmp="$(mktemp .env.chat-secret.XXXXXX)"
if [[ -f .env ]]; then
  grep -v "^${key}=" .env > "$tmp" || true
fi
printf '%s=%s\n' "$key" "$secret" >> "$tmp"
chmod 600 "$tmp"
mv "$tmp" .env
