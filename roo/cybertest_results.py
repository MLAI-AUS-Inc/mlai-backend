"""Account-owned, read-only summaries of existing Security receipts."""
from django.conf import settings
from django.db.models import F
from rest_framework.exceptions import NotFound, ValidationError
from rest_framework.response import Response

from community_chat.my_startup_views import MyStartupAccess

from .cybertest_entry_views import EntryErrors, authorize
from .cybertest_models import CybertestCheckout
from .cybertest_views import PrivateView


def result_summary(row):
    return {
        'audit_id': str(row['audit_id']),
        'repository': row['repository'],
        'phase': row['phase'],
        'status': row['status'],
        'created_at': row['created_at'].isoformat(),
        'settled': row['settled_at'] is not None,
        'sponsored': row['sponsored'] is True,
        'quoted_points': row['points'],
        'charge_microroo': row['charge_microroo'],
        'refund_microroo': row['refund_microroo'],
        'report_saved': row['report_saved'] is True,
        'completed_checks': row['completed_checks'],
        'planned_checks': row['planned_checks'],
    }


class ResultsView(EntryErrors, MyStartupAccess, PrivateView):
    startup_read_bucket = 'read'

    def get(self, request, audit_id=None):
        if any(len(request.query_params.getlist(name)) > 1 for name in ('company_id', 'companyId')):
            raise ValidationError({'companyId': 'Choose a single startup.'})
        identity = authorize(str(request.community_chat_account_session.pk), str(self.company.pk), provision=False)
        receipts = CybertestCheckout.objects.filter(
            user_id=request.user.pk,
            audit_id__isnull=False,
            binding__mlai_user_id=identity['user_id'],
            binding__mlai_company_id=identity['company_id'],
            binding__workspace_id=identity['workspace_id'],
            test_mode=not getattr(settings, 'CYBERTEST_ENTRY_PRODUCTION_ENABLED', False),
        ).values(
            'audit_id', 'status', 'created_at', 'settled_at', 'points',
            repository=F('binding__repository'),
            phase=F('phase_contract__phase'),
            sponsored=F('pricing_snapshot__sponsored'),
            charge_microroo=F('settlement__charge_microroo'),
            refund_microroo=F('settlement__refund_microroo'),
            completed_checks=F('settlement__delivery__completedChecks'),
            planned_checks=F('settlement__delivery__plannedChecks'),
            report_saved=F('delivery_evidence__report__persisted'),
        )
        if audit_id is not None:
            receipt = receipts.filter(audit_id=audit_id).first()
            if receipt is None:
                raise NotFound('Security check not found.')
            return Response({'result': result_summary(receipt)})
        return Response({'results': [result_summary(row) for row in receipts.order_by('-created_at', '-audit_id')[:50]]})
