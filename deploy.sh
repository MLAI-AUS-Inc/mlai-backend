#!/bin/bash
set -e


# Configuration
DROPLET_IP="209.38.85.60"
USER="root"
PROJECT_DIR="/root/mlai-backend"

echo "🚀 Deploying to $DROPLET_IP..."

# 1. Sync files to the server
echo "📦 Syncing files..."
rsync -avz --exclude 'venv' --exclude '.git' --exclude '__pycache__' --exclude '.env' . $USER@$DROPLET_IP:$PROJECT_DIR

# 2. Run setup commands on the server
echo "🔧 Configuring server..."
ssh $USER@$DROPLET_IP << EOF
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
        # Update .env with production values
        sed -i 's/DEBUG=False/DEBUG=False/' .env
        sed -i 's/ALLOWED_HOSTS=.*/ALLOWED_HOSTS=api.mlai.au,209.38.85.60,localhost,127.0.0.1/' .env
        sed -i 's|CORS_ALLOWED_ORIGINS=.*|CORS_ALLOWED_ORIGINS=https://mlai.au,https://www.mlai.au|' .env
        sed -i 's|CSRF_TRUSTED_ORIGINS=.*|CSRF_TRUSTED_ORIGINS=https://api.mlai.au|' .env
    fi

    # Build and start containers
    echo "🐳 Starting containers..."
    docker compose up -d --build
    
    # Run migrations
    echo "🗄️ Running migrations..."
    docker compose exec web python manage.py migrate
EOF

echo "✅ Deployment complete! Check http://$DROPLET_IP or https://api.mlai.au"
