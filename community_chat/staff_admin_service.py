"""Read-only database projections. Every response uses an explicit field allowlist."""
from datetime import timedelta

from django.contrib.auth import get_user_model
from django.core.paginator import Paginator
from django.db.models import Count, Exists, OuterRef, Q, Subquery
from django.utils import timezone

from integrations.models import ExternalServiceConnection
from mentoring.admin_service import _applications_query
from mentoring.models import MentorProfile, MentorSession, LOCATION_VIDEO
from mentoring.service import MEETING_CLAIM_TIMEOUT_SECONDS, MentoringError
from roo.models import PointsAccount, Ledger, PointsRequest, RewardRedemption
from startup_updates.models import StartupProfile, MonthlyUpdateDraft, MonthlyUpdateReminderDelivery
from .staff_admin_permissions import ROLES, user_role, with_roles


def iso(value):
    return value.isoformat() if value else None


def masked_email(value):
    local, sep, domain = (value or '').partition('@')
    return f'{local[:2]}***@{domain}' if sep else ''


def person(user):
    return {'id': user.pk, 'name': user.full_name or 'Member'} if user else None


def choice(params, key, choices):
    value = params.get(key, '')
    if value and value not in choices:
        raise MentoringError('invalid_filter', f'Unknown {key} filter.', 400)
    return value


def paginate(query, params, serialize):
    try:
        number = int(params.get('page', 1))
        size = int(params.get('page_size', 25))
        if number < 1 or not 1 <= size <= 100:
            raise ValueError
    except (ValueError, TypeError):
        raise MentoringError('invalid_page', 'Use page >= 1 and page_size between 1 and 100.', 400)
    paginator = Paginator(query, size)
    if number > max(1, paginator.num_pages):
        raise MentoringError('invalid_page', 'Page does not exist.', 400)
    page = paginator.page(number)
    return {'results': [serialize(row) for row in page], 'count': paginator.count,
            'page': number, 'page_size': size, 'has_next': page.has_next()}


def member_row(user):
    return {**person(user), 'email': masked_email(user.email), 'joined_at': iso(user.date_joined),
            'is_active': user.is_active, 'admin_role': user_role(user),
            'chat_role': getattr(getattr(user, 'chat_role_appointment', None), 'role', 'member')}


def member_query(params):
    query = get_user_model().objects.all()
    text = params.get('q', '').strip()[:200]
    if text:
        query = query.filter(Q(first_name__icontains=text) | Q(last_name__icontains=text) | Q(email__icontains=text)
                             | Q(first_name__icontains=text.split()[0], last_name__icontains=text.split()[-1]))
    active = choice(params, 'active', ('true', 'false'))
    if active:
        query = query.filter(is_active=active == 'true')
    joined = choice(params, 'joined', ('7d', '30d', 'month'))
    if joined:
        now = timezone.now()
        since = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0) if joined == 'month' else now - timedelta(days=int(joined[:-1]))
        query = query.filter(date_joined__gte=since)
    role = choice(params, 'role', (*ROLES, 'member'))
    if role:
        # Build the same precedence in SQL, so filtering happens before pagination.
        from django.db.models import Case, When, Value, CharField
        from .models import Moderator
        from roo.models import PointsAdmin
        points = PointsAdmin.objects.filter(user_id=OuterRef('pk'), is_active=True)
        query = query.annotate(
            _community=Exists(Moderator.objects.filter(user_id=OuterRef('pk'), is_active=True)),
            _ops=Exists(points.filter(portfolio='ops')),
            _marketing=Exists(points.filter(role='portfolio_lead', portfolio__in=('marketing', 'sales'))),
            _tech=Exists(points.filter(portfolio='tech')),
            _finance=Exists(points.filter(role='committee').filter(Q(portfolio__isnull=True) | Q(portfolio=''))),
        ).annotate(_admin_role=Case(
            When(is_active=False, then=Value('member')),
            When(Q(chat_role_appointment__role='owner') | Q(is_superuser=True), then=Value('superadmin')),
            When(chat_role_appointment__role='admin', then=Value('admin')),
            When(Q(_community=True) | Q(_ops=True), then=Value('community')),
            When(_marketing=True, then=Value('marketing')),
            When(_tech=True, then=Value('tech')),
            When(_finance=True, then=Value('finance')),
            When(is_staff=True, then=Value('tech')),
            default=Value('member'), output_field=CharField(),
        )).filter(_admin_role=role)
    return with_roles(query).order_by('-date_joined', '-pk')


def members(params):
    return {**paginate(member_query(params), params, member_row),
            'data_note': 'Backend accounts only. Active means account enabled, not recent chat presence. Relay-only members and chat activity are unavailable.'}


def member_detail(member_id):
    user = with_roles(get_user_model().objects.all()).filter(pk=member_id).first()
    if user is None:
        raise MentoringError('member_not_found', 'No such backend member.', 404)
    return member_row(user)


def roo(params):
    tab = choice(params, 'tab', ('balances', 'ledger', 'requests', 'redemptions')) or 'balances'
    status = params.get('status', '')
    if status and tab not in ('requests', 'redemptions'):
        raise MentoringError('invalid_filter', 'Status applies to requests or redemptions.', 400)
    if tab == 'balances':
        query = PointsAccount.objects.select_related('user').order_by('-balance_microroo', 'pk')
        serialize = lambda row: {'user': person(row.user), 'balance': row.balance,
                                'balance_microroo': row.balance_microroo, 'microroo_initialized': row.microroo_initialized}
    elif tab == 'ledger':
        query = Ledger.objects.select_related('user').order_by('-created_at', '-pk')
        serialize = lambda row: {'id': row.pk, 'user': person(row.user), 'delta': row.delta if row.delta is not None else row.points_delta,
                                'delta_microroo': row.delta_microroo if row.delta_microroo is not None else row.points_delta_microroo, 'kind': row.kind, 'source': row.source, 'created_at': iso(row.created_at)}
    elif tab == 'requests':
        status = choice(params, 'status', dict(PointsRequest.STATUS_CHOICES))
        query = PointsRequest.objects.order_by('-created_at', '-pk')
        serialize = lambda row: {'id': row.pk, 'points': row.points, 'status': row.status, 'created_at': iso(row.created_at), 'approved_at': iso(row.approved_at)}
    else:
        status = choice(params, 'status', dict(RewardRedemption.STATUS_CHOICES))
        query = RewardRedemption.objects.select_related('user', 'reward').order_by('-requested_at', '-pk')
        serialize = lambda row: {'id': str(row.pk), 'user': person(row.user), 'reward': row.reward.name,
                                'quantity': row.quantity, 'status': row.status, 'requested_at': iso(row.requested_at)}
    if status:
        query = query.filter(status=status)
    return {**paginate(query, params, serialize), 'tab': tab, 'read_only': True}


def month_start():
    return timezone.localdate().replace(day=1)


def startup_query(params):
    month = month_start()
    updates = MonthlyUpdateDraft.objects.filter(organization_id=OuterRef('organization_id'), month=month, status='ready')
    # Reminder deliveries are per founder, not per startup. The UI labels this scope.
    reminders = MonthlyUpdateReminderDelivery.objects.filter(
        user__startup_bindings__organization_id=OuterRef('organization_id'), reminder_date__gte=month,
    ).order_by('-reminder_date', '-updated_at')
    query = StartupProfile.objects.select_related('organization').annotate(
        updated_this_month=Exists(updates),
        reminder_status=Subquery(reminders.values('status')[:1]),
    )
    updated = choice(params, 'updated', ('true', 'false'))
    if updated:
        query = query.filter(updated_this_month=updated == 'true')
    text = params.get('q', '').strip()[:200]
    if text:
        query = query.filter(Q(organization__name__icontains=text) | Q(organization__domain__icontains=text))
    return query.order_by('organization__name', 'pk')


def connector_health():
    return list(ExternalServiceConnection.objects.values('provider', 'status').annotate(count=Count('pk')).order_by('provider', 'status'))


def startups(params):
    return {**paginate(startup_query(params), params, lambda row: {
        'id': row.pk, 'name': row.organization.name, 'domain': row.organization.domain,
        'updated_this_month': row.updated_this_month, 'reminder_status': row.reminder_status or 'none',
    }), 'month': month_start().isoformat(), 'connectors': connector_health(),
        'data_note': 'Updated means a ready update for this reporting month. Reminders are the latest delivery to a bound founder this month. Connector counts cover stored external-service connections, not live provider checks or Gmail sync health.'}


def mentoring_attention():
    now = timezone.now()
    video = Q(location_type=LOCATION_VIDEO, status__in=('accepted', 'completed'))
    problems = video & Q(meeting_status='failed')
    problems |= Q(location_type=LOCATION_VIDEO, status='accepted', meeting_status='pending') & (Q(meeting_claimed_at__isnull=True) | Q(meeting_claimed_at__lte=now - timedelta(seconds=MEETING_CLAIM_TIMEOUT_SECONDS)))
    problems |= Q(notification_status='failed')
    return MentorSession.objects.filter(problems).count()


def overview(sections):
    counts = {}
    attention = []
    def add(section, key, label, count):
        counts[key] = count
        if count:
            attention.append({'section': section, 'label': label, 'count': count})
    if 'members' in sections:
        counts['members'] = get_user_model().objects.count()
    if 'mentoring' in sections:
        counts['active_mentors'] = MentorProfile.objects.filter(is_active=True).count()
        add('mentoring', 'mentoring_problems', 'Mentoring sessions needing attention', mentoring_attention())
        add('mentoring', 'mentor_applications', 'Waiting mentor applications', _applications_query().filter(rejected_at__isnull=True).count())
    if 'roo' in sections:
        add('roo', 'pending_roo_requests', 'Roo requests waiting in Slack', PointsRequest.objects.filter(status='pending').count())
        add('roo', 'pending_redemptions', 'Reward redemptions to fulfil', RewardRedemption.objects.filter(status__in=('requested', 'approved')).count())
    if 'startups' in sections:
        add('startups', 'startups_missing_update', 'Startups without a ready update this month', startup_query({'updated': 'false'}).count())
    badges = {section: sum(item['count'] for item in attention if item['section'] == section) for section in sections}
    return {'counts': counts, 'attention': attention, 'badges': badges, 'as_of': timezone.now().isoformat()}
