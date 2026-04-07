@echo off
REM VMware-to-Proxmox Migration Tool Startup Script for Windows

echo 🚀 Starting VMware-to-Proxmox Migration Tool...

REM Check if .env file exists
if not exist .env (
    echo ⚠️  .env file not found. Creating from template...
    copy .env.example .env
    echo 📝 Please edit .env file with your configuration before running again.
    echo    Required changes:
    echo    - POSTGRES_PASSWORD: Set a secure PostgreSQL password
    echo    - REDIS_PASSWORD: Set a secure Redis password
    echo    - DJANGO_SECRET_KEY: Set a secure Django secret key
    echo.
    pause
    exit /b 1
)

REM Check if config.yaml exists
if not exist config.yaml (
    echo ⚠️  config.yaml not found. Creating from template...
    copy config.example.yaml config.yaml
    echo 📝 Please edit config.yaml with your VMware and Proxmox credentials.
    echo.
)

REM Create necessary directories
echo 📁 Creating necessary directories...
if not exist data mkdir data
if not exist configs mkdir configs
if not exist staging mkdir staging
if not exist logs mkdir logs

REM Build and start services
echo 🔨 Building Docker images...
docker compose build

echo 🚀 Starting services...
docker compose up -d

REM Wait for services to be ready
echo ⏳ Waiting for services to be ready...
timeout /t 10 /nobreak >nul

REM Check service health
echo 🔍 Checking service health...
docker compose ps

echo.
echo ✅ Startup complete!
echo.
echo 🌐 Web Interface: http://localhost:8000
echo 📊 Health Check:  http://localhost:8000/health/
echo.
echo 📋 Useful commands:
echo    View logs:     docker compose logs -f
echo    Stop services: docker compose down
echo    Restart:       docker compose restart
echo.
pause
