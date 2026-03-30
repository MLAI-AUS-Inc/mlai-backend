#!/bin/bash
set -euo pipefail


# Configuration
DROPLET_IP="209.38.85.60"
USER="root"
PROJECT_DIR="/root/mlai-backend"

echo "🚀 Deploying to $DROPLET_IP..."

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

    # Install Docker if not exists
    if ! command -v docker &> /dev/null; then
        echo "Installing Docker..."
        curl -fsSL https://get.docker.com -o get-docker.sh
        sh get-docker.sh
    fi

    cd $PROJECT_DIR

    # Create .env if it doesn't exist
    if [ ! -f .env ]; then
        echo "Creating .env file..."
        cp .env.example .env
    fi

    # Update .env with production values (Run on every deploy)
    sed -i 's/DEBUG=.*/DEBUG=False/' .env
    sed -i 's/ALLOWED_HOSTS=.*/ALLOWED_HOSTS=api.mlai.au,209.38.85.60,localhost,127.0.0.1,esafety.localhost/' .env
    sed -i 's|CORS_ALLOWED_ORIGINS=.*|CORS_ALLOWED_ORIGINS=https://mlai.au,https://www.mlai.au|' .env
    sed -i 's|CSRF_TRUSTED_ORIGINS=.*|CSRF_TRUSTED_ORIGINS=https://api.mlai.au|' .env
    if ! grep -q '^VALLEY_HARNESS_URL=' .env; then
        echo "VALLEY_HARNESS_URL=http://valley-api:8080" >> .env
    fi

    migration_applied() {
        local app_label="\$1"
        local migration_name="\$2"
        docker compose run --rm --no-deps web python manage.py shell -c "
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

        applied=\$(migration_applied "\$app_label" "\$migration_name")
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
    }

    docker network inspect mlai-shared >/dev/null 2>&1 || docker network create mlai-shared

    echo "🐘 Starting database..."
    docker compose up -d db

    echo "🏗️ Building web and scheduler images..."
    docker compose build web scheduler

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
    docker compose run --rm --no-deps web python manage.py migrate --plan

    echo "🗄️ Running migrations..."
    docker compose run --rm --no-deps web python manage.py migrate --noinput

    echo "✅ Verifying migration readiness..."
    docker compose run --rm --no-deps web python manage.py migrate --check --noinput

    echo "🌐 Starting web and scheduler services..."
    docker compose up -d web scheduler
EOF

echo "✅ Deployment complete! Check http://$DROPLET_IP or https://api.mlai.au"
