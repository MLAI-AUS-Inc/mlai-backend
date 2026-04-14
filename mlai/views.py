from django.conf import settings
from django.http import JsonResponse
from django.db import connections
from django.db.migrations.executor import MigrationExecutor
from django.utils import timezone
from datetime import datetime, timedelta


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
                "service": "mlai-backend",
                "app_env": getattr(settings, "APP_ENV", "unknown"),
                "release": getattr(settings, "APP_RELEASE", "unknown"),
                "error": str(exc),
            },
            status=503,
        )

    if pending_migrations:
        return JsonResponse(
            {
                "status": "not_ready",
                "message": "Unapplied migrations detected",
                "service": "mlai-backend",
                "app_env": getattr(settings, "APP_ENV", "unknown"),
                "release": getattr(settings, "APP_RELEASE", "unknown"),
                "pending_migrations": len(pending_migrations),
            },
            status=503,
        )

    return JsonResponse(
        {
            "status": "ok",
            "message": "MLAI Backend is running",
            "service": "mlai-backend",
            "app_env": getattr(settings, "APP_ENV", "unknown"),
            "release": getattr(settings, "APP_RELEASE", "unknown"),
        }
    )


def health_live(request):
    return JsonResponse(
        {
            "status": "ok",
            "service": "mlai-backend",
            "app_env": getattr(settings, "APP_ENV", "unknown"),
            "release": getattr(settings, "APP_RELEASE", "unknown"),
        }
    )


def health_ready(request):
    database_config = getattr(settings, "DATABASES", {}).get("default") or {}
    if not database_config:
        return JsonResponse(
            {
                "status": "error",
                "message": "Database configuration missing",
                "service": "mlai-backend",
                "app_env": getattr(settings, "APP_ENV", "unknown"),
                "release": getattr(settings, "APP_RELEASE", "unknown"),
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
                "service": "mlai-backend",
                "app_env": getattr(settings, "APP_ENV", "unknown"),
                "release": getattr(settings, "APP_RELEASE", "unknown"),
                "error": str(exc),
            },
            status=503,
        )

    return JsonResponse(
        {
            "status": "ok",
            "service": "mlai-backend",
            "app_env": getattr(settings, "APP_ENV", "unknown"),
            "release": getattr(settings, "APP_RELEASE", "unknown"),
        }
    )


def health_points(request):
    try:
        from roo.models import CoworkingBooking, Ledger, PointsAdmin

        with connections["default"].cursor() as cursor:
            cursor.execute("SELECT 1")
            cursor.fetchone()

        today = timezone.now().date()
        start_of_week = today - timedelta(days=today.weekday())
        start_of_week_dt = timezone.make_aware(
            datetime.combine(start_of_week, datetime.min.time())
        )

        PointsAdmin.objects.filter(is_active=True).only("slack_user_id").first()
        Ledger.objects.filter(
            created_by_slack_id="__healthcheck__",
            created_at__gte=start_of_week_dt,
        ).only("id").first()
        CoworkingBooking.objects.filter(
            date=today,
            status="booked",
        ).only("id").first()
    except Exception as exc:
        return JsonResponse(
            {
                "status": "error",
                "service": "mlai-backend",
                "subsystem": "points",
                "app_env": getattr(settings, "APP_ENV", "unknown"),
                "release": getattr(settings, "APP_RELEASE", "unknown"),
                "message": "Points subsystem health check failed",
                "error": str(exc),
            },
            status=503,
        )

    return JsonResponse(
        {
            "status": "ok",
            "service": "mlai-backend",
            "subsystem": "points",
            "app_env": getattr(settings, "APP_ENV", "unknown"),
            "release": getattr(settings, "APP_RELEASE", "unknown"),
        }
    )
