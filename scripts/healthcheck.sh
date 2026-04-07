#!/bin/bash

# Health check script for Docker containers
# Used by Dockerfile.production for container health monitoring

# Check if Django application is responding
curl -f http://localhost:8000/health/ > /dev/null 2>&1

# Exit with curl's exit code
exit $?
