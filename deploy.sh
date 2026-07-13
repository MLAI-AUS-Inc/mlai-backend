#!/bin/bash
set -euo pipefail


# Configuration
DROPLET_IP="209.38.85.60"
USER="root"
PROJECT_DIR="/root/mlai-backend"
APP_RELEASE="${APP_RELEASE:-$(git rev-parse --short=12 HEAD 2>/dev/null || date +%Y%m%d%H%M)}"

if [ -z "${REDIS_URL:-}" ]; then
    echo "❌ REDIS_URL must be supplied by the deployment secret store."
    exit 1
fi
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

echo "🚀 Deploying release $APP_RELEASE to $DROPLET_IP..."

# 1. Sync files to the server
echo "📦 Syncing files..."
rsync -avz --delete --exclude 'venv' --exclude '.git' --exclude '__pycache__' --exclude '.env' . $USER@$DROPLET_IP:$PROJECT_DIR

# Send the credential over SSH stdin rather than a command-line argument. The
# remote shell updates .env using builtins, so the value is neither echoed nor
# exposed in a child-process argv. This happens before any service restart.
echo "🔐 Updating Roo service credential (value redacted)..."
printf '%s' "$ROO_SIM_PATIENT_KEY" | ssh $USER@$DROPLET_IP '
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
printf '%s' "$REDIS_URL" | ssh $USER@$DROPLET_IP '
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

# 2. Run setup commands on the server
echo "🔧 Configuring server..."
ssh $USER@$DROPLET_IP <<EOF
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
    upsert_env_value ALLOWED_HOSTS "api.mlai.au,209.38.85.60,10.126.0.2,localhost,127.0.0.1,esafety.localhost"
    upsert_env_value CORS_ALLOWED_ORIGINS "https://mlai.au,https://www.mlai.au,https://victorai.win,https://www.victorai.win"
    upsert_env_value CSRF_TRUSTED_ORIGINS "https://mlai.au,https://www.mlai.au,https://api.mlai.au"
    upsert_env_value DEFAULT_BACKEND_URL "https://api.mlai.au"
    upsert_env_value DEFAULT_FRONTEND_URL "https://mlai.au"
    upsert_env_value MEDHACK_URL "https://mlai.au"
    upsert_env_value ESAFETY_URL "https://mlai.au"
    upsert_env_value VIBE_RAISING_URL "https://mlai.au"
    upsert_env_value FOUNDER_TOOLS_URL "https://mlai.au"
    upsert_env_value GOOGLE_OAUTH_REDIRECT_URI "https://api.mlai.au/integrations/callback/google"
    upsert_env_value GITHUB_OAUTH_REDIRECT_URI "https://api.mlai.au/integrations/callback/github"
    upsert_env_value STRIPE_OAUTH_REDIRECT_URI "https://api.mlai.au/integrations/callback/stripe"
    upsert_env_value XERO_OAUTH_REDIRECT_URI "https://api.mlai.au/integrations/callback/xero"
    upsert_env_value NOTION_OAUTH_REDIRECT_URI "https://api.mlai.au/integrations/callback/notion"
    upsert_env_value GOOGLE_DRIVE_OAUTH_REDIRECT_URI "https://api.mlai.au/integrations/callback/google-drive"
    upsert_env_value GOOGLE_ANALYTICS_OAUTH_REDIRECT_URI "https://api.mlai.au/integrations/callback/google-analytics"
    upsert_env_value FT_SLACK_OAUTH_REDIRECT_URI "https://api.mlai.au/integrations/callback/slack"
    upsert_env_value SLACK_OAUTH_REDIRECT_URI "https://api.mlai.au/integrations/callback/slack"
    upsert_env_value APP_RELEASE "$APP_RELEASE"
    upsert_env_value HEALTH_HACK_AI_BUDGET_MODE "enforce"
    # Keep the atomic worst-case reservation aligned with Roo's enforced model
    # request limits, including on hosts whose .env predates these defaults.
    upsert_env_value HEALTH_HACK_AI_MAX_PROMPT_TOKENS "12000"
    upsert_env_value HEALTH_HACK_AI_MAX_COMPLETION_TOKENS "2000"
    # Web concurrency: gunicorn sync-worker count (read by scripts/start-web.sh).
    # Sized to droplet RAM (~250MB/worker). 16 fits the 8GB/4vCPU droplet with headroom.
    upsert_env_value GUNICORN_WORKERS "16"
    print_redacted_env_status CONTENT_FACTORY_URL GITHUB_APP_ID GITHUB_APP_PRIVATE_KEY VALLEY_HARNESS_URL REDIS_URL ROO_SERVICE_URL ROO_SIM_PATIENT_KEY HEALTH_HACK_API_KEY ROO_API_KEY
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
    health_hack_key=\$(read_env_value HEALTH_HACK_API_KEY)
    roo_sim_key=\$(read_env_value ROO_SIM_PATIENT_KEY)
    roo_api_key=\$(read_env_value ROO_API_KEY)
    if [ "\${#health_hack_key}" -lt 32 ] || [ "\${#roo_sim_key}" -lt 32 ] || [ "\${#roo_api_key}" -lt 32 ]; then
        echo "❌ HEALTH_HACK_API_KEY, ROO_SIM_PATIENT_KEY, and ROO_API_KEY must each contain at least 32 characters."
        exit 1
    fi
    if [ "\$health_hack_key" = "\$roo_sim_key" ] || [ "\$health_hack_key" = "\$roo_api_key" ] || [ "\$roo_sim_key" = "\$roo_api_key" ]; then
        echo "❌ HEALTH_HACK_API_KEY, ROO_SIM_PATIENT_KEY, and ROO_API_KEY must be distinct credentials."
        exit 1
    fi
    unset health_hack_key roo_sim_key roo_api_key

    runtime_services=(web scheduler)
    if env_has_value SLACK_BRIDGE_BOT_TOKEN && env_has_value DISCORD_BRIDGE_BOT_TOKEN; then
        runtime_services+=(bridge-worker)
        bridge_worker_enabled=1
    else
        bridge_worker_enabled=0
        echo "ℹ️ Skipping bridge-worker startup because bridge tokens are not fully configured."
    fi

    migration_applied() {
        local app_label="\$1"
        local migration_name="\$2"
        compose_run_web python manage.py shell -c "
from django.db.migrations.recorder import MigrationRecorder
from django.db import connections
recorder = MigrationRecorder(connections['default'])
print('yes' if recorder.migration_qs.filter(app='\${app_label}', name='\${migration_name}').exists() else 'no')
" | tail -n 1
    }

    inspect_stale_migration() {
        local app_label="\$1"
        local migration_name="\$2"
        local file_path="\$3"
        local applied

        applied=\$(migration_applied "\$app_label" "\$migration_name" || echo no)
        applied=\$(printf "%s\n" "\$applied" | tail -n 1)
        if [ -f "\$file_path" ] && [ "\$applied" != "yes" ]; then
            echo "🧹 Removing stale unapplied migration \$app_label.\$migration_name (\$file_path)"
            rm -f "\$file_path"
            return
        fi

        if [ ! -f "\$file_path" ] && [ "\$applied" = "yes" ]; then
            echo "❌ Migration \$app_label.\$migration_name is applied in the database but the file is missing on disk."
            echo "   Restore the migration file before redeploying so Django sees a consistent graph."
            exit 1
        fi

        return 0
    }

    docker network inspect mlai-shared >/dev/null 2>&1 || docker network create mlai-shared

    echo "🐘 Starting database..."
    docker compose up -d db

    echo "🏗️ Building runtime images: \${runtime_services[*]}..."
    docker compose build "\${runtime_services[@]}"

    echo "🧱 Validating shared Redis security state..."
    compose_run_web python manage.py validate_health_hack_ai_cache

    echo "🔗 Validating production URL configuration and service connectivity..."
    compose_run_web python manage.py validate_prod_urls --check-connectivity --warn-connectivity --timeout 8

    echo "🔐 Verifying GitHub App server credentials..."
    compose_run_web python manage.py check_github_app_credentials

    echo "🔍 Inspecting for stale generated migrations..."
    inspect_stale_migration \
        core \
        0035_rename_cf_run_workflow_status_idx_content_fac_workflo_10aee3_idx_and_more \
        core/migrations/0035_rename_cf_run_workflow_status_idx_content_fac_workflo_10aee3_idx_and_more.py
    inspect_stale_migration \
        roo \
        0016_rename_roo_pointsr_status_8f1eab_idx_roo_pointsr_status_1880e1_idx_and_more \
        roo/migrations/0016_rename_roo_pointsr_status_8f1eab_idx_roo_pointsr_status_1880e1_idx_and_more.py

    echo "🗺️ Migration plan..."
    compose_run_web python manage.py migrate --plan

    restore_web_on_error() {
        echo "⚠️ Deployment failed after web traffic was paused; restoring existing web service."
        docker compose up -d web || true
    }

    echo "⏸️ Pausing web traffic before DB migrations..."
    docker compose stop web || true
    trap restore_web_on_error ERR

    echo "🗄️ Running migrations..."
    compose_run_web python manage.py migrate --noinput

    echo "✅ Verifying migration readiness..."
    compose_run_web python manage.py migrate --check --noinput

    echo "🧩 Verifying startup update schema..."
    compose_run_web python manage.py validate_startup_update_schema

    echo "🧭 Verifying Vibe Raising video upload routes..."
    compose_run_web python manage.py shell -c "
from django.urls import resolve
resolve('/api/v1/vibe-raising/uploads/video/session/')
resolve('/api/v1/vibe-raising/uploads/video/complete/')
print('vibe raising video upload routes ok')
"

    echo "🎞️ Configuring Firebase Storage CORS for direct video uploads..."
    compose_run_web python manage.py configure_firebase_storage_cors

    echo "🔒 Verifying coworking booking concurrency guard..."
    compose_run_web python manage.py shell -c "
from django.db import connection
with connection.cursor() as cursor:
    cursor.execute(\"SELECT to_regclass('public.unique_active_booking_per_user_date')\")
    index_name = cursor.fetchone()[0]
if not index_name:
    raise SystemExit('unique_active_booking_per_user_date is missing')
print(index_name)
"

    trap - ERR

    echo "🌐 Starting runtime services: \${runtime_services[*]}..."
    docker compose up -d --force-recreate "\${runtime_services[@]}"

    if [ "\$bridge_worker_enabled" != "1" ]; then
        echo "🧹 Stopping disabled bridge-worker service..."
        docker compose stop bridge-worker || true
        docker compose rm -f bridge-worker || true
    fi

    echo "🔁 Verifying the running web container picked up APP_RELEASE..."
    running_release=\$(docker compose exec -T web sh -lc 'printf "%s" "\$APP_RELEASE"' </dev/null)
    if [ "\$running_release" != "$APP_RELEASE" ]; then
        echo "Expected running web container APP_RELEASE=$APP_RELEASE but found \$running_release"
        exit 1
    fi

    echo "🩺 Verifying external health release..."
    release_ok=0
    for attempt in \$(seq 1 12); do
        health_body=\$(curl -fsS https://api.mlai.au/healthz/ready || true)
        if printf '%s\n' "\$health_body" | grep -F "\"release\": \"$APP_RELEASE\"" >/dev/null; then
            release_ok=1
            break
        fi
        sleep 5
    done
    if [ "\$release_ok" != "1" ]; then
        echo "Expected /healthz/ready to report release $APP_RELEASE"
        echo "\$health_body"
        exit 1
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
        exit 1
    fi
    rm -f "\$preflight_headers"
EOF

echo "✅ Deployment complete! Check http://$DROPLET_IP or https://api.mlai.au"
