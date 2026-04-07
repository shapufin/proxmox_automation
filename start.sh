#!/bin/bash

# VMware-to-Proxmox Migration Tool Startup Script
# This script sets up and starts the production environment

set -e

echo "🚀 Starting VMware-to-Proxmox Migration Tool..."

# Check if .env file exists
if [ ! -f .env ]; then
    echo "⚠️  .env file not found. Creating from template..."
    cp .env.example .env
    echo "📝 Please edit .env file with your configuration before running again."
    echo "   Required changes:"
    echo "   - POSTGRES_PASSWORD: Set a secure PostgreSQL password"
    echo "   - REDIS_PASSWORD: Set a secure Redis password"
    echo "   - DJANGO_SECRET_KEY: Set a secure Django secret key"
    echo ""
    exit 1
fi

# Check if config.yaml exists
if [ ! -f config.yaml ]; then
    echo "⚠️  config.yaml not found. Creating from template..."
    cp config.example.yaml config.yaml
    echo "📝 Please edit config.yaml with your VMware and Proxmox credentials."
    echo ""
fi

# Create necessary directories
echo "📁 Creating necessary directories..."
mkdir -p data configs staging logs

# Build and start services
echo "🔨 Building Docker images..."
docker-compose build

echo "🚀 Starting services..."
docker-compose up -d

# Wait for services to be ready
echo "⏳ Waiting for services to be ready..."
sleep 10

# Check service health
echo "🔍 Checking service health..."
docker-compose ps

# Show logs if there are issues
if docker-compose ps | grep -q "unhealthy\|Exit"; then
    echo ""
    echo "⚠️  Some services may have issues. Showing recent logs:"
    docker-compose logs --tail=20
fi

echo ""
echo "✅ Startup complete!"
echo ""
echo "🌐 Web Interface: http://localhost:8000"
echo "📊 Health Check:  http://localhost:8000/health/"
echo ""
echo "📋 Useful commands:"
echo "   View logs:     docker-compose logs -f"
echo "   Stop services: docker-compose down"
echo "   Restart:       docker-compose restart"
echo ""
