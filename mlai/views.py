from django.conf import settings
from django.http import JsonResponse
from django.db import connections
from django.db.migrations.executor import MigrationExecutor


def health_check(request):
    try:
        executor = MigrationExecutor(connections["default"])
        targets = executor.loader.graph.leaf_nodes()
        pending_migrations = executor.migration_plan(targets)
    except Exception as exc:
        return JsonResponse(
            {
                "status": "error",
                "message": "Database migration readiness check failed",
                "error": str(exc),
            },
            status=503,
        )

    if pending_migrations:
        return JsonResponse(
            {
                "status": "not_ready",
                "message": "Unapplied migrations detected",
                "pending_migrations": len(pending_migrations),
            },
            status=503,
        )

    return JsonResponse({"status": "ok", "message": "MLAI Backend is running"})


def health_live(request):
    return JsonResponse(
        {
            "status": "ok",
            "service": "mlai-backend",
            "app_env": getattr(settings, "APP_ENV", "unknown"),
        }
    )


def health_ready(request):
    database_config = getattr(settings, "DATABASES", {}).get("default") or {}
    if not database_config:
        return JsonResponse(
            {
                "status": "error",
                "message": "Database configuration missing",
            },
            status=503,
        )

    try:
        with connections["default"].cursor() as cursor:
            cursor.execute("SELECT 1")
            cursor.fetchone()
    except Exception as exc:
        return JsonResponse(
            {
                "status": "error",
                "message": "Database readiness check failed",
                "error": str(exc),
            },
            status=503,
        )

    return JsonResponse(
        {
            "status": "ok",
            "service": "mlai-backend",
            "app_env": getattr(settings, "APP_ENV", "unknown"),
        }
    )
