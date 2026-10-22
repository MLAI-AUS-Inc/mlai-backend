"""Staff-only Mentoring admin service. ORM and transactions are mocked: no database, no network."""
from datetime import datetime, timedelta, timezone as tz
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, patch

from mentoring import admin_service as svc
from mentoring.admin_service import MentoringAdminService, invite_status, session_flags
from mentoring.models import MentorSession
from mentoring.service import MentoringError

NOW = datetime(2026, 11, 2, 12, 0, tzinfo=tz.utc)
STAFF = SimpleNamespace(pk=1, email='staff@example.com', full_name='Sam Staff')


def user(pk=5, name='Ada Lovelace', email='ada@example.com'):
    return SimpleNamespace(pk=pk, full_name=name, email=email, avatar_url=None)


def invite(**kw):
    base = dict(pk=3, user=user(), created_by=STAFF, revoked_by=None, created_at=NOW,
                expires_at=NOW + timedelta(days=14), used_at=None, revoked_at=None, save=MagicMock())
    base.update(kw)
    return SimpleNamespace(**base)


def profile(**kw):
    base = dict(pk=9, user=user(), reviewed_by=None, is_active=False, approved_at=None, rejected_at=None,
                paused_at=None, headline='Coach', session_minutes=30, price_roo=40, location_type='video',
                updated_at=NOW, save=MagicMock(), sessions_count=2)
    base.update(kw)
    return SimpleNamespace(**base)


def session(**kw):
    base = dict(pk='abc', status='accepted', starts_at=NOW, ends_at=NOW, price_roo=40, location_type='video',
                mentor_id=9, mentor=SimpleNamespace(user=user(7, 'Mia Mentor')), founder=user(),
                meeting_status='created', meeting_error='', meeting_claimed_at=None, notification_status='sent',
                requested_at=NOW, responded_at=None, cancelled_at=None)
    base.update(kw)
    return SimpleNamespace(**base)


class Fixture(unittest.TestCase):
    def setUp(self):
        for p in (
            patch.object(svc, 'transaction', MagicMock()),
            patch.object(svc.timezone, 'now', return_value=NOW),
        ):
            p.start()
            self.addCleanup(p.stop)

    def code(self, fn, *a, **kw):
        with self.assertRaises(MentoringError) as ctx:
            fn(*a, **kw)
        return ctx.exception.code, ctx.exception.status_code


class InviteTests(Fixture):
    def test_status_is_one_of_four(self):
        self.assertEqual(invite_status(invite(), NOW), 'pending')
        self.assertEqual(invite_status(invite(used_at=NOW), NOW), 'used')
        self.assertEqual(invite_status(invite(expires_at=NOW), NOW), 'expired')
        self.assertEqual(invite_status(invite(revoked_at=NOW, used_at=NOW), NOW), 'revoked')

    def test_create_returns_link_once_and_records_creator(self):
        issued = invite()
        users = MagicMock()
        users.objects.filter.return_value.first.return_value = user()
        profiles = MagicMock()
        profiles.objects.filter.return_value.exists.return_value = False
        invites = MagicMock()
        invites.issue.return_value = (issued, 'TOKEN123')
        with patch.object(svc, 'get_user_model', return_value=users), patch.object(svc, 'MentorProfile', profiles), \
                patch.object(svc, 'MentorInvite', invites), patch.object(svc, 'invite_link', side_effect=lambda t: f'L/{t}'):
            data = MentoringAdminService.create_invite(STAFF, 5)
        invites.issue.assert_called_once()
        self.assertIs(invites.issue.call_args.kwargs['created_by'], STAFF)
        self.assertEqual(data['link'], 'L/TOKEN123')
        self.assertEqual(data['status'], 'pending')
        self.assertNotIn('token_hash', data)

    def test_create_rejects_bad_ids_unknown_and_active_mentors(self):
        for bad in ('5', None, True, 1.5):
            self.assertEqual(self.code(MentoringAdminService.create_invite, STAFF, bad), ('invalid_user', 400))
        users = MagicMock()
        users.objects.filter.return_value.first.return_value = None
        with patch.object(svc, 'get_user_model', return_value=users):
            self.assertEqual(self.code(MentoringAdminService.create_invite, STAFF, 5), ('member_not_found', 404))
        users.objects.filter.return_value.first.return_value = user()
        profiles = MagicMock()
        profiles.objects.filter.return_value.exists.return_value = True
        with patch.object(svc, 'get_user_model', return_value=users), patch.object(svc, 'MentorProfile', profiles):
            self.assertEqual(self.code(MentoringAdminService.create_invite, STAFF, 5), ('already_a_mentor', 409))

    def revoke_with(self, row):
        invites = MagicMock()
        invites.objects.select_for_update.return_value.select_related.return_value.filter.return_value.first.return_value = row
        return patch.object(svc, 'MentorInvite', invites)

    def test_revoke_records_who_and_when(self):
        row = invite()
        with self.revoke_with(row):
            data = MentoringAdminService.revoke_invite(STAFF, 3)
        self.assertEqual(row.revoked_at, NOW)
        self.assertIs(row.revoked_by, STAFF)
        row.save.assert_called_once()
        self.assertEqual(data['status'], 'revoked')

    def test_revoke_twice_is_a_noop_and_used_invites_cannot_be_revoked(self):
        row = invite(revoked_at=NOW)
        with self.revoke_with(row):
            self.assertEqual(MentoringAdminService.revoke_invite(STAFF, 3)['status'], 'revoked')
        row.save.assert_not_called()
        with self.revoke_with(invite(used_at=NOW)):
            self.assertEqual(self.code(MentoringAdminService.revoke_invite, STAFF, 3), ('invite_used', 409))
        with self.revoke_with(None):
            self.assertEqual(self.code(MentoringAdminService.revoke_invite, STAFF, 3), ('invite_not_found', 404))

    def test_list_filters_by_status(self):
        expected = {
            'revoked': {'revoked_at__isnull': False},
            'used': {'revoked_at__isnull': True, 'used_at__isnull': False},
            'expired': {'revoked_at__isnull': True, 'used_at__isnull': True, 'expires_at__lte': NOW},
            'pending': {'revoked_at__isnull': True, 'used_at__isnull': True, 'expires_at__gt': NOW},
        }
        for status, kwargs in expected.items():
            invites = MagicMock()
            query = invites.objects.select_related.return_value
            query.filter.return_value.__getitem__.return_value = [invite()]
            with patch.object(svc, 'MentorInvite', invites):
                MentoringAdminService.list_invites(status)
            query.filter.assert_called_once_with(**kwargs)
        self.assertEqual(self.code(MentoringAdminService.list_invites, 'open'), ('invalid_status', 400))


class ApplicationTests(Fixture):
    def lock_with(self, row):
        profiles = MagicMock()
        profiles.objects.select_for_update.return_value.select_related.return_value.filter.return_value.first.return_value = row
        return patch.object(svc, 'MentorProfile', profiles)

    def test_approve_activates_and_records_reviewer(self):
        row = profile(rejected_at=NOW - timedelta(days=1))
        with self.lock_with(row):
            data = MentoringAdminService.approve_application(STAFF, 9)
        self.assertTrue(row.is_active)
        self.assertEqual(row.approved_at, NOW)
        self.assertIsNone(row.rejected_at)
        self.assertIs(row.reviewed_by, STAFF)
        row.save.assert_called_once()
        self.assertEqual(data['state'], 'pending')

    def test_reject_keeps_inactive_and_marks_rejected(self):
        row = profile()
        with self.lock_with(row):
            data = MentoringAdminService.reject_application(STAFF, 9)
        self.assertFalse(row.is_active)
        self.assertEqual(row.rejected_at, NOW)
        self.assertIs(row.reviewed_by, STAFF)
        self.assertEqual(data['state'], 'rejected')

    def test_only_pending_applications_can_be_reviewed(self):
        for row in (profile(is_active=True), profile(paused_at=NOW), profile(approved_at=NOW)):
            for fn in (MentoringAdminService.approve_application, MentoringAdminService.reject_application):
                with self.lock_with(row):
                    self.assertEqual(self.code(fn, STAFF, 9), ('not_an_application', 409))
            row.save.assert_not_called()
        with self.lock_with(None):
            self.assertEqual(self.code(MentoringAdminService.approve_application, STAFF, 9), ('application_not_found', 404))

    def test_list_filters_by_state_and_only_looks_at_invited_inactive_profiles(self):
        base = MagicMock()
        base.filter.return_value.distinct.return_value = base
        profiles = MagicMock()
        profiles.objects.select_related.return_value = base
        with patch.object(svc, 'MentorProfile', profiles):
            MentoringAdminService.list_applications('rejected')
        first = base.filter.call_args_list[0].kwargs
        self.assertEqual(first['is_active'], False)
        self.assertTrue(first['user__mentor_invites__used_at__isnull'] is False)
        self.assertTrue(first['approved_at__isnull'] and first['paused_at__isnull'])
        self.assertEqual(base.filter.call_args_list[1].kwargs, {'rejected_at__isnull': False})
        self.assertEqual(self.code(MentoringAdminService.list_applications, 'approved'), ('invalid_state', 400))


class MentorTests(Fixture):
    def lock_with(self, row):
        profiles = MagicMock()
        profiles.objects.select_for_update.return_value.select_related.return_value.filter.return_value.first.return_value = row
        sessions = MagicMock()
        sessions.objects.filter.return_value.count.return_value = 4
        return patch.object(svc, 'MentorProfile', profiles), patch.object(svc, 'MentorSession', sessions)

    def run_pause(self, row, paused):
        a, b = self.lock_with(row)
        with a, b:
            return MentoringAdminService.set_paused(STAFF, 9, paused)

    def test_pause_then_unpause(self):
        row = profile(is_active=True, approved_at=NOW)
        data = self.run_pause(row, True)
        self.assertFalse(row.is_active)
        self.assertEqual(row.paused_at, NOW)
        self.assertIs(row.reviewed_by, STAFF)
        self.assertEqual(data['sessions_count'], 4)
        self.run_pause(row, False)
        self.assertTrue(row.is_active)
        self.assertIsNone(row.paused_at)

    def test_pause_state_errors(self):
        self.assertEqual(self.code(self.run_pause, profile(is_active=False), True), ('not_active', 409))
        self.assertEqual(self.code(self.run_pause, profile(is_active=True), False), ('not_paused', 409))
        self.assertEqual(self.code(self.run_pause, profile(is_active=False, rejected_at=NOW), False), ('not_paused', 409))
        self.assertEqual(self.code(self.run_pause, None, True), ('mentor_not_found', 404))

    def test_mentor_row_has_price_length_location_and_count(self):
        row = svc.serialize_admin_mentor(profile(is_active=True))
        for key, value in (('price_roo', 40), ('session_minutes', 30), ('location_type', 'video'), ('sessions_count', 2)):
            self.assertEqual(row[key], value)


class SessionTests(Fixture):
    def test_flags(self):
        self.assertEqual(session_flags(session(), NOW), [])
        self.assertEqual(session_flags(session(meeting_status='failed'), NOW), ['meeting_failed'])
        self.assertEqual(session_flags(session(meeting_status='pending'), NOW), ['stale_pending'])
        fresh = session(meeting_status='pending', meeting_claimed_at=NOW - timedelta(seconds=5))
        self.assertEqual(session_flags(fresh, NOW), [])
        self.assertEqual(session_flags(session(notification_status='failed'), NOW), ['notification_failed'])
        both = session(meeting_status='failed', notification_status='failed')
        self.assertEqual(session_flags(both, NOW), ['meeting_failed', 'notification_failed'])
        # in-person and not-yet-accepted sessions have no meeting to fail
        self.assertEqual(session_flags(session(location_type='mlai_space', meeting_status='failed'), NOW), [])
        self.assertEqual(session_flags(session(status='requested', meeting_status='failed'), NOW), [])

    def list_with(self, **kwargs):
        sessions = MagicMock()
        sessions.STATUS_CHOICES = MentorSession.STATUS_CHOICES
        for name in ('STATUS_ACCEPTED', 'STATUS_COMPLETED', 'MEETING_FAILED', 'MEETING_PENDING', 'NOTIFY_FAILED'):
            setattr(sessions, name, getattr(MentorSession, name))
        query = sessions.objects.select_related.return_value
        query.filter.return_value = query
        query.order_by.return_value.__getitem__.return_value = [session()]
        with patch.object(svc, 'MentorSession', sessions):
            result = MentoringAdminService.list_sessions(**kwargs)
        return query, result

    def test_status_filter(self):
        query, result = self.list_with(status='cancelled')
        query.filter.assert_called_once_with(status='cancelled')
        self.assertEqual(result[0]['mentor']['name'], 'Mia Mentor')
        self.assertEqual(result[0]['founder']['email'], 'ada@example.com')

    def test_no_filter_does_not_filter(self):
        query, _ = self.list_with()
        query.filter.assert_not_called()

    def test_problem_filters(self):
        query, _ = self.list_with(problem='notification_failed')
        query.filter.assert_called_once_with(notification_status='failed')
        query, _ = self.list_with(problem='meeting_failed')
        self.assertEqual(query.filter.call_args.kwargs, {'meeting_status': 'failed'})
        query, _ = self.list_with(status='accepted', problem='stale_pending')
        self.assertEqual(query.filter.call_count, 3)

    def test_unknown_filters_are_400(self):
        for kwargs, code in (({'status': 'open'}, 'invalid_status'), ({'problem': 'bad'}, 'invalid_problem')):
            self.assertEqual(self.code(MentoringAdminService.list_sessions, **kwargs), (code, 400))


class MemberSearchTests(Fixture):
    def test_short_or_missing_query_returns_nothing_without_touching_the_database(self):
        for q in (None, '', ' a ', 5):
            self.assertEqual(MentoringAdminService.search_members(q), [])

    def test_search_matches_and_hides_inactive(self):
        users = MagicMock()
        users.objects.filter.return_value.order_by.return_value.__getitem__.return_value = [user()]
        with patch.object(svc, 'get_user_model', return_value=users):
            result = MentoringAdminService.search_members('ada')
        self.assertEqual(result, [{'id': 5, 'name': 'Ada Lovelace', 'email': 'ada@example.com', 'avatar_url': None}])
        self.assertTrue(users.objects.filter.call_args.kwargs['is_active'])


if __name__ == '__main__':
    unittest.main()
