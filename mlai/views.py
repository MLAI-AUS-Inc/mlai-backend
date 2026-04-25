from django.conf import settings
from django.http import JsonResponse
from django.db import connections
from django.db.migrations.executor import MigrationExecutor
from django.utils import timezone
from datetime import datetime, timedelta


def _health_payload(**values):
    payload = {
        "service": "mlai-backend",
        "app_env": getattr(settings, "APP_ENV", "unknown"),
        "release": getattr(settings, "APP_RELEASE", "unknown"),
    }
    payload.update(values)
    return payload


def _migration_readiness_response():
    try:
        executor = MigrationExecutor(connections["default"])
        targets = executor.loader.graph.leaf_nodes()
        pending_migrations = executor.migration_plan(targets)
    except Exception as exc:
        return JsonResponse(
            _health_payload(
                status="error",
                message="Database migration readiness check failed",
                error=str(exc),
            ),
            status=503,
        )

    if pending_migrations:
        return JsonResponse(
            _health_payload(
                status="not_ready",
                message="Unapplied migrations detected",
                pending_migrations=len(pending_migrations),
            ),
            status=503,
        )

    return None


def health_check(request):
    migration_response = _migration_readiness_response()
    if migration_response is not None:
        return migration_response

    return JsonResponse(
        _health_payload(
            status="ok",
            message="MLAI Backend is running",
        )
    )


def health_live(request):
    return JsonResponse(
        _health_payload(
            status="ok",
        )
    )


def health_ready(request):
    database_config = getattr(settings, "DATABASES", {}).get("default") or {}
    if not database_config:
        return JsonResponse(
            _health_payload(
                status="error",
                message="Database configuration missing",
            ),
            status=503,
        )

    try:
        with connections["default"].cursor() as cursor:
            cursor.execute("SELECT 1")
            cursor.fetchone()
    except Exception as exc:
        return JsonResponse(
            _health_payload(
                status="error",
                message="Database readiness check failed",
                error=str(exc),
            ),
            status=503,
        )

    migration_response = _migration_readiness_response()
    if migration_response is not None:
        return migration_response

    return JsonResponse(
        _health_payload(
            status="ok",
        )
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
            _health_payload(
                status="error",
                subsystem="points",
                message="Points subsystem health check failed",
                error=str(exc),
            ),
            status=503,
        )

    return JsonResponse(
        _health_payload(
            status="ok",
            subsystem="points",
        )
    )
