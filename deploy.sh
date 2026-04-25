#!/bin/bash
set -euo pipefail


# Configuration
DROPLET_IP="209.38.85.60"
USER="root"
PROJECT_DIR="/root/mlai-backend"
APP_RELEASE="${APP_RELEASE:-$(git rev-parse --short=12 HEAD 2>/dev/null || date +%Y%m%d%H%M)}"

echo "🚀 Deploying release $APP_RELEASE to $DROPLET_IP..."

# 1. Sync files to the server
echo "📦 Syncing files..."
rsync -avz --delete --exclude 'venv' --exclude '.git' --exclude '__pycache__' --exclude '.env' . $USER@$DROPLET_IP:$PROJECT_DIR

# 2. Run setup commands on the server
echo "🔧 Configuring server..."
ssh $USER@$DROPLET_IP << EOF
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
    upsert_env_value ALLOWED_HOSTS "api.mlai.au,209.38.85.60,localhost,127.0.0.1,esafety.localhost"
    upsert_env_value CORS_ALLOWED_ORIGINS "https://mlai.au,https://www.mlai.au"
    upsert_env_value CSRF_TRUSTED_ORIGINS "https://mlai.au,https://www.mlai.au,https://api.mlai.au"
    upsert_env_value DEFAULT_BACKEND_URL "https://api.mlai.au"
    upsert_env_value DEFAULT_FRONTEND_URL "https://mlai.au"
    upsert_env_value MEDHACK_URL "https://mlai.au"
    upsert_env_value ESAFETY_URL "https://mlai.au"
    upsert_env_value INNOVATE_CONNECT_ALLIANCE_URL "https://mlai.au"
    upsert_env_value VIBE_RAISING_URL "https://mlai.au"
    upsert_env_value FOUNDER_TOOLS_URL "https://mlai.au"
    upsert_env_value GOOGLE_OAUTH_REDIRECT_URI "https://api.mlai.au/integrations/callback/google"
    upsert_env_value GITHUB_OAUTH_REDIRECT_URI "https://api.mlai.au/integrations/callback/github"
    upsert_env_value STRIPE_OAUTH_REDIRECT_URI "https://api.mlai.au/integrations/callback/stripe"
    upsert_env_value XERO_OAUTH_REDIRECT_URI "https://api.mlai.au/integrations/callback/xero"
    upsert_env_value NOTION_OAUTH_REDIRECT_URI "https://api.mlai.au/integrations/callback/notion"
    upsert_env_value GOOGLE_DRIVE_OAUTH_REDIRECT_URI "https://api.mlai.au/integrations/callback/google-drive"
    upsert_env_value FT_SLACK_OAUTH_REDIRECT_URI "https://api.mlai.au/integrations/callback/slack"
    upsert_env_value SLACK_OAUTH_REDIRECT_URI "https://api.mlai.au/integrations/callback/slack"
    upsert_env_value APP_RELEASE "$APP_RELEASE"
    require_env_value CONTENT_FACTORY_URL "Set CONTENT_FACTORY_URL to http://<content-factory-private-ip>:8000 for the cross-droplet Content Factory deployment."
    require_env_value VALLEY_HARNESS_URL "Set VALLEY_HARNESS_URL to http://<valley-private-ip>:8080 for the cross-droplet Valley deployment."
    if ! grep -Eq '^(VALLEY_HARNESS_API_KEY|INTERNAL_API_KEY|ROO_API_KEY|MLAI_API_KEY)=.+' .env; then
        echo "WARNING: no Valley service API key is configured; Vibe Raising email draft runs will not reach Valley."
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

    echo "🏗️ Building web, scheduler, and bridge-worker images..."
    docker compose build web scheduler bridge-worker

    echo "🔗 Validating production URL configuration and service connectivity..."
    docker compose run --rm --no-deps web python manage.py validate_prod_urls --check-connectivity --timeout 8

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

    echo "🌐 Starting web, scheduler, and bridge-worker services..."
    docker compose up -d web scheduler bridge-worker

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
