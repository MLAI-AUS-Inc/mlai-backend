"""Account-owned, read-only summaries of existing Security receipts."""
from django.conf import settings
from rest_framework.exceptions import NotFound, ValidationError
from rest_framework.response import Response

from community_chat.my_startup_views import MyStartupAccess

from .cybertest_checkout import is_sponsored
from .cybertest_entry_views import EntryErrors, authorize
from .cybertest_models import CybertestCheckout
from .cybertest_views import PrivateView


def result_summary(receipt):
    settlement = receipt.settlement or {}
    delivery = settlement.get('delivery') or {}
    evidence = receipt.delivery_evidence or {}
    return {
        'audit_id': str(receipt.audit_id),
        'repository': receipt.binding['repository'],
        'phase': (receipt.phase_contract or {}).get('phase'),
        'status': receipt.status,
        'created_at': receipt.created_at.isoformat(),
        'settled': receipt.settled_at is not None,
        'sponsored': is_sponsored(receipt),
        'quoted_points': receipt.points,
        'charge_microroo': settlement.get('charge_microroo'),
        'refund_microroo': settlement.get('refund_microroo'),
        'report_saved': (evidence.get('report') or {}).get('persisted') is True,
        'completed_checks': delivery.get('completedChecks'),
        'planned_checks': delivery.get('plannedChecks'),
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
        )
        if audit_id is not None:
            receipt = receipts.filter(audit_id=audit_id).first()
            if receipt is None:
                raise NotFound('Security check not found.')
            return Response({'result': result_summary(receipt)})
        return Response({'results': [result_summary(row) for row in receipts.order_by('-created_at', '-audit_id')[:50]]})
