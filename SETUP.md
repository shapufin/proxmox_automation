# VMware-to-Proxmox Migration Tool - Production Setup Guide

## 🚀 Quick Start

### Prerequisites
- Docker and Docker Compose installed
- At least 4GB RAM available
- 10GB free disk space

### 1. Initial Setup

```bash
# Clone or extract the project
cd vmware_to_proxmox

# Run the startup script (Linux/Mac)
./start.sh

# Or on Windows
start.bat
```

### 2. Configure Environment

Edit the `.env` file with your settings:
```bash
# Set secure passwords
POSTGRES_PASSWORD=your-secure-postgres-password
REDIS_PASSWORD=your-secure-redis-password
DJANGO_SECRET_KEY=your-very-long-and-secret-django-key-here
```

### 3. Configure Migration Settings

Edit `config.yaml` with your VMware and Proxmox credentials.

### 4. Access the Application

- **Web Interface**: http://localhost:8000
- **Health Check**: http://localhost:8000/health/

## 📋 All Implemented Features

### ✅ Phase 1: Data Model & Hardware Enhancement

- **HardwareSpec JSON Schema** (`vmware_to_proxmox/schemas/hardware_spec.py`)
  - CPU topology (sockets/cores/threads) preservation
  - MAC address preservation with fallback logic
  - PCI/USB/ISO pass-through metadata tracking
  - Comprehensive validation system

- **PostgreSQL Schema** (`vmware_to_proxmox/schemas/postgresql_schema.sql`)
  - Enterprise-grade database with proper indexing
  - Hardware specs, network interfaces, disk controllers
  - Task graph DAG with dependency management
  - Checkpointing system for large transfers
  - Comprehensive audit trail

- **Snapshot Detection & Consolidation** (`vmware_to_proxmox/preflight/snapshot_checker.py`)
  - Automated snapshot detection via pyvmomi
  - Pre-flight consolidation with safety checks
  - Detailed size analysis and recommendations

### ✅ Phase 2: Engine Enhancement

- **Task Graph DAG System** (`vmware_to_proxmox/tasks/dag.py`)
  - Atomic task execution with dependency management
  - Topological sorting for execution order
  - Parallel task execution where dependencies allow
  - Comprehensive error handling and status tracking

- **Checkpointing & Resume/Retry** (`vmware_to_proxmox/tasks/checkpoint.py`)
  - Resumable file transfers with rsync and dd methods
  - Offset-based resume for large files (1TB+ support)
  - Checksum verification for data integrity
  - Bandwidth throttling with adaptive optimization

- **Rollback & Cleanup** (`vmware_to_proxmox/tasks/rollback.py`)
  - Atomic task execution with automatic rollback on failure
  - Resource tracking (VMs, disks, temp directories)
  - Cleanup state management with critical/non-critical steps
  - Dependency-aware cleanup execution

### ✅ Phase 3: Guest Surgery

- **Network Rename System** (`vmware_to_proxmox/remediation/network_rename.py`)
  - udev rule generation for consistent interface naming
  - Distribution-specific network configuration (Debian, RHEL, SUSE)
  - MAC address-based interface mapping
  - Comprehensive remediation script generation

- **Driver Purge Automation** (`vmware_to_proxmox/remediation/driver_purge.py`)
  - VMware driver detection and blacklisting
  - VirtIO driver prioritization
  - VMware tools removal and qemu-guest-agent installation
  - Distribution-agnostic driver management

### ✅ Production Infrastructure

- **Modular Architecture**
  ```
  vmware_to_proxmox/
  ├── schemas/           # Hardware specs and database schemas
  ├── tasks/            # DAG, checkpointing, and rollback systems
  ├── preflight/        # Snapshot detection and checklist system
  ├── remediation/      # Network and driver remediation
  └── audit/           # Comprehensive audit trail system
  ```

- **Pre-flight Checklist System** (`vmware_to_proxmox/preflight/checklist.py`)
  - Comprehensive validation before migration execution
  - Configurable checks with dependency management
  - Blocking vs non-blocking issue classification
  - Detailed reporting with recommendations

- **Enterprise Deployment**
  - PostgreSQL backend with connection pooling
  - Redis for caching and session management
  - Health checks and resource limits
  - Security hardening with non-root users

## 🛠️ Manual Docker Commands

If you prefer manual control:

```bash
# Build images
docker-compose build

# Start services
docker-compose up -d

# View logs
docker-compose logs -f

# Stop services
docker-compose down

# Restart services
docker-compose restart
```

## 🔧 Configuration Files

### Environment Variables (.env)
- Database credentials
- Django settings
- Redis configuration
- Security settings

### Migration Configuration (config.yaml)
- VMware host credentials
- Proxmox host credentials
- Network and storage mappings
- Migration preferences

## 📊 Monitoring

### Health Check Endpoint
```bash
curl http://localhost:8000/health/
```

Response:
```json
{
  "status": "healthy",
  "database": "healthy",
  "redis": "healthy",
  "version": "2.0.0"
}
```

### Logs
- Application logs: `docker-compose logs -f web`
- Worker logs: `docker-compose logs -f worker`
- Database logs: `docker-compose logs -f postgres`

## 🔒 Security Features

- Non-root container execution
- Secure password management
- SSL/TLS support (configurable)
- CSRF protection
- Security headers
- Audit logging

## 🚨 Troubleshooting

### Common Issues

1. **Port 8000 already in use**
   ```bash
   # Change port in .env
   WEB_PORT=8001
   ```

2. **Database connection failed**
   ```bash
   # Check database logs
   docker-compose logs postgres
   
   # Restart database
   docker-compose restart postgres
   ```

3. **Static files not loading**
   ```bash
   # Rebuild static files
   docker-compose restart migrate
   ```

### Reset Everything
```bash
docker-compose down -v
docker system prune -f
./start.sh  # or start.bat
```

## 📈 Performance Tuning

### For Large Migrations
- Increase `WEB_CONCURRENCY` in .env
- Increase `WORKER_CONCURRENCY` in .env
- Add more RAM to Docker
- Use SSD storage for PostgreSQL

### Bandwidth Optimization
- Configure `ssh_bandwidth_limit` in config.yaml
- Use dedicated network for migrations
- Schedule migrations during off-peak hours

## 🎯 Next Steps

1. **Configure your credentials** in `.env` and `config.yaml`
2. **Test with a non-critical VM** first
3. **Monitor the first migration** closely
4. **Scale up** based on your migration volume

## 📞 Support

All features from your requirements have been implemented:
- ✅ Hardware DNA with CPU topology and MAC preservation
- ✅ Snapshot management and consolidation
- ✅ Task graph DAG with atomic execution
- ✅ Checkpointing and resume/retry for large transfers
- ✅ Bandwidth throttling
- ✅ Rollback and cleanup on failure
- ✅ Network rename scripts with udev rules
- ✅ Driver purge automation
- ✅ Modular file structure
- ✅ PostgreSQL schema
- ✅ Pre-flight checklist system
- ✅ Production-ready Docker deployment

The system is now enterprise-ready with all requested features!
