"""Post-session feedback: tokens, timing, email, Slack, the public page and rating rules.

ORM, Slack and Resend are mocked: no database, no network."""
from datetime import datetime, timedelta, timezone as tz
from types import SimpleNamespace
import unittest
import uuid
from unittest.mock import MagicMock, patch

from django.test import RequestFactory, SimpleTestCase, override_settings

from community_chat import mentoring_admin_redaction as redacted
from mentoring import feedback, feedback_views
from mentoring.models import MentorSession, MentorSessionFeedback
from system_email.registry import get_spec
from system_email.render import render
from system_email.transport_resend import SystemEmailRejected, SystemEmailRetryable

NOW = datetime(2026, 11, 19, 3, 35, tzinfo=tz.utc)  # 2:35 pm Melbourne
FID = uuid.UUID('22222222-2222-2222-2222-222222222222')
SETTINGS = dict(
    DEFAULT_BACKEND_URL='https://api.test', CRM_EMAIL_ASSET_BASE_URL='https://api.test/static/crm/email',
    COMMUNITY_CHAT_FRONTEND_URL='https://chat.test', SECRET_KEY='unit-test-secret',
    MENTOR_FEEDBACK_ENABLED=True, MENTOR_FEEDBACK_NOT_BEFORE='2026-11-01T00:00:00Z',
)


def user(pk, first, last, email='', slack_id=''):
    return SimpleNamespace(pk=pk, first_name=first, full_name=f'{first} {last}', email=email, slack_id=slack_id)


def a_session(**kw):
    base = dict(
        pk=uuid.UUID('33333333-3333-3333-3333-333333333333'),
        starts_at=datetime(2026, 11, 19, 3, 0, tzinfo=tz.utc), ends_at=datetime(2026, 11, 19, 3, 30, tzinfo=tz.utc),
        status=MentorSession.STATUS_ACCEPTED, location_type='video',
        mentor=SimpleNamespace(user=user(7, 'Priya', 'Raman')), founder=user(5, 'Alex', 'Chen'),
        brief_answers=[{'id': 'q1', 'question': 'Topic?', 'answer': 'Pricing your first paid plan'}],
        brief_stage='', brief_struggle='',
    )
    base.update(kw)
    return SimpleNamespace(**base)


def a_row(**kw):
    session = kw.pop('session', a_session())
    base = dict(
        pk=FID, session=session, session_id=session.pk,
        respondent=user(5, 'Alex', 'Chen', 'alex@example.com', 'U123'), respondent_id=5,
        token_expires_at=NOW + timedelta(days=30), rating='', rated_at=None, rated_via='', rating_history=[],
        comment='', commented_at=None, suspect_scanner=False, sent_at=None,
        email_status='pending', email_log_id=None, email_error='',
        slack_status='pending', slack_channel='', slack_ts='', alert_ts='', save=MagicMock(),
    )
    base.update(kw)
    return SimpleNamespace(**base)


@override_settings(**SETTINGS)
class TokenTests(SimpleTestCase):
    def test_round_trip(self):
        token = feedback.make_token(FID)
        self.assertEqual(feedback.parse_token(token), FID)
        self.assertNotIn('=', token)

    def test_tampered_or_junk_tokens_are_refused(self):
        token = feedback.make_token(FID)
        flipped = token[:-2] + ('A' if token[-2] != 'A' else 'B') + token[-1]
        for bad in (flipped, '', 'x' * 200, 'not-base64!!', feedback.make_token(FID)[:20], None):
            self.assertIsNone(feedback.parse_token(bad))

    def test_token_depends_on_the_secret(self):
        token = feedback.make_token(FID)
        with override_settings(SECRET_KEY='another'):
            self.assertIsNone(feedback.parse_token(token))

    def test_links(self):
        token = feedback.make_token(FID)
        self.assertEqual(feedback.page_url(token, 'happy', 'email'),
                         f'https://api.test/api/v1/mentoring/feedback/{token}/?r=happy&src=email')
        self.assertEqual(feedback.unsubscribe_url(token),
                         f'https://api.test/api/v1/mentoring/feedback/{token}/unsubscribe/')


class FormattingTests(SimpleTestCase):
    def test_melbourne_times(self):
        session = a_session()
        self.assertEqual(feedback.long_date(session), 'Thursday 19 November')
        self.assertEqual(feedback.short_date(session), 'Thu 19 Nov')
        self.assertEqual(feedback.time_range(session), '2:00 to 2:30 pm')

    def test_range_across_noon(self):
        session = a_session(starts_at=datetime(2026, 11, 19, 0, 30, tzinfo=tz.utc),
                            ends_at=datetime(2026, 11, 19, 1, 30, tzinfo=tz.utc))
        self.assertEqual(feedback.time_range(session), '11:30 am to 12:30 pm')

    def test_brief_line_falls_back_and_truncates(self):
        self.assertEqual(feedback.brief_line(a_session()), 'Pricing your first paid plan')
        self.assertEqual(feedback.brief_line(a_session(brief_answers=[], brief_stage='Seed')), 'Seed')
        long = a_session(brief_answers=[{'answer': 'x ' * 80}])
        self.assertTrue(feedback.brief_line(long).endswith('…'))
        self.assertLessEqual(len(feedback.brief_line(long)), 60)

    def test_place(self):
        self.assertEqual(feedback.place(a_session()), 'Google Meet')
        self.assertEqual(feedback.place(a_session(location_type='mlai_space')), 'MLAI space')


@override_settings(**SETTINGS)
class EmailRenderTests(SimpleTestCase):
    def rendered(self, **kw):
        return render(get_spec(feedback.MESSAGE_KEY), feedback.email_context(a_row(**kw)))

    def test_subject_and_faces(self):
        email = self.rendered()
        self.assertEqual(email.subject, 'How was your session with Priya?')
        token = feedback.make_token(FID)
        for rating in ('sad', 'neutral', 'happy', 'no_show'):
            self.assertIn(f'{token}/?r={rating}&amp;src=email', email.html)
            self.assertIn(f'{token}/?r={rating}&src=email', email.text)
        for face in ('face-sad.png', 'face-neutral.png', 'face-happy.png'):
            self.assertIn(f'https://api.test/static/crm/email/{face}', email.html)
        for label in ('Not great', 'Okay', 'Great'):
            self.assertIn(f'alt="{label}"', email.html)

    def test_copy_footer_and_facts(self):
        email = self.rendered()
        for text in (email.html, email.text):
            self.assertIn('Suite 350, 585 Little Collins St, Melbourne VIC 3000', text)
            self.assertIn('Hi Alex,', text)
        self.assertIn('Priya Raman?', email.html)
        self.assertIn('Thursday 19 November', email.html)
        self.assertIn('2:00 to 2:30 pm, Melbourne time', email.html)
        self.assertIn('Pricing your first paid plan', email.html)
        self.assertIn('video-call.png', email.html)
        self.assertIn('Stop session feedback emails', email.html)

    def test_names_are_escaped_in_html_not_subject(self):
        session = a_session(mentor=SimpleNamespace(user=user(7, "Sinéad", "O'Brien <b>")))
        email = self.rendered(session=session)
        self.assertEqual(email.subject, 'How was your session with Sinéad?')
        self.assertNotIn('<b>', email.html)

    def test_spec_is_optional_mail(self):
        spec = get_spec(feedback.MESSAGE_KEY)
        self.assertTrue(spec.unsubscribe)
        self.assertEqual(spec.category, 'notification')


@override_settings(**SETTINGS)
class SendEmailTests(SimpleTestCase):
    def setUp(self):
        self.set = patch.object(feedback, '_set', side_effect=lambda row, **f: [setattr(row, k, v) for k, v in f.items()])
        self.set.start()
        self.addCleanup(self.set.stop)
        self.sent = patch.object(feedback, '_mark_sent')
        self.sent.start()
        self.addCleanup(self.sent.stop)
        self.optout = patch.object(feedback, 'MentorFeedbackOptOut')
        self.optout_model = self.optout.start()
        self.optout_model.objects.filter.return_value.exists.return_value = False
        self.addCleanup(self.optout.stop)

    def test_sends_once_per_row_with_unsubscribe(self):
        row = a_row()
        with patch('system_email.api.send', return_value=SimpleNamespace(log_id=str(FID))) as send:
            self.assertEqual(feedback.send_email(row, now=NOW), 'sent')
        kwargs = send.call_args.kwargs
        self.assertEqual(kwargs['idempotency_key'], f'mentor-feedback/{FID}/email')
        self.assertEqual(kwargs['to'], 'alex@example.com')
        self.assertTrue(kwargs['unsubscribe_url'].endswith('/unsubscribe/'))
        self.assertEqual(row.email_log_id, FID)

    def test_no_address_or_placeholder_is_skipped(self):
        for email in ('', 'U1@slack.placeholder.com'):
            row = a_row(respondent=user(5, 'Alex', 'Chen', email, 'U1'))
            with patch('system_email.api.send') as send:
                self.assertEqual(feedback.send_email(row, now=NOW), 'skipped')
            send.assert_not_called()

    def test_opted_out(self):
        self.optout_model.objects.filter.return_value.exists.return_value = True
        row = a_row()
        with patch('system_email.api.send') as send:
            self.assertEqual(feedback.send_email(row, now=NOW), 'opted_out')
        send.assert_not_called()

    def test_retryable_stays_pending_and_rejection_fails(self):
        row = a_row()
        with patch('system_email.api.send', side_effect=SystemEmailRetryable('provider_timeout')):
            self.assertEqual(feedback.send_email(row, now=NOW), 'pending')
        with patch('system_email.api.send', side_effect=SystemEmailRejected('recipient_suppressed')):
            self.assertEqual(feedback.send_email(row, now=NOW), 'suppressed')


@override_settings(**SETTINGS)
class SlackTests(SimpleTestCase):
    def setUp(self):
        self.set = patch.object(feedback, '_set', side_effect=lambda row, **f: [setattr(row, k, v) for k, v in f.items()])
        self.set.start()
        self.addCleanup(self.set.stop)
        patcher = patch.object(feedback, '_mark_sent')
        patcher.start()
        self.addCleanup(patcher.stop)
        self.objects = MagicMock()
        self.objects.filter.return_value.update.return_value = 1
        patcher = patch.object(MentorSessionFeedback, 'objects', self.objects)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.slack = MagicMock()
        self.slack.open_dm_channel.return_value = 'D1'
        self.slack.send_message.return_value = (True, '1700000000.0001')
        patcher = patch.object(feedback, '_slack', return_value=self.slack)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_ask_blocks_have_three_url_buttons(self):
        _text, blocks = feedback.ask_blocks(a_row())
        buttons = blocks[2]['elements']
        self.assertEqual([b['value'] for b in buttons], ['sad', 'neutral', 'happy'])
        for button in buttons:
            self.assertIn('src=slack', button['url'])
            self.assertTrue(button['action_id'].startswith('mentor_feedback_link_'))
        self.assertIn('*Priya Raman*', blocks[0]['text']['text'])
        self.assertIn('Thu 19 Nov, 2:00 to 2:30 pm', blocks[1]['elements'][0]['text'])

    def test_sends_dm_and_records_channel_and_ts(self):
        row = a_row()
        self.assertEqual(feedback.send_slack(row, now=NOW), 'sent')
        self.assertEqual((row.slack_channel, row.slack_ts), ('D1', '1700000000.0001'))

    def test_no_slack_id_is_skipped(self):
        row = a_row(respondent=user(5, 'Alex', 'Chen', 'alex@example.com', ''))
        self.assertEqual(feedback.send_slack(row, now=NOW), 'skipped')
        self.slack.send_message.assert_not_called()

    def test_lost_claim_never_posts_twice(self):
        self.objects.filter.return_value.update.return_value = 0
        feedback.send_slack(a_row(), now=NOW)
        self.slack.send_message.assert_not_called()

    def test_thanks_blocks(self):
        text, blocks = feedback.thanks_blocks(a_row(rating='happy'))
        self.assertIn('Great', text)
        self.assertIn('Add a comment', blocks[1]['elements'][0]['text'])
        _t, blocks = feedback.thanks_blocks(a_row(rating='no_show'))
        self.assertIn('didn’t happen', blocks[0]['text']['text'])

    @override_settings(MENTOR_FEEDBACK_ALERT_CHANNEL='C9')
    def test_negative_answer_alerts_once_then_threads(self):
        row = a_row(rating='sad', comment='Mentor was late')
        self.objects.select_related.return_value.get.return_value = row
        self.assertTrue(feedback.alert(FID))
        self.assertEqual(self.slack.send_message.call_args.args[0], 'C9')
        self.assertIn('*Not great*', self.slack.send_message.call_args.args[1])
        row.alert_ts = '1.1'
        feedback.alert(FID, comment_added=True)
        self.assertEqual(self.slack.send_message.call_args.kwargs['thread_ts'], '1.1')

    @override_settings(MENTOR_FEEDBACK_ALERT_CHANNEL='C9')
    def test_happy_or_scanner_never_alerts(self):
        for row in (a_row(rating='happy'), a_row(rating='sad', suspect_scanner=True)):
            self.objects.select_related.return_value.get.return_value = row
            self.assertFalse(feedback.alert(FID))
        self.slack.send_message.assert_not_called()

    def test_no_alert_channel_no_alert(self):
        self.assertFalse(feedback.alert(FID))


@override_settings(**SETTINGS)
class RunnerTests(SimpleTestCase):
    def test_off_unless_enabled_and_not_before_set(self):
        with override_settings(MENTOR_FEEDBACK_ENABLED=False):
            self.assertEqual(feedback.run_scheduler(NOW)['reason'], 'disabled')
        with override_settings(MENTOR_FEEDBACK_NOT_BEFORE=''):
            self.assertEqual(feedback.run_scheduler(NOW)['reason'], 'not_before_unset')

    def test_window_uses_delay_age_and_not_before(self):
        def window():
            sessions = MagicMock()
            with patch.object(MentorSession, 'objects', sessions), \
                    patch.object(MentorSessionFeedback, 'objects', MagicMock()), \
                    patch.object(feedback, 'Exists'), patch.object(feedback, 'OuterRef'):
                feedback.due_sessions(NOW)
            return sessions.filter.call_args.kwargs
        kwargs = window()
        self.assertEqual(kwargs['ends_at__lte'], NOW - timedelta(minutes=5))
        self.assertEqual(kwargs['ends_at__gte'], NOW - timedelta(hours=24))
        self.assertEqual(set(kwargs['status__in']), {'accepted', 'completed'})
        with override_settings(MENTOR_FEEDBACK_NOT_BEFORE='2026-11-19T03:00:00Z'):
            self.assertEqual(window()['ends_at__gte'], datetime(2026, 11, 19, 3, 0, tzinfo=tz.utc))

    def test_creates_then_delivers_and_skips_calendar_changes(self):
        moving = a_session(pk='moving')
        steady = a_session(pk='steady')
        due = MagicMock()
        due.__getitem__.return_value = [moving, steady]
        with patch.object(feedback, 'due_sessions', return_value=due), \
                patch.object(feedback, '_calendar_change_pending', side_effect=lambda s: s is moving), \
                patch.object(feedback, 'start_feedback', return_value=a_row()) as start, \
                patch.object(MentorSessionFeedback, 'objects') as objects, \
                patch.object(feedback, 'deliver') as deliver:
            objects.filter.return_value.filter.return_value.order_by.return_value \
                .values_list.return_value.__getitem__.return_value = [FID]
            result = feedback.run_scheduler(NOW)
        start.assert_called_once_with(steady, NOW)
        deliver.assert_called_once_with(FID, now=NOW)
        self.assertEqual(result, {'status': 'completed', 'created': 1, 'delivered': 1})

    def test_channel_switch(self):
        row = a_row()
        with override_settings(MENTOR_FEEDBACK_CHANNELS='slack'), \
                patch.object(MentorSessionFeedback, 'objects') as objects, \
                patch.object(feedback, '_set', side_effect=lambda r, **f: [setattr(r, k, v) for k, v in f.items()]), \
                patch.object(feedback, 'send_email') as email, patch.object(feedback, 'send_slack') as slack:
            objects.select_related.return_value.get.return_value = row
            feedback.deliver(FID, now=NOW)
        email.assert_not_called()
        slack.assert_called_once()
        self.assertEqual(row.email_status, 'skipped')


@override_settings(**SETTINGS)
class RatingTests(SimpleTestCase):
    def setUp(self):
        patcher = patch.object(feedback.transaction, 'on_commit', side_effect=lambda fn: None)
        patcher.start()
        self.addCleanup(patcher.stop)
        patcher = patch.object(feedback.transaction, 'atomic', MagicMock())
        patcher.start()
        self.addCleanup(patcher.stop)

    def locked(self, row):
        objects = MagicMock()
        patcher = patch.object(MentorSessionFeedback, 'objects', objects)
        patcher.start()
        self.addCleanup(patcher.stop)
        objects.select_for_update.return_value.select_related.return_value.filter.return_value.first.return_value = row
        return row

    def test_records_and_keeps_history(self):
        row = self.locked(a_row(sent_at=NOW - timedelta(hours=1)))
        feedback.record_rating(feedback.make_token(FID), 'happy', 'email', now=NOW)
        feedback.record_rating(feedback.make_token(FID), 'neutral', 'slack', now=NOW + timedelta(minutes=1))
        self.assertEqual((row.rating, row.rated_via), ('neutral', 'slack'))
        self.assertEqual([h['rating'] for h in row.rating_history], ['happy', 'neutral'])
        self.assertFalse(row.suspect_scanner)

    def test_bad_input(self):
        self.locked(a_row())
        with self.assertRaises(feedback.FeedbackError) as ctx:
            feedback.record_rating(feedback.make_token(FID), 'angry', now=NOW)
        self.assertEqual(ctx.exception.code, 'invalid_rating')
        with self.assertRaises(feedback.FeedbackError) as ctx:
            feedback.record_rating('junk', 'happy', now=NOW)
        self.assertEqual(ctx.exception.code, 'invalid_token')

    def test_expired_link_cannot_write(self):
        row = self.locked(a_row(token_expires_at=NOW - timedelta(seconds=1)))
        with self.assertRaises(feedback.FeedbackError) as ctx:
            feedback.record_rating(feedback.make_token(FID), 'happy', now=NOW)
        self.assertEqual(ctx.exception.code, 'expired')
        self.assertEqual(row.rating, '')

    def test_three_faces_in_seconds_after_send_is_a_scanner(self):
        row = self.locked(a_row(sent_at=NOW - timedelta(seconds=30)))
        token = feedback.make_token(FID)
        for i, rating in enumerate(('sad', 'neutral', 'happy')):
            feedback.record_rating(token, rating, 'email', now=NOW + timedelta(seconds=i))
        self.assertTrue(row.suspect_scanner)

    def test_comment_is_cleaned_and_capped(self):
        row = self.locked(a_row(rating='happy'))
        feedback.record_comment(feedback.make_token(FID), '  Great \r\n\r\n\r\n\r\nhelp  ', now=NOW)
        self.assertEqual(row.comment, 'Great\n\nhelp')
        with self.assertRaises(feedback.FeedbackError):
            feedback.record_comment(feedback.make_token(FID), 'x' * 2001, now=NOW)


@override_settings(**SETTINGS, ROOT_URLCONF='mentoring.feedback_urls')
class PageTests(SimpleTestCase):
    def setUp(self):
        self.factory = RequestFactory()
        self.token = feedback.make_token(FID)
        self.row = a_row()
        patcher = patch.object(feedback, 'load', side_effect=lambda token: self.row)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_face_link_get_never_writes_and_auto_submits(self):
        with patch.object(feedback, 'record_rating') as record:
            response = feedback_views.feedback_page(self.factory.get('/', {'r': 'happy', 'src': 'email'}), self.token)
        record.assert_not_called()
        body = response.content.decode()
        self.assertEqual(response.status_code, 200)
        self.assertIn('name="rating" value="happy"', body)
        nonce = response['Content-Security-Policy'].split("'nonce-")[1].split("'")[0]
        self.assertIn(f'<script nonce="{nonce}">', body)
        self.assertIn('Confirm: Great', body)  # the noscript fallback
        self.assertEqual(response['Cache-Control'], 'no-store')

    def test_post_records_and_redirects(self):
        with patch.object(feedback, 'record_rating') as record:
            response = feedback_views.feedback_page(
                self.factory.post('/', {'rating': 'sad', 'src': 'email'}), self.token)
        record.assert_called_once_with(self.token, 'sad', 'email')
        self.assertEqual(response.status_code, 303)

    def test_rated_page_copy(self):
        self.row.rating = 'sad'
        body = feedback_views.feedback_page(self.factory.get('/'), self.token).content.decode()
        self.assertIn('Thanks for telling us, Alex.', body)
        self.assertIn('What went wrong?', body)
        self.assertIn('not to Priya', body)
        self.assertNotIn('Book another session', body)
        self.row.rating = 'happy'
        body = feedback_views.feedback_page(self.factory.get('/'), self.token).content.decode()
        self.assertIn('Glad it was great', body)
        self.assertIn('https://chat.test/mentoring', body)

    def test_expired_and_invalid(self):
        self.row.token_expires_at = datetime(2020, 1, 1, tzinfo=tz.utc)
        response = feedback_views.feedback_page(self.factory.get('/', {'r': 'happy'}), self.token)
        self.assertEqual(response.status_code, 410)
        with patch.object(feedback, 'load', side_effect=feedback.FeedbackError('invalid_token', 'x', 404)):
            self.assertEqual(feedback_views.feedback_page(self.factory.get('/'), 'junk').status_code, 404)

    def test_unsubscribe_needs_a_post(self):
        with patch.object(feedback, 'opt_out') as opt_out:
            response = feedback_views.feedback_unsubscribe(self.factory.get('/'), self.token)
            opt_out.assert_not_called()
            self.assertIn('Stop feedback emails', response.content.decode())
            feedback_views.feedback_unsubscribe(self.factory.post('/', {'List-Unsubscribe': 'One-Click'}), self.token)
            opt_out.assert_called_once_with(self.token)


class RedactionTests(unittest.TestCase):
    def test_read_only_staff_never_see_comments(self):
        data = {'id': 's', 'status': 'completed', 'mentor': None, 'founder': None,
                'feedback': {'rating': 'sad', 'rated_at': 'x', 'comment': 'private', 'suspect_scanner': False}}
        self.assertEqual(redacted.session(data)['feedback'], {'rating': 'sad', 'rated_at': 'x', 'suspect_scanner': False})
        self.assertIsNone(redacted.session({**data, 'feedback': None})['feedback'])


if __name__ == '__main__':
    unittest.main()
