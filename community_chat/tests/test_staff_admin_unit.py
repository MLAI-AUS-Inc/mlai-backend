"""No database or network: role boundaries, SQL shapes and redacted projections."""
from datetime import datetime, timezone as dt_timezone
from types import SimpleNamespace as NS
from unittest import TestCase
from unittest.mock import patch, MagicMock

from django.test import override_settings, SimpleTestCase
from rest_framework.test import APIRequestFactory, force_authenticate
from community_chat import staff_admin_permissions as permissions, staff_admin_service as service, staff_admin_views as views
from community_chat import mentoring_views
from mentoring.service import MentoringError
from core.models import User
from roo.models import PointsAccount, Ledger, PointsRequest, RewardRedemption, RewardsCatalog
from startup_updates.models import StartupProfile
from organizations.models import Organization


def user(**kwargs):
    return NS(pk=1, is_authenticated=True, is_active=True, is_staff=False, is_superuser=False, **kwargs)


def appointment(role='committee', portfolio=None, active=True):
    return NS(role=role, portfolio=portfolio, is_active=active)


ENDPOINTS = (
    (views.StaffAdminAccessView, 'overview', None, {}),
    (views.StaffAdminOverviewView, 'overview', 'overview', {}),
    (views.StaffAdminMembersView, 'members', 'members', {}),
    (views.StaffAdminMemberView, 'members', 'member_detail', {'member_id': 1}),
    (views.StaffAdminRooView, 'roo', 'roo', {}),
    (views.StaffAdminStartupsView, 'startups', 'startups', {}),
    (views.StaffAdminRolesView, 'roles', None, {}),
)


class RoleTests(TestCase):
    def test_existing_appointments_resolve_approved_roles(self):
        cases = [('owner', False, [], 'superadmin'), ('admin', False, [], 'admin'),
                 ('member', False, [appointment()], None),
                 ('member', True, [], 'community'), ('member', False, [appointment(portfolio='ops')], 'community'),
                 ('member', False, [appointment('portfolio_lead', 'marketing')], 'marketing'),
                 ('member', False, [appointment('portfolio_lead', 'sales')], 'marketing'),
                 ('member', False, [appointment(portfolio='tech')], 'tech')]
        for chat, mod, points, expected in cases:
            with self.subTest(expected=expected):
                self.assertEqual(permissions.compute_role(user(), chat, mod, points), expected)
        staff = user(); staff.is_staff = True
        self.assertEqual(permissions.compute_role(staff, 'member', False, []), 'tech')
        staff.is_superuser = True
        self.assertEqual(permissions.compute_role(staff, 'member', False, []), 'superadmin')

    def test_fail_closed_and_precedence(self):
        self.assertIsNone(permissions.compute_role(user(), 'member', False, []))
        self.assertIsNone(permissions.compute_role(user(), 'member', False, [appointment(active=False)]))
        self.assertIsNone(permissions.compute_role(user(), 'member', False, [appointment('partner', 'sales')]))
        inactive = user(); inactive.is_active = False
        self.assertIsNone(permissions.compute_role(inactive, 'owner', True, [appointment()]))
        self.assertEqual(permissions.compute_role(user(), 'member', True, [appointment()]), 'community')
        self.assertEqual(permissions.sections_for('finance').get('members'), None)
        self.assertEqual(permissions.sections_for('marketing').get('mentoring'), 'view')
        self.assertEqual(permissions.sections_for('tech').get('mentoring'), 'view')

    def test_partners_and_unknown_appointments_never_grant_portfolio_access(self):
        for portfolio in ('ops', 'tech', 'marketing', 'sales', None):
            for role in ('partner', 'unknown'):
                with self.subTest(role=role, portfolio=portfolio):
                    actual = permissions.compute_role(user(), 'member', False, [appointment(role, portfolio)])
                    self.assertIsNone(actual)
                    self.assertEqual(permissions.sections_for(actual), {})
        for role in permissions.STAFF_APPOINTMENT_ROLES:
            for portfolio, expected in (('ops', 'community'), ('tech', 'tech')):
                self.assertEqual(permissions.compute_role(user(), 'member', False, [appointment(role, portfolio)]), expected)

    def test_member_role_filters_require_the_same_approved_appointment_roles(self):
        for role in ('community', 'tech', 'member'):
            query = service.member_query({'role': role})
            for annotation in ('_ops', '_marketing', '_tech'):
                conditions = query.query.annotations[annotation].query.where.children
                grants = [condition for condition in conditions if getattr(condition.lhs.target, 'name', '') == 'role']
                approved = [condition.rhs for condition in grants if condition.lookup_name == 'in']
                self.assertEqual(approved, [list(permissions.STAFF_APPOINTMENT_ROLES)])

    def test_ambiguous_committee_appointments_never_become_finance(self):
        for portfolio in (None, ''):
            self.assertIsNone(permissions.compute_role(user(), 'member', False, [appointment(portfolio=portfolio)]))
        query = service.member_query({'role': 'finance'})
        expression = query.query.annotations['_admin_role']
        self.assertNotIn('finance', [case.result.value for case in expression.cases])
        self.assertNotIn('_finance', query.query.annotations)

    def test_viewer_chat_role_uses_verified_session(self):
        request = NS(user=user(), community_chat_public_key='synthetic', community_chat_installation_id='installation')
        with patch.object(permissions, 'account_chat_role', return_value='admin') as role, \
             patch.object(permissions.Moderator.objects, 'filter') as mod, \
             patch.object(permissions.PointsAdmin.objects, 'filter', return_value=[]):
            mod.return_value.exists.return_value = False
            self.assertEqual(permissions.viewer_role(request), 'admin')
            role.assert_called_once_with(request.user, 'synthetic', 'installation')


@override_settings(STAFF_ADMIN_PANEL_ENABLED=True, MENTOR_BOOKING_ENABLED=True)
class ViewTests(SimpleTestCase):
    def call(self, cls, method='get', authenticated=True, **kwargs):
        request = getattr(APIRequestFactory(), method)('/x/', {}, format='json')
        if authenticated:
            force_authenticate(request, user=user())
        return cls.as_view(throttle_classes=[])(request, **kwargs)

    def test_every_endpoint_enforces_each_role(self):
        for role in (*permissions.ROLES, None):
            for cls, section, method, kwargs in ENDPOINTS:
                with self.subTest(role=role, endpoint=cls.__name__), \
                     patch.object(views, 'viewer_role', return_value=role), \
                     patch.object(views, 'service') as api:
                    if method:
                        getattr(api, method).return_value = {'results': [], 'count': 0}
                    response = self.call(cls, **kwargs)
                    allowed = section in permissions.sections_for(role)
                    self.assertEqual(response.status_code, 200 if allowed else 403)
                    self.assertEqual(response['Cache-Control'], 'private, no-store')
                    if not allowed:
                        self.assertEqual(api.mock_calls, [])

    def test_access_shape_and_server_matrix(self):
        with patch.object(views, 'viewer_role', return_value='community'):
            response = self.call(views.StaffAdminAccessView)
        self.assertEqual(response.data, {'role': 'community', 'label': 'Community', 'title': 'Community Manager',
                                        'sections': permissions.sections_for('community')})
        with patch.object(views, 'viewer_role', return_value='admin'):
            data = self.call(views.StaffAdminRolesView).data
        self.assertEqual(len(data['roles']), 6)
        self.assertTrue(data['read_only'])

    @override_settings(STAFF_ADMIN_PANEL_ENABLED=False)
    def test_disabled_and_anonymous(self):
        with patch.object(views, 'viewer_role') as role:
            for cls, _, _, kwargs in ENDPOINTS:
                self.assertEqual(self.call(cls, **kwargs).status_code, 404)
                self.assertEqual(self.call(cls, authenticated=False, **kwargs).status_code, 404)
            role.assert_not_called()

    def test_authentication_and_read_only(self):
        for cls, _, _, kwargs in ENDPOINTS:
            self.assertIn(self.call(cls, authenticated=False, **kwargs).status_code, (401, 403))
            with patch.object(views, 'viewer_role', return_value='superadmin'):
                for method in ('post', 'put', 'patch', 'delete'):
                    self.assertEqual(self.call(cls, method=method, **kwargs).status_code, 405)

    def test_endpoint_shapes_and_query_forwarding(self):
        for cls, _, method, kwargs in ENDPOINTS:
            if not method:
                continue
            with patch.object(views, 'viewer_role', return_value='superadmin'), patch.object(service, method, return_value={'synthetic': True}) as api:
                response = self.call(cls, **kwargs)
                self.assertEqual(response.data, {'synthetic': True})
                self.assertEqual(api.call_count, 1)

    def test_all_mentoring_endpoints_apply_read_and_write_levels(self):
        from community_chat.tests.test_mentoring_admin_views_unit import ADMIN_VIEWS
        for role in permissions.ROLES:
            for cls, method, kwargs in ADMIN_VIEWS:
                with self.subTest(role=role, endpoint=cls.__name__, method=method), \
                     patch.object(permissions, 'viewer_role', return_value=role), \
                     patch.object(mentoring_views, 'MentoringAdminService') as api:
                    # Patch handlers to isolate initial() and ensure denied calls never reach them.
                    with patch.object(cls, method, return_value=views.Response({})) as handler:
                        response = self.call(cls, method=method, **kwargs)
                        level = permissions.sections_for(role).get('mentoring')
                        allowed = level == 'edit' or (level == 'view' and method == 'get' and cls is not mentoring_views.MentoringAdminMembersView)
                        self.assertEqual(response.status_code, 200 if allowed else 403)
                        self.assertEqual(handler.call_count, 1 if allowed else 0)


    def test_readonly_mentoring_roles_cannot_search_members(self):
        for role in ('marketing', 'tech'):
            for method in ('get', 'head', 'options'):
                with self.subTest(role=role, method=method), \
                     patch.object(permissions, 'viewer_role', return_value=role), \
                     patch.object(mentoring_views, 'MentoringAdminService') as api:
                    response = self.call(mentoring_views.MentoringAdminMembersView, method=method)
                    self.assertEqual(response.status_code, 403)
                    self.assertEqual(api.mock_calls, [])

    def test_readonly_mentoring_responses_redact_private_data_for_every_resource(self):
        from mentoring.admin_service import serialize_application
        from mentoring.models import MentorProfile
        member = User(pk=1, email='private@example.test', first_name='Ada')
        profile = MentorProfile(pk=1, user=member, busy_calendar_email='calendar-private@example.test', space_notes='private door code')
        profile._prefetched_objects_cache = {'availability_rules': []}
        application = serialize_application(profile, detail=True)
        identity = {'id': 1, 'name': 'Ada', 'email': member.email}
        row = {'id': 1, 'user': identity, 'created_by': identity, 'revoked_by': identity,
               'reviewed_by': identity, 'founder': identity, 'mentor': identity,
               'meeting_error': 'private@example.test private meeting URL', 'unexpected_private': 'private'}
        cases = [
            (mentoring_views.MentoringAdminInvitesView, 'list_invites', [row], {}),
            (mentoring_views.MentoringAdminApplicationsView, 'list_applications', [application], {}),
            (mentoring_views.MentoringAdminApplicationView, 'get_application', application, {'profile_id': 1}),
            (mentoring_views.MentoringAdminMentorsView, 'list_mentors', [row], {}),
            (mentoring_views.MentoringAdminSessionsView, 'list_sessions', [row], {}),
        ]
        for role in ('marketing', 'tech', 'community', 'admin', 'superadmin'):
            for cls, method, data, kwargs in cases:
                with self.subTest(role=role, endpoint=cls.__name__), \
                     patch.object(permissions, 'viewer_role', return_value=role), \
                     patch.object(mentoring_views.MentoringAdminService, method, return_value=data):
                    response = self.call(cls, **kwargs)
                    self.assertEqual(response.status_code, 200)
                    if role in ('marketing', 'tech'):
                        self.assertNotIn('private', str(response.data))
                        self.assertNotIn('busy_calendar_email', str(response.data))
                        self.assertNotIn('space_notes', str(response.data))
                        self.assertNotIn('meeting_error', str(response.data))
                    else:
                        self.assertIn('private', str(response.data))
        self.assertEqual(application['profile']['space_notes'], 'private door code')


class ProjectionTests(TestCase):
    def test_member_allowlist_masks_personal_data(self):
        member = User(pk=1, email='ada@example.test', first_name='Ada', last_name='Lovelace', phone='private', about='private', password='private')
        member._state.fields_cache['chat_role_appointment'] = None
        with patch.object(service, 'user_role', return_value='tech'):
            row = service.member_row(member)
        self.assertEqual(set(row), {'id', 'name', 'email', 'joined_at', 'is_active', 'admin_role', 'chat_role'})
        self.assertEqual(row['email'], 'ad***@example.test')
        self.assertNotIn('private', str(row))

    def test_pagination_boundaries_and_shape(self):
        page = service.paginate(list(range(30)), {'page': '2', 'page_size': '25'}, lambda v: {'id': v})
        self.assertEqual((page['count'], page['page'], len(page['results']), page['has_next']), (30, 2, 5, False))
        for params in ({'page': '0'}, {'page': '-1'}, {'page_size': '101'}, {'page_size': '0'}, {'page': 'x'}, {'page': '9'}):
            with self.assertRaises(MentoringError):
                service.paginate([], params, str)

    def test_filters_compile_against_real_model_fields(self):
        from django.db.backends.sqlite3.base import DatabaseWrapper
        connection = DatabaseWrapper({'ENGINE': 'django.db.backends.sqlite3', 'NAME': ':memory:', 'TIME_ZONE': None})
        def sql(query):
            return query.query.get_compiler(connection=connection).as_sql()[0]
        for role in (*permissions.ROLES, 'member'):
            query = service.member_query({'role': role, 'active': 'true', 'joined': 'month', 'q': 'Ada Love'})
            self.assertIn('CASE WHEN', sql(query))
            self.assertTrue(query._prefetch_related_lookups)
        query = service.startup_query({'updated': 'false', 'q': 'synthetic'})
        self.assertIn('EXISTS', sql(query))
        self.assertIn('monthlyupdatereminderdelivery', sql(query))
        self.assertEqual(query.query.select_related, {'organization': {}})
        for params in ({'role': 'owner'}, {'active': 'yes'}, {'joined': '200d'}):
            with self.assertRaises(MentoringError):
                service.member_query(params)
        with self.assertRaises(MentoringError):
            service.startup_query({'updated': 'yes'})

    def test_roo_each_tab_redacts_sensitive_fields(self):
        now = datetime(2026, 10, 22, tzinfo=dt_timezone.utc)
        member = User(pk=1, first_name='Ada', email='private@example.test')
        cases = [
            ('balances', PointsAccount, PointsAccount(user=member, balance=10), {'user', 'balance', 'balance_microroo', 'microroo_initialized'}),
            ('ledger', Ledger, Ledger(pk=1, user=member, created_at=now, points_delta=8, description='private', idempotency_key='private'), {'id', 'user', 'delta', 'delta_microroo', 'kind', 'source', 'created_at'}),
            ('requests', PointsRequest, PointsRequest(pk=1, reason='private', slack_channel_id='private', created_at=now), {'id', 'points', 'status', 'created_at', 'approved_at'}),
            ('redemptions', RewardRedemption, RewardRedemption(user=member, reward=RewardsCatalog(name='Shirt'), notes='private', requested_at=now), {'id', 'user', 'reward', 'quantity', 'status', 'requested_at'}),
        ]
        for tab, model, obj, keys in cases:
            with self.subTest(tab=tab), patch.object(model.objects, 'select_related') as related, patch.object(model.objects, 'order_by') as ordered:
                related.return_value.order_by.return_value = [obj]
                ordered.return_value = [obj]
                data = service.roo({'tab': tab})
                self.assertEqual(set(data['results'][0]), keys)
                self.assertNotIn('private', str(data))
                self.assertNotIn('stripe', str(data).lower())
                self.assertTrue(data['read_only'])
                if tab == 'ledger':
                    self.assertEqual(data['results'][0]['delta'], 8)
        for params in ({'tab': 'purchases'}, {'tab': 'requests', 'status': 'fulfilled'}, {'tab': 'ledger', 'status': 'approved'}):
            with self.assertRaises(MentoringError):
                service.roo(params)

    def test_roo_status_is_filtered_in_query(self):
        for tab, model in [('requests', PointsRequest), ('redemptions', RewardRedemption)]:
            with patch.object(model.objects, 'order_by') as ordered, patch.object(model.objects, 'select_related') as related:
                query = ordered.return_value if tab == 'requests' else related.return_value.order_by.return_value
                query.filter.return_value = []
                data = service.roo({'tab': tab, 'status': 'approved'})
                query.filter.assert_called_once_with(status='approved')
                self.assertEqual(data['results'], [])

    def test_startup_shape_contains_counts_and_statuses_only(self):
        obj = StartupProfile(pk=1, organization=Organization(pk=1, name='Synthetic', domain='example.test'), notes='private')
        obj.updated_this_month = True; obj.reminder_status = 'sent'
        with patch.object(service, 'startup_query', return_value=[obj]), patch.object(service, 'connector_health', return_value=[{'provider': 'slack', 'status': 'connected', 'count': 2}]):
            data = service.startups({})
        self.assertEqual(set(data['results'][0]), {'id', 'name', 'domain', 'updated_this_month', 'reminder_status'})
        self.assertNotIn('private', str(data))
        self.assertEqual(data['connectors'][0]['count'], 2)

    def test_overview_does_not_read_hidden_sections(self):
        with patch.object(service, 'get_user_model') as users, patch.object(service, 'MentorProfile') as mentors, \
             patch.object(service, 'PointsRequest') as points, patch.object(service, 'RewardRedemption') as rewards, \
             patch.object(service, 'startup_query') as startups, patch.object(service, 'mentoring_attention') as problems:
            points.objects.filter.return_value.count.return_value = 2
            rewards.objects.filter.return_value.count.return_value = 3
            data = service.overview(permissions.sections_for('finance'))
            self.assertEqual(data['badges']['roo'], 5)
            self.assertEqual(set(data['counts']), {'pending_roo_requests', 'pending_redemptions'})
            users.assert_not_called(); mentors.objects.filter.assert_not_called()
            startups.assert_not_called(); problems.assert_not_called()

    def test_missing_member_is_404(self):
        with patch.object(service, 'with_roles') as query:
            query.return_value.filter.return_value.first.return_value = None
            with self.assertRaises(MentoringError) as exc:
                service.member_detail(404)
            self.assertEqual(exc.exception.status_code, 404)

class FlagContractTests(TestCase):
    def test_home_exposes_flag_without_changing_mentoring_flag(self):
        from community_chat import home_views
        with patch.object(home_views, 'PointsService') as points, \
             patch.object(home_views, 'Task'), patch.object(home_views, 'RewardsCatalog'), \
             patch.object(home_views, 'coworking_booking_available', return_value=False), \
             patch.object(home_views, 'public_roo_target', return_value=None):
            points.get_balance.return_value = {'earned_balance': 0, 'purchased_topup_balance': 0,
                                               'lifetime_earned': 0, 'lifetime_spent': 0}
            points.microroo_to_legacy_whole.return_value = 0
            for enabled in (False, True):
                with override_settings(STAFF_ADMIN_PANEL_ENABLED=enabled, MENTOR_BOOKING_ENABLED=False):
                    response = home_views.CommunityHomeView().get(NS(user=user()))
                    self.assertEqual(response.data['feature_flags']['staff_admin_panel'], enabled)
                    self.assertFalse(response.data['feature_flags']['mentor_booking'])
