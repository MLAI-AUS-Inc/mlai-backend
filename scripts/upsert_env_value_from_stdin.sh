#!/usr/bin/env bash
set -euo pipefail

key="${1:-}"
case "$key" in
  LINEAR_MEETING_REQUIRED_TEAM_KEYS|LINEAR_CHANNEL_ISSUE_BINDINGS_JSON|LINEAR_CHANNEL_ISSUE_MAX_COMMENTS|LINEAR_CHANNEL_ISSUE_WRITES_ENABLED) ;;
  *)
    echo "Unsupported production environment key" >&2
    exit 64
    ;;
esac

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$repo_root"
umask 077
value="$(cat)"

if [[ -z "$value" || "$value" == *$'\n'* || "$value" == *$'\r'* ]]; then
  echo "Invalid single-line payload for ${key}" >&2
  exit 1
fi

case "$key" in
  LINEAR_MEETING_REQUIRED_TEAM_KEYS)
    [[ "$value" =~ ^[A-Za-z0-9_-]+(,[A-Za-z0-9_-]+)*$ ]] || {
      echo "LINEAR_MEETING_REQUIRED_TEAM_KEYS must be a comma-separated team-key list" >&2
      exit 1
    }
    ;;
  LINEAR_CHANNEL_ISSUE_BINDINGS_JSON)
    LINEAR_API_KEY="validation-placeholder-at-least-32-characters" \
      LINEAR_MEETING_REQUIRED_TEAM_KEYS="TECH,STU,MLA" \
      LINEAR_CHANNEL_ISSUE_BINDINGS_JSON="$value" \
      LINEAR_CHANNEL_ISSUE_MAX_COMMENTS=250 \
      python3 scripts/validate_linear_channel_issue_deploy_config.py
    ;;
  LINEAR_CHANNEL_ISSUE_MAX_COMMENTS)
    [[ "$value" =~ ^[1-9][0-9]*$ ]] || {
      echo "LINEAR_CHANNEL_ISSUE_MAX_COMMENTS must be a positive integer" >&2
      exit 1
    }
    ;;
  LINEAR_CHANNEL_ISSUE_WRITES_ENABLED)
    [[ "$value" == "true" || "$value" == "false" ]] || {
      echo "LINEAR_CHANNEL_ISSUE_WRITES_ENABLED must be true or false" >&2
      exit 1
    }
    ;;
esac

tmp="$(mktemp .env.managed-value.XXXXXX)"
if [[ -f .env ]]; then
  grep -v "^${key}=" .env > "$tmp" || true
fi
printf '%s=%s\n' "$key" "$value" >> "$tmp"
chmod 600 "$tmp"
mv "$tmp" .env
