"""Verified MLAI-admin, Django-staff-only startup incident endpoints."""
from django.core import signing
from rest_framework.exceptions import NotAuthenticated, PermissionDenied
from rest_framework.response import Response

from organizations.models import Organization
from workflow_runs.models import ContentFactoryRun
from content_factory import staff_console
from .admin_views import MlaiAdminView
from .admin_access import AdminError


class StaffConsoleView(MlaiAdminView):
    """Retain the admin installation gate and additionally require active staff."""

    def initial(self, request, *args, **kwargs):
        super().initial(request, *args, **kwargs)
        staff_console.require_staff(request.user)

    def handle_exception(self, exc):
        if isinstance(exc, (AdminError, NotAuthenticated, PermissionDenied)):
            return Response({"detail": "Not authorised.", "reason": "not_authorised"}, status=403)
        if isinstance(exc, (Organization.DoesNotExist, ContentFactoryRun.DoesNotExist)):
            return Response({"detail": "Not found.", "reason": "not_found"}, status=404)
        if isinstance(exc, (ValueError, TypeError, signing.BadSignature)):
            return Response({"detail": "Invalid request.", "reason": "invalid_request"}, status=400)
        return super().handle_exception(exc)


class StartupListView(StaffConsoleView):
    """List startup states and overdue incidents in bounded batches."""
    def get(self, request):
        return Response(staff_console.startup_list(request.query_params))


class FounderFunnelView(StaffConsoleView):
    """Reproduce the staff command's signup cohort and proved draft timings."""

    def get(self, request):
        from datetime import timedelta
        from django.core.management import CommandError
        from django.db import connection, transaction
        from django.utils import timezone
        from content_factory.founder_funnel import journey_funnel
        from content_factory.management.commands.founder_funnel_report import founder_funnel, parse_window_bound
        try:
            since = parse_window_bound(request.query_params['since'], end=False) if request.query_params.get('since') else timezone.now()-timedelta(days=30)
            until = parse_window_bound(request.query_params['until'], end=True) if request.query_params.get('until') else timezone.now()
            if until <= since or until-since > timedelta(days=366):
                raise ValueError('Choose a window of up to 366 days.')
            with transaction.atomic():
                if connection.vendor == 'postgresql':
                    with connection.cursor() as cursor:
                        cursor.execute('SET TRANSACTION READ ONLY')
                report = founder_funnel(since=since, until=until)
                report['journey'] = journey_funnel(since=since, until=until)
                transaction.set_rollback(True)
        except (ValueError, CommandError):
            return Response({'detail': 'Choose valid dates within a window of up to 366 days.', 'reason': 'invalid_window'}, status=400)
        return Response(report, headers={'Cache-Control': 'private, no-store'})


class StartupDetailView(StaffConsoleView):
    """Resolve a startup's ten recent runs and website support evidence."""
    def get(self, request, organization_id):
        return Response(staff_console.startup_detail(organization_id))


class StartupActionView(StaffConsoleView):
    """Execute one idempotent, scoped staff recovery action."""
    def post(self, request, organization_id):
        if not isinstance(request.data, dict):
            raise ValueError("Expected an object.")
        status, body, replay = staff_console.perform_action(organization_id, request.data, by_user=request.user)
        response = Response(body, status=status)
        if replay:
            response["Idempotent-Replayed"] = "true"
        return response


class StartupSafetyStateView(StaffConsoleView):
    """Show staff the held safety sentences and bindings they can verify."""
    def get(self, request, organization_id, run_id):
        from content_factory.staff_safety_verification import staff_safety_state
        return Response(staff_safety_state(organization_id, run_id), headers={"Cache-Control": "private, no-store"})


class AlertListView(StaffConsoleView):
    """List open or overdue startup and global alerts."""
    def get(self, request):
        state = request.query_params.get("state", "open")
        if state not in {"open", "overdue"}:
            raise ValueError("Invalid alert state.")
        return Response(staff_console.alert_list(state=state))


class AlertAckView(StaffConsoleView):
    """Acknowledge a receipt through the shared alert store."""
    def post(self, request, alert_id):
        receipt = staff_console.acknowledge_alert(alert_id, by_user=request.user)
        if receipt is None:
            return Response({"detail": "Not found.", "reason": "not_found"}, status=404)
        return Response({"status": "done", "receipt": receipt})
