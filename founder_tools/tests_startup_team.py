"""Persisted membership boundaries, privacy, owner consent and concurrency."""
from django.db import connections
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
import json
from unittest import skipUnless
from unittest.mock import patch
from uuid import uuid4

from django.core.cache import cache
from django.db import close_old_connections, connection
from django.test import TransactionTestCase, SimpleTestCase, override_settings
from django.utils import timezone
from rest_framework.test import APIRequestFactory, force_authenticate

from core.models import User
from content_factory.models import OrganizationContentConfig
from content_factory.self_heal_testing import ALL_FLAGS_ON, make_p2_org, make_run
from . import startup_team as teams
from .models import VibeRaisingCompany
from .services import get_founder_company_context, organization_owner_user_id
from .startup_team_domains import registrable_domain, denied_domain


@override_settings(**{**ALL_FLAGS_ON, 'MY_STARTUP_TEAM_DOMAIN_AUTO_JOIN_ENABLED': False,
                     'MY_STARTUP_TRANSITION_NOTIFICATIONS_ENABLED': False,
                     'COMMUNITY_CHAT_FRONTEND_URL': 'https://chat.synthetic.test'})
class TeamTests(TransactionTestCase):
    def setUp(self):
        cache.clear()
        self.fixture = make_p2_org()
        self.org, self.owner = self.fixture.organization, self.fixture.owner
        self.owner.email_verified_at = timezone.now()
        self.owner.save(update_fields=['email_verified_at'])
        self.member = self.user()
        self.factory = APIRequestFactory()
        self.notice = patch.object(teams, '_notify')
        self.notify = self.notice.start()
        self.addCleanup(self.notice.stop)

    def user(self, *, verified=True):
        user = User.objects.create_user(email=f'{uuid4().hex}@member.synthetic.test', first_name='Member')
        if verified:
            user.email_verified_at = timezone.now()
            user.save(update_fields=['email_verified_at'])
        return user

    def roster(self):
        return OrganizationContentConfig.objects.get(organization=self.org).pillar_strategy['startup_team']

    def request(self, user=None):
        user = user or self.member
        reference = teams.issue_join_reference(user=user, organization=self.org)
        result = teams.request_join(user=user, join_reference=reference)
        return result, next(row['request_id'] for row in self.roster()['requests'] if row['user_id'] == user.pk and row['state'] == 'pending')

    def approve(self, user=None):
        user = user or self.member
        _, identifier = self.request(user)
        teams.decide_request(organization=self.org, user=self.owner, request_id=identifier, decision='approve')
        return next(row for row in self.roster()['members'] if row['user_id'] == user.pk)

    def call(self, view, *, user=None, method='get', data=None, company=True, request_format='json', **kwargs):
        path = f'/?company_id={self.fixture.company.pk}' if company else '/'
        request = getattr(self.factory, method)(path, data=data or {}, format=request_format)
        force_authenticate(request, user=user or self.owner)
        return view.as_view()(request, **kwargs)

    def test_T1_reference_is_opaque_user_bound_and_response_has_status_only(self):
        nonce = teams.issue_join_reference(user=self.member, organization=self.org)
        self.assertGreaterEqual(len(nonce), 22)
        self.assertNotIn(self.owner.email, nonce)
        self.assertNotIn(self.org.domain, nonce)
        with self.assertRaises(teams.TeamError):
            teams.request_join(user=self.user(), join_reference=nonce)
        first = teams.request_join(user=self.member, join_reference=nonce)
        second = teams.request_join(user=self.member, join_reference=nonce)
        self.assertEqual(first, {'status': 'requested'})
        self.assertEqual(second, {'status': 'already_requested'})
        self.assertEqual(set(first), {'status'})
        self.assertEqual(teams.pending_request_count(self.org), 1)

    def test_T2_member_foreign_owner_and_staff_cannot_approve(self):
        _, identifier = self.request()
        outsiders = [self.member, make_p2_org().owner, User.objects.create_superuser(email=f'{uuid4().hex}@staff.synthetic.test')]
        for user in outsiders:
            with self.subTest(user=user.pk), self.assertRaises(teams.TeamError) as error:
                teams.decide_request(organization=self.org, user=user, request_id=identifier, decision='approve')
            self.assertEqual(error.exception.reason, 'owner_only')
        self.assertEqual(self.roster()['members'], [])

    def test_T3_unverified_and_closed_requests_cannot_join(self):
        unverified = self.user(verified=False)
        nonce = teams.issue_join_reference(user=unverified, organization=self.org)
        with self.assertRaises(teams.TeamError) as error:
            teams.request_join(user=unverified, join_reference=nonce)
        self.assertEqual(error.exception.reason, 'email_unverified')
        _, identifier = self.request()
        later = timezone.now() + timedelta(days=8)
        with self.assertRaises(teams.TeamError) as error:
            teams.decide_request(organization=self.org, user=self.owner, request_id=identifier, decision='approve', now=later)
        self.assertEqual(error.exception.reason, 'request_closed')
        self.assertEqual(self.roster()['members'], [])

    def test_T3_declined_withdrawn_and_approved_requests_are_closed(self):
        for state in ['declined', 'withdrawn', 'approved']:
            user = self.user()
            _, identifier = self.request(user)
            config = OrganizationContentConfig.objects.get(organization=self.org)
            roster = config.pillar_strategy['startup_team']
            next(row for row in roster['requests'] if row['request_id'] == identifier)['state'] = state
            config.save(update_fields=['pillar_strategy'])
            with self.assertRaises(teams.TeamError) as error:
                teams.decide_request(organization=self.org, user=self.owner, request_id=identifier, decision='approve')
            self.assertEqual(error.exception.reason, 'request_closed')

    def test_T5_approval_does_not_create_a_member_company_or_default_rights(self):
        before = VibeRaisingCompany.objects.count()
        row = self.approve()
        self.assertEqual(VibeRaisingCompany.objects.count(), before)
        self.assertEqual(row['rights'], {'publish': False, 'billing': False})
        self.assertFalse(teams.member_may_publish(self.member, self.org))
        self.assertIsNone(teams.member_context(self.member, self.fixture.company.pk, need='publish'))
        context = teams.member_context(self.member, self.fixture.company.pk, need='write_articles')
        self.assertEqual(context.role, 'member')
        self.assertEqual(context.profile.user_id, self.member.pk)

    def test_T5_only_owner_can_grant_rights_and_unconfirmed_cannot_gain_them(self):
        row = self.approve()
        with self.assertRaises(teams.TeamError):
            teams.update_member(organization=self.org, user=self.member, member_id=row['memberId'], rights={'publish': True})
        teams.update_member(organization=self.org, user=self.owner, member_id=row['memberId'], rights={'publish': True})
        self.assertTrue(teams.member_may_publish(self.member, self.org))
        self.assertIsNotNone(teams.member_context(self.member, self.fixture.company.pk, need='publish'))
        with self.assertRaises(teams.TeamError):
            teams.update_member(organization=self.org, user=self.owner, member_id=row['memberId'], rights={'owner': True})

    def test_T5_billing_requires_explicit_current_member_grant(self):
        from content_factory.company_billing import resolve_content_billing_user
        self.org.billing_user = self.owner
        self.org.save(update_fields=['billing_user'])
        row = self.approve()
        self.assertEqual(resolve_content_billing_user(self.member, self.org.domain).pk, self.member.pk)
        teams.update_member(organization=self.org, user=self.owner, member_id=row['memberId'], rights={'billing': True})
        self.assertEqual(resolve_content_billing_user(self.member, self.org.domain).pk, self.owner.pk)
        teams.update_member(organization=self.org, user=self.owner, member_id=row['memberId'], remove=True)
        self.assertEqual(resolve_content_billing_user(self.member, self.org.domain).pk, self.member.pk)

    def test_missing_team_package_never_promotes_an_owner_over_a_retained_pin(self):
        from content_factory.self_heal_testing import missing_provider
        from .services import organization_owner_user_id
        self.approve()
        with missing_provider('founder_tools.startup_team'):
            self.assertIsNone(organization_owner_user_id(self.org))

    def test_billing_summary_matches_live_spend_rights_without_other_account_email(self):
        from content_factory.company_billing import billing_summary
        self.org.billing_user = self.owner
        self.org.save(update_fields=['billing_user'])
        row = self.approve()
        def balance(user):
            return {'digital_service_balance_microroo': (20 if user.pk == self.owner.pk else 4) * 1_000_000}
        with patch('roo.services.PointsService.get_balance', side_effect=balance), \
                patch('content_factory.billing.get_content_factory_article_cost_points', return_value=12):
            own = billing_summary(self.org, self.member)
            self.assertEqual((own['balance'], own['companyBillingEnabled'], own['billingFounderIsYou']), (4, False, True))
            self.assertFalse(own['otherFounderHasPoints'])
            teams.update_member(organization=self.org, user=self.owner, member_id=row['memberId'], rights={'billing': True})
            granted = billing_summary(self.org, self.member)
            self.assertEqual((granted['balance'], granted['companyBillingEnabled'], granted['billingFounderIsYou']), (20, True, False))
            self.assertEqual(granted['billingAccount'], 'Company points')
            self.assertNotIn(self.owner.email, json.dumps(granted))
            teams.update_member(organization=self.org, user=self.owner, member_id=row['memberId'], rights={'billing': False})
            self.assertEqual(billing_summary(self.org, self.member)['balance'], 4)

    @override_settings(COMMUNITY_CHAT_STARTUP_UPDATES_ENABLED=True)
    def test_account_bootstrap_recognizes_member_workspace_without_profile_tenant_grant(self):
        from community_chat.startups.views import BootstrapView
        from .models import VibeRaisingProfile
        row = self.approve()
        response = self.call(BootstrapView, user=self.member, company=False)
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.data['capabilities']['hasStartupWorkspace'])
        self.assertEqual(response.data['profile']['companies'], [])
        self.assertFalse(VibeRaisingCompany.objects.filter(profile__user=self.member).exists())
        self.assertIsNone(VibeRaisingProfile.objects.get(user=self.member).active_company_id)
        self.assertNotIn(self.owner.email, json.dumps(response.data))
        teams.update_member(organization=self.org, user=self.owner, member_id=row['memberId'], remove=True)
        self.assertFalse(self.call(BootstrapView, user=self.member, company=False).data['capabilities']['hasStartupWorkspace'])
        with override_settings(MY_STARTUP_TEAM_ENABLED=False), patch.object(teams, 'member_companies') as membership:
            self.assertNotIn('hasStartupWorkspace', self.call(BootstrapView, user=self.member, company=False).data['capabilities'])
            membership.assert_not_called()

    def test_T8_invite_is_hashed_fragment_revocable_and_five_uses(self):
        invitation = teams.invite_link(organization=self.org, user=self.owner, action='create')
        token = invitation['url'].split('#')[1]
        self.assertEqual(len(token), 43)
        self.assertNotIn(token, json.dumps(self.roster()))
        for _ in range(5):
            self.assertEqual(teams.join_invite(user=self.user(), token=token), {'status': 'joined'})
        with self.assertRaises(teams.TeamError):
            teams.join_invite(user=self.user(), token=token)
        renewed = teams.invite_link(organization=self.org, user=self.owner, action='create')['url'].split('#')[1]
        teams.invite_link(organization=self.org, user=self.owner, action='revoke')
        with self.assertRaises(teams.TeamError):
            teams.join_invite(user=self.member, token=renewed)

    def test_T8_expired_invite_and_unverified_join_refused(self):
        token = teams.invite_link(organization=self.org, user=self.owner, action='create')['url'].split('#')[1]
        with self.assertRaises(teams.TeamError):
            teams.join_invite(user=self.member, token=token, now=timezone.now() + timedelta(days=8))
        with self.assertRaises(teams.TeamError) as error:
            teams.join_invite(user=self.user(verified=False), token=token)
        self.assertEqual(error.exception.reason, 'email_unverified')

    def test_T9_removed_member_loses_context_and_run_access_immediately(self):
        row = self.approve()
        run = make_run(self.org, workflow='direct_generate')
        context = teams.member_context(self.member, self.fixture.company.pk, need='read')
        token = teams.bind_member_context(context)
        try:
            self.assertTrue(teams.member_may_use_run(self.member, run))
            teams.update_member(organization=self.org, user=self.owner, member_id=row['memberId'], remove=True)
            self.assertFalse(teams.member_may_use_run(self.member, run))
            self.assertIsNone(teams.bound_member_context(self.member, self.fixture.company.pk))
        finally:
            teams.reset_member_context(token)

    def test_T10_members_receive_first_names_only_and_owner_only_asks_stay_private(self):
        self.approve()
        other = self.user()
        self.approve(other)
        payload = teams.team_payload(organization=self.org, user=self.member)
        self.assertFalse(payload['canManage'])
        self.assertEqual(payload['requests'], [])
        self.assertNotIn(other.email, json.dumps(payload))
        self.assertNotIn(self.owner.email, json.dumps(payload))
        for ask in ['approve_setup', 'authorize_github', 'approve_join_request', 'agree_terms']:
            self.assertEqual([u.pk for u in teams.notification_recipients(self.org, item='article:x', ask=ask)], [self.owner.pk])
        self.assertEqual({u.pk for u in teams.notification_recipients(self.org, item='article:x')}, {self.owner.pk, self.member.pk, other.pk})

    def test_T10_proxy_ask_is_owner_only_even_under_article_item_name(self):
        row = self.approve()
        teams.update_member(organization=self.org, user=self.owner, member_id=row['memberId'], rights={'publish': True})
        run = make_run(self.org, workflow='direct_generate', result={'publish_pr_v1': {'kind': 'proxy'}})
        self.assertEqual({u.pk for u in teams.notification_recipients(self.org, item=f'article:{run.run_id}', ask='approve_on_github')}, {self.owner.pk})
        run.result = {'publish_pr_v1': {'kind': 'article'}}
        run.save(update_fields=['result'])
        self.assertEqual({u.pk for u in teams.notification_recipients(self.org, item=f'article:{run.run_id}', ask='approve_on_github')}, {self.owner.pk, self.member.pk})

    def test_T11_owner_departure_freezes_without_promoting_another_company(self):
        row = self.approve()
        teams.update_member(organization=self.org, user=self.owner, member_id=row['memberId'], rights={'publish': True, 'billing': True})
        from .services import offboard_company
        from .services import get_or_create_founder_profile
        other_owner = self.user()
        VibeRaisingCompany.objects.create(profile=get_or_create_founder_profile(other_owner), organization=self.org,
            name='Historical second company', domain=self.org.domain)
        company_id = self.fixture.company.pk
        with patch('startup_updates.data_deletion.disconnect_gmail_for_user', return_value={}), \
                patch('content_factory.website_connections.offboard_website_connections'), \
                patch('founder_tools.services._purge_org_marketing_data') as purge:
            offboard_company(self.fixture.company)
        purge.assert_not_called()
        self.assertIsNone(organization_owner_user_id(self.org))
        self.assertIsNone(teams.viewer_role(other_owner, self.org))
        self.assertTrue(self.roster()['frozen'])
        self.assertFalse(teams.member_may_publish(self.member, self.org))
        self.assertIsNotNone(teams.member_context(self.member, company_id, need='read'))
        self.assertIsNone(teams.member_context(self.member, company_id, need='write_articles'))

    @skipUnless(connection.vendor == 'postgresql', 'real row locks require PostgreSQL')
    def test_T12_simultaneous_approval_adds_exactly_one_member(self):
        _, identifier = self.request()
        def approve():
            close_old_connections()
            try:
                teams.decide_request(organization=self.org, user=self.owner, request_id=identifier, decision='approve')
                return 'approved'
            except teams.TeamError as error:
                return error.reason
            finally:
                connections.close_all()
        with ThreadPoolExecutor(max_workers=2) as pool:
            result = list(pool.map(lambda _: approve(), range(2)))
        self.assertCountEqual(result, ['approved', 'request_closed'])
        self.assertEqual(len(self.roster()['members']), 1)

    def test_T13_flags_off_and_no_roster_preserve_owner_behavior(self):
        self.assertEqual(organization_owner_user_id(self.org), self.owner.pk)
        self.assertEqual(teams.viewer_role(self.member, self.org), None)
        with override_settings(MY_STARTUP_TEAM_ENABLED=False):
            self.assertIsNone(teams.issue_join_reference(user=self.member, organization=self.org))
            self.assertIsNone(teams.member_context(self.member, self.fixture.company.pk, need='read'))
        self.assertNotIn('startup_team', OrganizationContentConfig.objects.get(organization=self.org).pillar_strategy)

    def test_T14_bound_context_matches_exact_user_company_and_resets(self):
        self.approve()
        context = teams.member_context(self.member, self.fixture.company.pk, need='read')
        token = teams.bind_member_context(context)
        try:
            self.assertEqual(get_founder_company_context(self.member, self.fixture.company.pk).role, 'member')
            self.assertIsNone(teams.bound_member_context(self.user(), self.fixture.company.pk))
            self.assertIsNone(teams.bound_member_context(self.member, uuid4()))
        finally:
            teams.reset_member_context(token)
        with self.assertRaises(VibeRaisingCompany.DoesNotExist):
            get_founder_company_context(self.member, self.fixture.company.pk)

    def test_expiry_and_72_hour_ticket_are_once(self):
        self.request()
        now = timezone.now()
        first = teams.run_team_request_sweep(now=now + timedelta(hours=73))
        second = teams.run_team_request_sweep(now=now + timedelta(hours=74))
        self.assertEqual(first['alerted'], 1)
        self.assertEqual(second['alerted'], 0)
        self.assertEqual(teams.run_team_request_sweep(now=now + timedelta(days=8))['expired'], 1)

    def test_T4_cookie_foreign_origin_refused_and_bearer_without_origin_accepted(self):
        from types import SimpleNamespace
        from community_chat.account_sessions import issue_account_session
        from .startup_team_views import JoinRequestDecisionView
        _, identifier = self.request()
        credentials = issue_account_session(self.owner, SimpleNamespace(client_id='synthetic-team',
            installation_id=uuid4(), origin='https://chat.synthetic.test', platform='web',
            device_name='Synthetic', public_key='d' * 64))
        data = {'company_id': str(self.fixture.company.pk), 'decision': 'approve'}
        with override_settings(COMMUNITY_CHAT_ALLOWED_ORIGINS=['https://chat.synthetic.test']):
            foreign = self.factory.post('/', data, format='json', HTTP_ORIGIN='https://attacker.synthetic.test')
            foreign.COOKIES['mlai_chat_access'] = credentials.access_token
            refused = JoinRequestDecisionView.as_view()(foreign, request_id=identifier)
            self.assertEqual(refused.status_code, 401)
            accepted = self.factory.post('/', data, format='json', HTTP_AUTHORIZATION='Bearer ' + credentials.access_token)
            result = JoinRequestDecisionView.as_view()(accepted, request_id=identifier)
        self.assertEqual(result.status_code, 200, result.data)
        for response in [refused, result]:
            self.assertEqual(response['Cache-Control'], 'private, no-store')

    def test_T5_every_facade_view_method_uses_the_single_member_allowlist(self):
        from rest_framework.response import Response
        from community_chat import my_startup_views as views, my_startup_urls
        from community_chat.throttles import StartupScopedThrottle
        row = self.approve()
        teams.update_member(organization=self.org, user=self.owner, member_id=row['memberId'], rights={'publish': True})
        run = make_run(self.org, workflow='direct_generate', run_id='member-article')
        def domain(request, *args, **kwargs):
            return Response({'domain_called': True})
        covered = set()
        with patch.object(StartupScopedThrottle, 'allow_request', return_value=True):
            for route, view in my_startup_urls.ROUTES:
                covered.add(view)
                for method in ['GET', 'POST', 'PUT', 'PATCH', 'DELETE']:
                    identity = (view.__module__, view.__name__)
                    allowed = method in views.MEMBER_ACCESS.get(identity, {})
                    # These reads return the caller's own account/chooser,
                    # independently of any supplied member workspace hint.
                    allowed = allowed or (method == 'GET' and view in {
                        views.AccountView, views.ProfileView, views.CompaniesView})
                    with self.subTest(route=route, method=method), patch.object(view, method.lower(), side_effect=domain, create=True) as handler:
                        kwargs = {'run_id': run.run_id} if '<str:run_id>' in route else {}
                        response = self.call(view, user=self.member, method=method.lower(),
                            request_format='multipart' if view is views.AvatarView else 'json', **kwargs)
                        self.assertEqual(response.status_code, 200 if allowed else 404, response.data)
                        self.assertEqual(handler.call_count, int(allowed))
            for action in ['approve', 'publish-pr', 'resume', 'restart', 'deny']:
                with patch.object(views.ArticleRunActionView, 'post', side_effect=domain) as handler:
                    response = self.call(views.ArticleRunActionView, user=self.member, method='post', run_id=run.run_id, action=action)
                    self.assertEqual(response.status_code, 200 if action in {'approve', 'publish-pr'} else 404)
                    self.assertEqual(handler.call_count, int(action in {'approve', 'publish-pr'}))
        self.assertEqual(len(covered), len({view for _, view in my_startup_urls.ROUTES}))

    def test_T5_setup_scaffold_proxy_approval_stops_before_domain_view(self):
        from community_chat.my_startup_views import ArticleRunActionView
        from rest_framework.response import Response
        row = self.approve()
        teams.update_member(organization=self.org, user=self.owner, member_id=row['memberId'], rights={'publish': True})
        for workflow, kind in [('article_system_setup', 'setup'), ('repo_scan', 'scaffold'), ('direct_generate', 'proxy')]:
            run = make_run(self.org, workflow=workflow, result={'publish_pr_v1': {'kind': kind}})
            with patch.object(ArticleRunActionView, 'post', return_value=Response({'unsafe': True})) as domain:
                response = self.call(ArticleRunActionView, user=self.member, method='post', run_id=run.run_id, action='approve')
            self.assertEqual(response.status_code, 403, response.data)
            self.assertEqual(response.data['reason'], 'owner_only')
            domain.assert_not_called()

    def test_T14_context_resets_after_a_domain_exception(self):
        from community_chat.my_startup_views import ArticleView
        self.approve()
        with patch.object(ArticleView, 'post', side_effect=RuntimeError('synthetic failure')):
            with self.assertRaises(RuntimeError):
                self.call(ArticleView, user=self.member, method='post')
        self.assertIsNone(teams.bound_member_context(self.member, self.fixture.company.pk))

    def test_member_company_list_and_team_view_use_no_member_owned_rows(self):
        from community_chat.my_startup_views import CompaniesView
        from .startup_team_views import TeamView
        self.approve()
        response = self.call(CompaniesView, user=self.member, company=False)
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data[0]['role'], 'member')
        self.assertEqual(response.data[0]['id'], str(self.fixture.company.pk))
        response = self.call(TeamView, user=self.member)
        self.assertEqual(response.status_code, 200, response.data)
        self.assertFalse(response.data['canManage'])
        self.assertEqual(response['Cache-Control'], 'private, no-store')

    def test_T7_ten_pending_requests_per_organization(self):
        for _ in range(10):
            self.request(self.user())
        with self.assertRaises(teams.TeamError) as error:
            self.request(self.user())
        self.assertEqual(error.exception.status, 429)
        self.assertEqual(teams.pending_request_count(self.org), 10)

    def test_T7_user_daily_and_weekly_limits_cover_different_organizations(self):
        now = timezone.now()
        monday = now - timedelta(days=now.weekday())
        organizations = [make_p2_org().organization for _ in range(12)]
        for index in range(10):
            nonce = teams.issue_join_reference(user=self.member, organization=organizations[index])
            teams.request_join(user=self.member, join_reference=nonce, now=monday + timedelta(days=index // 3))
        nonce = teams.issue_join_reference(user=self.member, organization=organizations[10])
        with self.assertRaises(teams.TeamError) as error:
            teams.request_join(user=self.member, join_reference=nonce, now=monday + timedelta(days=3))
        self.assertEqual(error.exception.status, 429)
        daily_user = self.user()
        for organization in organizations[:3]:
            nonce = teams.issue_join_reference(user=daily_user, organization=organization)
            teams.request_join(user=daily_user, join_reference=nonce, now=now)
        nonce = teams.issue_join_reference(user=daily_user, organization=organizations[3])
        with self.assertRaises(teams.TeamError) as error:
            teams.request_join(user=daily_user, join_reference=nonce, now=now)
        self.assertEqual(error.exception.reason, 'rate_limited')

    def test_T6_unconfirmed_members_have_no_publish_or_startup_spend(self):
        self.request()
        config = OrganizationContentConfig.objects.get(organization=self.org)
        roster = config.pillar_strategy['startup_team']
        teams._join(roster, self.member, state='unconfirmed', via='domain', actor=None, now=timezone.now())
        config.save(update_fields=['pillar_strategy'])
        self.assertIsNotNone(teams.member_context(self.member, self.fixture.company.pk, need='write_articles'))
        self.assertIsNone(teams.member_context(self.member, self.fixture.company.pk, need='publish'))
        self.assertIsNone(teams.member_context(self.member, self.fixture.company.pk, need='spend_startup_points'))
        row = self.roster()['members'][0]
        with self.assertRaises(teams.TeamError):
            teams.update_member(organization=self.org, user=self.owner, member_id=row['memberId'], rights={'publish': True})

    def test_T8_join_logs_only_member_id_and_token_never_appears_in_routes(self):
        from community_chat.my_startup_urls import urlpatterns
        token = teams.invite_link(organization=self.org, user=self.owner, action='create')['url'].split('#')[1]
        with self.assertLogs(teams.logger, level='INFO') as logs:
            teams.join_invite(user=self.member, token=token)
        self.assertNotIn(token, ' '.join(logs.output))
        for route in urlpatterns:
            self.assertNotIn('<token', str(route.pattern))
            self.assertNotIn('<str:token', str(route.pattern))

    def test_account_profile_capability_is_additive_and_flag_gated(self):
        from community_chat.my_startup_views import ProfileView
        response = self.call(ProfileView, user=self.member, company=False)
        self.assertIs(response.data['teamAvailable'], True)
        self.assertEqual(response.data['companies'], [])
        with override_settings(MY_STARTUP_TEAM_ENABLED=False):
            response = self.call(ProfileView, user=self.member, company=False)
        self.assertNotIn('teamAvailable', response.data)

    def prove_mailbox(self, user, email):
        from community_chat.email_codes import email_digest
        from community_chat.models import CommunityChatEmailCodeChallenge
        user.email, user.email_verified_at = email, timezone.now()
        user.save(update_fields=['email', 'email_verified_at', 'updated_at'])
        CommunityChatEmailCodeChallenge.objects.create(user=user, email_digest=email_digest(email), code_digest='a' * 64,
            client_id='synthetic-team', installation_id=uuid4(), origin='https://chat.synthetic.test', platform='web',
            public_key='c' * 64, expires_at=timezone.now() + timedelta(minutes=10), consumed_at=timezone.now())

    def test_T6_auto_join_is_separately_gated_and_requires_real_current_mail_proof(self):
        domain = f'acme-{uuid4().hex[:12]}.com.au'
        self.org.domain = domain
        self.org.save(update_fields=['domain'])
        self.fixture.company.domain = domain
        self.fixture.company.save(update_fields=['domain'])
        self.prove_mailbox(self.owner, 'owner@' + domain)
        self.prove_mailbox(self.member, 'member@' + domain)
        nonce = teams.issue_join_reference(user=self.member, organization=self.org)
        with override_settings(MY_STARTUP_TEAM_DOMAIN_AUTO_JOIN_ENABLED=True):
            self.assertEqual(teams.request_join(user=self.member, join_reference=nonce), {'status': 'joined'})
        self.assertEqual(self.roster()['members'][0]['state'], 'unconfirmed')
        identifier = self.roster()['requests'][0]['request_id']
        teams.decide_request(organization=self.org, user=self.owner, request_id=identifier, decision='approve')
        self.assertEqual(self.roster()['members'][0]['state'], 'active')
        self.assertEqual(self.roster()['members'][0]['rights'], {'publish': False, 'billing': False})

    def test_T6_typed_domain_or_old_email_stamp_does_not_auto_join(self):
        domain = f'acme-{uuid4().hex[:12]}.com.au'
        self.org.domain = domain
        self.org.save(update_fields=['domain'])
        self.owner.email = 'owner@' + domain
        self.owner.save(update_fields=['email', 'updated_at'])
        self.member.email = 'member@' + domain
        self.member.save(update_fields=['email', 'updated_at'])
        nonce = teams.issue_join_reference(user=self.member, organization=self.org)
        with override_settings(MY_STARTUP_TEAM_DOMAIN_AUTO_JOIN_ENABLED=True):
            result = teams.request_join(user=self.member, join_reference=nonce)
        self.assertEqual(result, {'status': 'requested'})
        self.assertEqual(self.roster()['members'], [])

    def test_T6_any_website_connection_requires_owner_approval(self):
        from content_factory.website_models import WebsiteConnection
        domain = f'acme-{uuid4().hex[:12]}.com.au'
        self.org.domain = domain
        self.org.save(update_fields=['domain'])
        self.prove_mailbox(self.owner, 'owner@' + domain)
        self.prove_mailbox(self.member, 'member@' + domain)
        WebsiteConnection.objects.create(organization=self.org, github_repo='example/site')
        nonce = teams.issue_join_reference(user=self.member, organization=self.org)
        with override_settings(MY_STARTUP_TEAM_DOMAIN_AUTO_JOIN_ENABLED=True):
            self.assertEqual(teams.request_join(user=self.member, join_reference=nonce), {'status': 'requested'})

    def test_T1_claimed_company_response_adds_only_opaque_reference(self):
        from community_chat.my_startup_views import CompaniesView
        response = self.call(CompaniesView, user=self.member, method='post', company=False,
            data={'name': 'Existing startup', 'domain': self.org.domain})
        self.assertEqual(response.status_code, 409, response.data)
        self.assertIn('joinReference', response.data)
        self.assertNotIn(self.owner.email, json.dumps(response.data))
        self.assertNotIn('organization_id', response.data)
        nonce = response.data['joinReference']
        self.assertEqual(teams.request_join(user=self.member, join_reference=nonce), {'status': 'requested'})

    def test_T14_bound_member_selection_never_changes_active_company(self):
        self.approve()
        context = teams.member_context(self.member, self.fixture.company.pk, need='read')
        before = context.profile.active_company_id
        token = teams.bind_member_context(context)
        try:
            selected = get_founder_company_context(self.member, self.fixture.company.pk, persist_active=True)
            self.assertEqual(selected.role, 'member')
        finally:
            teams.reset_member_context(token)
        context.profile.refresh_from_db()
        self.assertEqual(context.profile.active_company_id, before)

    def test_T13_owner_pin_survives_flag_rollback(self):
        row = self.approve()
        self.assertTrue(teams.freeze_for_offboard(self.org, self.owner))
        self.fixture.company.delete()
        with override_settings(MY_STARTUP_TEAM_ENABLED=False):
            self.assertIsNone(organization_owner_user_id(self.org))
            self.assertIsNone(teams.member_context(self.member, row['memberId'], need='read'))

    def test_T9_member_preview_capability_is_revoked_with_membership(self):
        from urllib.parse import unquote, urlsplit
        from community_chat.my_startup_views import ArticlePreviewView
        from content_factory.article_preview_lease import ArticlePreviewLeaseProxyView, SALT
        from django.core import signing
        row = self.approve()
        run = make_run(self.org, workflow='direct_generate', run_request={'delivery_mode': 'content_only',
            'delivery_mode_confirmed': True, 'resolved_delivery_mode': 'content_only'})
        response = self.call(ArticlePreviewView, user=self.member, method='post', run_id=run.run_id)
        self.assertEqual(response.status_code, 200, response.data)
        path = urlsplit(response.data['url']).path
        token = unquote(path.split('/article-preview/', 1)[1].split('/', 1)[0])
        grant = signing.loads(token, salt=SALT)
        self.assertEqual(grant['member_user_id'], self.member.pk)
        proxy = ArticlePreviewLeaseProxyView()
        proxy.kwargs = {'token': token}
        request = self.factory.get('/')
        _, _, error = proxy._resolve_run(request, run.run_id)
        self.assertIsNone(error)
        teams.update_member(organization=self.org, user=self.owner, member_id=row['memberId'], remove=True)
        _, _, error = proxy._resolve_run(request, run.run_id)
        self.assertEqual(error.status_code, 404)

    def test_T11_pending_requests_alone_do_not_prevent_normal_content_purge(self):
        self.request()
        run = make_run(self.org, workflow='direct_generate')
        from .services import offboard_company
        with patch('startup_updates.data_deletion.disconnect_gmail_for_user', return_value={}), \
                patch('startup_updates.data_deletion.delete_startup_data_for_organization', return_value={}), \
                patch('content_factory.website_connections.offboard_website_connections'):
            offboard_company(self.fixture.company)
        self.assertFalse(type(run).objects.filter(pk=run.pk).exists())
        self.assertTrue(self.roster()['frozen'])
        self.assertIsNone(organization_owner_user_id(self.org))

    def test_unicode_company_snapshot_and_member_names_round_trip(self):
        self.fixture.company.name = 'Café founder’s studio'
        self.fixture.company.save(update_fields=['name'])
        self.member.first_name = 'Zoë'
        self.member.save(update_fields=['first_name'])
        self.approve()
        self.assertEqual(self.roster()['company']['name'], 'Café founder’s studio')
        payload = teams.team_payload(organization=self.org, user=self.owner)
        self.assertEqual(payload['members'][0]['firstName'], 'Zoë')


class DomainRulesTests(SimpleTestCase):
    def test_T6_public_suffix_wildcards_exceptions_idna_and_private_hosts(self):
        for host, apex in [('team.example.com.au', 'example.com.au'), ('www.example.co.uk', 'example.co.uk'),
                           ('www.ck', 'www.ck'), ('a.b.ck', 'a.b.ck'), ('www.city.kawasaki.jp', 'city.kawasaki.jp'),
                           ('bücher.de', 'xn--bcher-kva.de')]:
            self.assertEqual(registrable_domain(host), apex)
        for host in ['localhost', '127.0.0.1', 'com.au', 'invalid.invalid', 'bad host.com', 'a..com']:
            self.assertIsNone(registrable_domain(host))

    def test_T6_shared_free_and_isp_mail_are_denied_at_every_depth(self):
        for host in ['acme.wixsite.com', 'gmail.com', 'bigpond.com', 'team.vercel.app', 'x.github.io', 'x.pages.dev', 'x.netlify.app']:
            self.assertTrue(denied_domain(host), host)
        self.assertFalse(denied_domain('example.com.au'))
