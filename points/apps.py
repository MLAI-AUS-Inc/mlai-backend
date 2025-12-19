from django.apps import AppConfig


class PointsConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'points'
    verbose_name = 'Points System'

    def ready(self):
        """Bootstrap admin users from environment on startup."""
        from django.conf import settings
        from django.db import connection
        
        # Only run if tables exist (avoid errors during migrations)
        try:
            if 'points_pointsadmin' not in connection.introspection.table_names():
                return
        except Exception:
            return
        
        bootstrap_ids = getattr(settings, 'POINTS_BOOTSTRAP_ADMIN_SLACK_IDS', [])
        if not bootstrap_ids:
            return
        
        from .models import PointsAdmin
        
        for slack_id in bootstrap_ids:
            if slack_id:
                PointsAdmin.objects.get_or_create(
                    slack_user_id=slack_id,
                    defaults={
                        'role': 'admin',
                        'is_active': True,
                    }
                )
