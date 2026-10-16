"""Service logic with PointsService and the ORM mocked. No database, no network."""
from datetime import datetime, timedelta, timezone as tz
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, patch
import uuid

from mentoring import service
from mentoring.models import MentorSession
from mentoring.service import MentoringError, MentoringService

NOW = datetime(2026, 11, 2, 0, 0, tzinfo=tz.utc)
START = NOW + timedelta(days=3)
SID = uuid.uuid4()


def make_session(status='requested', **kw):
    mentor_user = SimpleNamespace(pk=2, slack_id='UMENTOR', full_name='Mia Mentor')
    founder = SimpleNamespace(pk=1, slack_id='UFOUNDER', full_name='Fi Founder')
    base = dict(
        pk=SID, status=status, mentor_id=7, mentor=SimpleNamespace(pk=7, user=mentor_user, user_id=2, timezone='Australia/Melbourne'),
        founder=founder, founder_id=1, price_roo=40, purchased_points_cost_microroo=10_000_000,
        hold_ledger_entry_id=99, release_ledger_entry=None, payout_ledger_entry=None,
        starts_at=START, ends_at=START + timedelta(minutes=30), expires_at=NOW + timedelta(hours=48),
        responded_at=None, completed_at=None, cancelled_at=None, cancelled_by=None,
    )
    base.update(kw)
    s = SimpleNamespace(**base)
    s.save = MagicMock()
    return s


class Base(unittest.TestCase):
    def setUp(self):
        self.points = MagicMock()
        self.points.refund.return_value = ('refund-ledger', True)
        self.points.award.return_value = ('payout-ledger', True)
        self.points.spend.return_value = ('hold-ledger', True)
        self.points.roo_to_microroo.side_effect = lambda n: n * 1_000_000
        self.clock = NOW
        for p in (
            patch.object(service, 'PointsService', self.points),
            patch.object(service, 'transaction', MagicMock()),
            patch.object(service, '_lock_keys'),
            patch.object(service.timezone, 'now', side_effect=lambda: self.clock),
        ):
            p.start()
            self.addCleanup(p.stop)
        self.mentor = SimpleNamespace(pk=7, user=SimpleNamespace(pk=2), user_id=2)
        self.actor_mentor = SimpleNamespace(pk=2, slack_id='UMENTOR')
        self.actor_founder = SimpleNamespace(pk=1, slack_id='UFOUNDER')

    def load(self, session):
        p = patch.object(MentoringService, '_load_for_update', return_value=session)
        p.start()
        self.addCleanup(p.stop)


class TransitionTests(Base):
    def test_decline_refunds_with_release_key_and_exact_split(self):
        s = make_session()
        self.load(s)
        MentoringService.decline(SID, self.actor_mentor)
        kw = self.points.refund.call_args.kwargs
        self.assertEqual(kw['idempotency_key'], f'mentor_release:{SID}')
        self.assertEqual(kw['delta'], 40)
        self.assertEqual(kw['purchased_delta_microroo'], 10_000_000)
        self.assertEqual(kw['source'], 'MANUAL')
        self.assertEqual(kw['reference_type'], 'MENTOR_SESSION')
        self.assertTrue(kw['reverse_lifetime_spent'])
        self.assertEqual(kw['user'], s.founder)
        self.assertEqual(s.status, 'declined')

    def test_only_mentor_can_accept_decline_complete(self):
        for fn in (MentoringService.accept, MentoringService.decline, MentoringService.complete):
            self.load(make_session())
            with self.assertRaises(MentoringError) as ctx:
                fn(SID, self.actor_founder)
            self.assertEqual(ctx.exception.code, 'not_session_mentor')
        self.points.refund.assert_not_called()

    def test_accept_does_not_move_points(self):
        s = make_session()
        self.load(s)
        with patch.object(service.MentorSession, 'objects') as objs:
            objs.filter.return_value.exclude.return_value.exists.return_value = False
            MentoringService.accept(SID, self.actor_mentor)
        self.assertEqual(s.status, 'accepted')
        self.points.refund.assert_not_called()
        self.points.award.assert_not_called()

    def test_accept_blocked_by_overlapping_accepted_session(self):
        self.load(make_session())
        with patch.object(service.MentorSession, 'objects') as objs:
            objs.filter.return_value.exclude.return_value.exists.return_value = True
            with self.assertRaises(MentoringError) as ctx:
                MentoringService.accept(SID, self.actor_mentor)
        self.assertEqual(ctx.exception.code, 'slot_unavailable')

    def test_accept_after_48h_refunds_and_reports_expired(self):
        s = make_session()
        self.load(s)
        self.clock = NOW + timedelta(hours=48, seconds=1)
        with self.assertRaises(MentoringError) as ctx:
            MentoringService.accept(SID, self.actor_mentor)
        self.assertEqual(ctx.exception.code, 'request_expired')
        self.assertEqual(s.status, 'expired')
        self.assertEqual(self.points.refund.call_args.kwargs['idempotency_key'], f'mentor_release:{SID}')

    def test_cancel_by_founder_or_mentor_before_start_refunds(self):
        for actor in (self.actor_founder, self.actor_mentor):
            self.points.refund.reset_mock()
            s = make_session('accepted')
            self.load(s)
            MentoringService.cancel(SID, actor)
            self.assertEqual(s.status, 'cancelled')
            self.assertEqual(s.cancelled_by, actor)
            self.points.refund.assert_called_once()

    def test_cancel_after_start_is_refused_without_refund(self):
        self.load(make_session('accepted'))
        self.clock = START + timedelta(minutes=1)
        with self.assertRaises(MentoringError) as ctx:
            MentoringService.cancel(SID, self.actor_founder)
        self.assertEqual(ctx.exception.code, 'session_started')
        self.points.refund.assert_not_called()

    def test_complete_pays_mentor_with_payout_key(self):
        s = make_session('accepted')
        self.load(s)
        self.clock = START + timedelta(minutes=45)
        MentoringService.complete(SID, self.actor_mentor)
        kw = self.points.award.call_args.kwargs
        self.assertEqual(kw['idempotency_key'], f'mentor_payout:{SID}')
        self.assertEqual(kw['delta'], 40)
        self.assertEqual(kw['user'], s.mentor.user)
        self.assertEqual(kw['source'], 'MANUAL')
        self.assertEqual(kw['reference_type'], 'MENTOR_SESSION')
        self.assertEqual(s.status, 'completed')

    def test_complete_before_start_is_refused(self):
        self.load(make_session('accepted'))
        with self.assertRaises(MentoringError) as ctx:
            MentoringService.complete(SID, self.actor_mentor)
        self.assertEqual(ctx.exception.code, 'session_not_started')
        self.points.award.assert_not_called()

    def test_auto_complete_24h_after_end_pays_once_on_read(self):
        s = make_session('accepted')
        self.clock = s.ends_at + timedelta(hours=24)
        MentoringService._apply_settlement(s)
        self.assertEqual(s.status, 'completed')
        self.points.award.assert_called_once()

    def test_no_auto_complete_before_grace(self):
        s = make_session('accepted')
        self.clock = s.ends_at + timedelta(hours=23)
        MentoringService._apply_settlement(s)
        self.assertEqual(s.status, 'accepted')
        self.points.award.assert_not_called()

    def test_retry_of_finished_action_is_success_without_double_points(self):
        s = make_session('declined')
        self.load(s)
        MentoringService.decline(SID, self.actor_mentor)
        self.points.refund.assert_not_called()

    def test_wrong_state_is_a_409(self):
        self.load(make_session('declined'))
        with self.assertRaises(MentoringError) as ctx:
            MentoringService.accept(SID, self.actor_mentor)
        self.assertEqual(ctx.exception.code, 'invalid_state')
        self.assertEqual(ctx.exception.status_code, 409)

    def test_notification_failure_never_changes_booking_status(self):
        s = make_session('requested')
        with patch.object(service.MentorSession, 'objects') as objs, \
                patch.dict('sys.modules', {'integrations.services.slack': MagicMock(
                    SlackService=MagicMock(send_dm=MagicMock(side_effect=RuntimeError('slack down'))))}):
            objs.select_related.return_value.get.return_value = s
            outcome = MentoringService.notify_mentor(SID)
            self.assertEqual(outcome, 'failed')
            update = objs.filter.return_value.update.call_args.kwargs
        self.assertEqual(set(update), {'notification_status', 'notification_attempted_at'})
        self.assertEqual(s.status, 'requested')

    def _notify(self, send_dm):
        s = make_session('requested')
        slack = MagicMock(SlackService=MagicMock(send_dm=send_dm))
        with patch.object(service.MentorSession, 'objects') as objs, \
                patch.dict('sys.modules', {'integrations.services.slack': slack}):
            objs.select_related.return_value.get.return_value = s
            return MentoringService.notify_mentor(SID), send_dm

    def test_dm_outcomes_are_recorded(self):
        outcome, send = self._notify(MagicMock(return_value=(True, '1.2')))
        self.assertEqual(outcome, 'sent')
        self.assertEqual(send.call_args.args[0], 'UMENTOR')
        self.assertEqual(self._notify(MagicMock(return_value=(False, None)))[0], 'failed')

    def test_staging_503_from_slack_only_sets_notification_status(self):
        from rest_framework.exceptions import APIException

        class StagingUnavailable(APIException):
            status_code = 503
            default_code = 'staging_integration_unavailable'

        outcome, _ = self._notify(MagicMock(side_effect=StagingUnavailable(
            {'code': 'staging_integration_unavailable', 'capability': 'slack'})))
        self.assertEqual(outcome, 'failed')

    def test_mentor_without_slack_is_skipped(self):
        s = make_session('requested')
        s.mentor.user.slack_id = None
        with patch.object(service.MentorSession, 'objects') as objs:
            objs.select_related.return_value.get.return_value = s
            self.assertEqual(MentoringService.notify_mentor(SID), 'skipped')


class FakeSession:
    """Stands in for the model class in request_session."""
    STATUS_REQUESTED, STATUS_ACCEPTED = 'requested', 'accepted'
    ACTIVE_STATUSES = ('requested', 'accepted')
    objects = MagicMock()
    created = []

    def __init__(self, **kw):
        self.__dict__.update(kw)
        self.mentor_id = kw['mentor'].pk
        self.pk = SID
        self.save = MagicMock()
        FakeSession.created.append(self)


class RequestTests(Base):
    def setUp(self):
        super().setUp()
        FakeSession.created = []
        FakeSession.objects = MagicMock()
        FakeSession.objects.select_for_update.return_value.select_related.return_value.filter.return_value.first.return_value = None
        FakeSession.objects.filter.return_value.exists.return_value = False
        self.profile = SimpleNamespace(
            pk=7, user=SimpleNamespace(pk=2, slack_id='UMENTOR'), user_id=2, is_active=True,
            price_roo=40, session_minutes=30, timezone='Australia/Melbourne',
            location_type='video', video_link='https://meet.example/mia', space_notes='',
        )
        self.slot = START
        account = SimpleNamespace(purchased_topup_balance_microroo=15_000_000)
        profile_cls = MagicMock()
        profile_cls.objects.select_for_update.return_value.select_related.return_value.get.return_value = self.profile
        acct_cls = MagicMock()
        acct_cls.objects.select_for_update.return_value.filter.return_value.first.return_value = account
        for p in (
            patch.object(service, 'MentorSession', FakeSession),
            patch.object(service, 'MentorProfile', profile_cls),
            patch.object(service, 'PointsAccount', acct_cls),
            patch.object(MentoringService, '_get_active_mentor', return_value=self.profile),
            patch.object(MentoringService, 'sweep'),
            patch.object(MentoringService, 'free_slots', side_effect=lambda *a, **k: [self.slot]),
            patch.object(MentoringService, 'notify_mentor'),
        ):
            p.start()
            self.addCleanup(p.stop)
        self.founder = SimpleNamespace(pk=1, slack_id='UF')
        self.brief = {'stage': 'idea', 'struggle': 'no users', 'outcome': 'a plan'}

    def call(self, **kw):
        args = dict(
            founder=self.founder, mentor_id=7, starts_at=START.isoformat(), location_type=None,
            brief=self.brief, client_request_id=str(uuid.uuid4()), expected_price=None,
        )
        args.update(kw)
        return MentoringService.request_session(**args)

    def test_request_spends_default_40_with_hold_key_and_snapshots(self):
        session, created = self.call()
        self.assertTrue(created)
        kw = self.points.spend.call_args.kwargs
        self.assertEqual(kw['delta'], 40)
        self.assertEqual(kw['idempotency_key'], f'mentor_hold:{SID}')
        self.assertEqual(kw['source'], 'MANUAL')
        self.assertEqual(kw['reference_type'], 'MENTOR_SESSION')
        self.assertEqual(kw['user'], self.founder)
        self.assertEqual(session.price_roo, 40)
        self.assertEqual(session.purchased_points_cost_microroo, 15_000_000)
        self.assertEqual(session.expires_at, NOW + timedelta(hours=48))
        self.assertEqual(session.location_detail, 'https://meet.example/mia')
        self.assertEqual(session.ends_at - session.starts_at, timedelta(minutes=30))
        service.MentoringService.notify_mentor.assert_called_once()

    def test_insufficient_points_maps_to_409_and_no_session_is_returned(self):
        self.points.spend.side_effect = service.InsufficientBalanceError('no')
        with self.assertRaises(MentoringError) as ctx:
            self.call()
        self.assertEqual((ctx.exception.code, ctx.exception.status_code), ('insufficient_points', 409))
        service.MentoringService.notify_mentor.assert_not_called()

    def test_slot_not_free_is_rejected_before_any_charge(self):
        self.slot = START + timedelta(hours=1)
        with self.assertRaises(MentoringError) as ctx:
            self.call()
        self.assertEqual(ctx.exception.code, 'slot_unavailable')
        self.points.spend.assert_not_called()

    def test_price_changed_is_rejected(self):
        with self.assertRaises(MentoringError) as ctx:
            self.call(expected_price=35)
        self.assertEqual(ctx.exception.code, 'price_changed')
        self.points.spend.assert_not_called()

    def test_cannot_book_yourself(self):
        with self.assertRaises(MentoringError) as ctx:
            self.call(founder=SimpleNamespace(pk=2, slack_id='U'))
        self.assertEqual(ctx.exception.code, 'own_mentor_profile')

    def test_brief_is_required(self):
        for brief in (None, {}, {'stage': 'x', 'struggle': 'y'}, {'stage': ' ', 'struggle': 'y', 'outcome': 'z'}):
            with self.subTest(brief=brief), self.assertRaises(MentoringError) as ctx:
                self.call(brief=brief)
            self.assertIn(ctx.exception.code, ('invalid_brief', 'invalid_field'))
        self.points.spend.assert_not_called()

    def test_naive_time_and_bad_request_id_rejected(self):
        with self.assertRaises(MentoringError):
            self.call(starts_at='2026-11-05T10:00:00')
        with self.assertRaises(MentoringError):
            self.call(client_request_id='nope')

    def test_replay_returns_existing_without_charging(self):
        existing = make_session(starts_at=START)
        existing.mentor_id = 7
        FakeSession.objects.select_for_update.return_value.select_related.return_value.filter.return_value.first.return_value = existing
        session, created = self.call()
        self.assertFalse(created)
        self.assertIs(session, existing)
        self.points.spend.assert_not_called()

    def test_replay_with_different_time_is_a_conflict(self):
        existing = make_session(starts_at=START + timedelta(days=1))
        existing.mentor_id = 7
        FakeSession.objects.select_for_update.return_value.select_related.return_value.filter.return_value.first.return_value = existing
        with self.assertRaises(MentoringError) as ctx:
            self.call()
        self.assertEqual(ctx.exception.code, 'idempotency_conflict')

    def test_mentor_offering_either_location_needs_a_choice(self):
        self.profile.location_type = 'either'
        with self.assertRaises(MentoringError) as ctx:
            self.call()
        self.assertEqual(ctx.exception.code, 'location_required')
        self.profile.space_notes = 'Level 2, desk 4'
        session, _ = self.call(location_type='mlai_space')
        self.assertEqual((session.location_type, session.location_detail), ('mlai_space', 'Level 2, desk 4'))

    def test_request_still_succeeds_if_notification_raises(self):
        service.MentoringService.notify_mentor.side_effect = RuntimeError('slack 503')
        session, created = self.call()
        self.assertTrue(created)
        self.points.spend.assert_called_once()

    def test_location_not_offered_is_refused(self):
        with self.assertRaises(MentoringError) as ctx:
            self.call(location_type='mlai_space')
        self.assertEqual(ctx.exception.code, 'location_unavailable')


class ProfileValidationTests(unittest.TestCase):
    cur = {'location_type': 'video', 'video_link': 'https://meet.example/x'}

    def bad(self, payload, current=None):
        with self.assertRaises(MentoringError):
            service.validate_profile_update(payload, current or self.cur)

    def test_length_bounds_and_price(self):
        self.bad({'session_minutes': 10})
        self.bad({'session_minutes': 125})
        self.bad({'session_minutes': 33})
        self.bad({'price_roo': 0})
        self.bad({'price_roo': True})
        self.assertEqual(service.validate_profile_update({'session_minutes': 45, 'price_roo': 60}, self.cur),
                         {'session_minutes': 45, 'price_roo': 60})

    def test_video_needs_own_https_link(self):
        self.bad({'video_link': 'http://insecure.example'})
        self.bad({'location_type': 'either'}, {'location_type': 'video', 'video_link': ''})
        self.assertEqual(service.validate_profile_update({'location_type': 'mlai_space'},
                         {'location_type': 'video', 'video_link': ''}), {'location_type': 'mlai_space'})

    def test_timezone_and_topics(self):
        self.bad({'timezone': 'Mars/Base'})
        self.bad({'topics': ['x'] * 11})

    def test_is_active_cannot_be_set_by_a_mentor(self):
        self.assertEqual(service.validate_profile_update({'is_active': True, 'user': 5}, self.cur), {})

    def test_away_dates_validation(self):
        got = service.validate_away_dates([
            {'from': '2026-12-20', 'to': '2027-01-05'}, {'from': '2026-11-10', 'to': '2026-11-10'}])
        self.assertEqual(got[0], {'from': '2026-11-10', 'to': '2026-11-10'})
        for bad in ('x', [{'from': '2026-12-20'}], [{'from': '2026-12-20', 'to': '2026-12-01'}],
                    [{'from': 'soon', 'to': 'later'}], [{'from': '2026-01-01', 'to': '2028-01-01'}],
                    [{'from': '2026-01-01', 'to': '2026-01-02'}] * 21):
            with self.subTest(bad=bad), self.assertRaises(MentoringError):
                service.validate_away_dates(bad)

    def test_away_dates_flow_through_profile_update_and_slot_rules(self):
        clean = service.validate_profile_update(
            {'away_dates': [{'from': '2026-11-02', 'to': '2026-11-03'}]}, self.cur)
        self.assertEqual(clean['away_dates'], [{'from': '2026-11-02', 'to': '2026-11-03'}])
        from datetime import date
        profile = SimpleNamespace(away_dates=clean['away_dates'] + [{'from': 'junk'}])
        self.assertEqual(service._away_ranges(profile), [(date(2026, 11, 2), date(2026, 11, 3))])

    def test_availability_validation(self):
        ok = service.validate_availability([{'weekday': 1, 'start': '09:00', 'end': '11:00'}])
        self.assertEqual(len(ok), 1)
        for bad in ([{'weekday': 7, 'start': '09:00', 'end': '10:00'}],
                    [{'weekday': 1, 'start': '10:00', 'end': '09:00'}],
                    [{'weekday': 1, 'start': '9am', 'end': '10:00'}],
                    [{'weekday': 1, 'start': '09:00', 'end': '11:00'}, {'weekday': 1, 'start': '10:00', 'end': '12:00'}],
                    'monday'):
            with self.subTest(bad=bad), self.assertRaises(MentoringError):
                service.validate_availability(bad)


class SerializationTests(unittest.TestCase):
    def test_video_link_hidden_from_founder_until_accepted(self):
        s = make_session('requested', location_detail='https://meet.example/mia', location_type='video',
                         brief_stage='a', brief_struggle='b', brief_outcome='c', brief_link='',
                         requested_at=NOW, mentor_id=7)
        founder = SimpleNamespace(pk=1)
        self.assertEqual(service.serialize_session(s, founder)['location_detail'], '')
        s.status = 'accepted'
        self.assertEqual(service.serialize_session(s, founder)['location_detail'], 'https://meet.example/mia')
        s.status = 'requested'
        self.assertEqual(service.serialize_session(s, SimpleNamespace(pk=2))['location_detail'], 'https://meet.example/mia')

    def test_no_contact_details_leak(self):
        s = make_session('accepted', location_detail='', location_type='video', brief_stage='a',
                         brief_struggle='b', brief_outcome='c', brief_link='', requested_at=NOW)
        text = str(service.serialize_session(s, SimpleNamespace(pk=1)))
        self.assertNotIn('UMENTOR', text)
        self.assertNotIn('UFOUNDER', text)


if __name__ == '__main__':
    unittest.main()
