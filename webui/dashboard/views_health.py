"""
Health check views for production monitoring
"""

from django.http import JsonResponse
from django.views.decorators.http import require_GET
from django.db import connection
import redis
import os


@require_GET
def health_check(request):
    """Simple health check endpoint"""
    try:
        # Check database connection
        with connection.cursor() as cursor:
            cursor.execute("SELECT 1")
            db_status = "healthy"
    except Exception:
        db_status = "unhealthy"
    
    # Check Redis connection
    redis_status = "healthy"
    try:
        redis_url = os.environ.get('REDIS_URL', 'redis://localhost:6379/0')
        # Simple Redis connection test
        import django_redis
        cache = django_redis.get_redis_connection("default")
        cache.ping()
    except Exception:
        redis_status = "unhealthy"
    
    overall_status = "healthy" if db_status == "healthy" and redis_status == "healthy" else "unhealthy"
    
    response_data = {
        "status": overall_status,
        "database": db_status,
        "redis": redis_status,
        "version": "2.0.0"
    }
    
    status_code = 200 if overall_status == "healthy" else 503
    return JsonResponse(response_data, status=status_code)
