"""Mentor invite emails: creating, previewing, sending again and rendering. No database, no network."""
from datetime import datetime, timedelta, timezone as tz
from types import SimpleNamespace
import logging
import unittest
import uuid
from unittest.mock import MagicMock, patch

from django.test import SimpleTestCase, override_settings

from community_chat import mentoring_admin_redaction as redacted
from mentoring import admin_service as svc, invite_email
from mentoring.admin_service import MentoringAdminService, serialize_invite
from mentoring.models import mask_email, is_placeholder_email
from mentoring.service import MentoringError
from system_email import api as system_email_api, transport_resend
from system_email.registry import get_spec
from system_email.render import RenderedEmail, render
from system_email.transport_resend import SystemEmailRejected, SystemEmailRetryable

NOW = datetime(2026, 11, 16, 2, 0, tzinfo=tz.utc)
TOKEN = 'T' * 43
STAFF = SimpleNamespace(pk=1, email='sam@mlai.au', full_name='Sam Donegan', first_name='Sam')
LOG_ID = uuid.UUID('11111111-1111-1111-1111-111111111111')


def member(pk=5, name='Sonia Kaurah', email='sonia@talathrive.com', **kw):
    return SimpleNamespace(pk=pk, full_name=name, first_name=name.split()[0], email=email,
                           is_active=True, avatar_url=None, **kw)


def an_invite(**kw):
    base = dict(pk=3, user=member(), user_id=5, email='sonia@talathrive.com', created_by=STAFF, revoked_by=None,
                created_at=NOW, expires_at=NOW + timedelta(days=14), used_at=None, revoked_at=None,
                emailed_at=None, email_log_id=None, send_count=0, save=MagicMock(), token_hash='h')
    base.update(kw)
    return SimpleNamespace(**base)


class Fixture(unittest.TestCase):
    def setUp(self):
        self.users = MagicMock()
        self.users.objects.filter.return_value.first.return_value = member()
        self.users.objects.resolve_email.return_value = None
        self.profiles = MagicMock()
        self.profiles.objects.filter.return_value.exists.return_value = False
        self.invites = MagicMock()
        self.issued = an_invite()
        self.invites.issue.return_value = (self.issued, TOKEN)
        self.open = MagicMock()
        for p in (
            patch.object(svc, 'transaction', MagicMock()),
            patch.object(svc.timezone, 'now', return_value=NOW),
            patch.object(svc, 'get_user_model', return_value=self.users),
            patch.object(svc, 'MentorProfile', self.profiles),
            patch.object(svc, 'MentorInvite', self.invites),
            patch.object(svc, '_open_invites_for', self.open),
            patch.object(svc, 'invite_link', side_effect=lambda t: f'https://chat.test/mentoring/apply?mentor_invite={t}'),
            patch.object(invite_email, 'available', return_value=True),
        ):
            p.start()
            self.addCleanup(p.stop)

    def code(self, fn, *a, **kw):
        with self.assertRaises(MentoringError) as ctx:
            fn(*a, **kw)
        return ctx.exception.code


class CreateTests(Fixture):
    def test_sent_email_returns_no_link_and_records_the_send(self):
        with patch.object(invite_email, 'send', return_value={'status': 'sent', 'to': 'sonia@talathrive.com',
                                                              'log_id': LOG_ID}) as send:
            data = MentoringAdminService.create_invite(STAFF, user_id=5, send_email=True, note='  Hi  Sonia ')
        self.assertNotIn('link', data)
        self.assertEqual(data['email'], {'status': 'sent', 'to': 'sonia@talathrive.com'})
        self.assertEqual(data['email_status'], 'sent')
        self.assertEqual(send.call_args.kwargs['note'], 'Hi Sonia')
        self.assertIs(send.call_args.kwargs['inviter'], STAFF)
        self.assertEqual((self.issued.send_count, self.issued.email_log_id, self.issued.emailed_at), (1, LOG_ID, NOW))
        self.issued.save.assert_called_once_with(update_fields=['emailed_at', 'email_log_id', 'send_count'])

    def test_a_new_invite_revokes_the_persons_earlier_unused_ones(self):
        with patch.object(invite_email, 'send', return_value={'status': 'sent', 'to': 'x', 'log_id': LOG_ID}):
            MentoringAdminService.create_invite(STAFF, user_id=5, send_email=True)
        self.open.assert_called_once()
        self.open.return_value.update.assert_called_once_with(revoked_at=NOW, revoked_by=STAFF)

    def test_failed_or_suppressed_email_falls_back_to_the_link(self):
        for status, code in (('failed', 'resend_500'), ('suppressed', 'recipient_suppressed')):
            with patch.object(invite_email, 'send', return_value={'status': status, 'to': 'x', 'code': code}):
                data = MentoringAdminService.create_invite(STAFF, user_id=5, send_email=True)
            self.assertEqual(data['email']['status'], status)
            self.assertEqual(data['link'], f'https://chat.test/mentoring/apply?mentor_invite={TOKEN}')

    def test_without_send_email_or_with_the_switch_off_it_is_the_old_link_flow(self):
        with patch.object(invite_email, 'send') as send:
            data = MentoringAdminService.create_invite(STAFF, user_id=5)
            self.assertIn('link', data)
            invite_email.available.return_value = False
            data = MentoringAdminService.create_invite(STAFF, user_id=5, send_email=True)
            self.assertIn('link', data)
        send.assert_not_called()
        self.assertEqual(data['email'], {'status': 'not_sent'})

    def test_invite_by_email_for_someone_without_an_account(self):
        with patch.object(invite_email, 'send', return_value={'status': 'sent', 'to': 'x', 'log_id': LOG_ID}):
            MentoringAdminService.create_invite(STAFF, email=' New.Person@Example.com ', send_email=True)
        self.assertEqual(self.invites.issue.call_args.args, (None,))
        self.assertEqual(self.invites.issue.call_args.kwargs['email'], 'new.person@example.com')

    def test_invite_by_email_uses_the_existing_account_for_that_address(self):
        owner = member(8)
        self.users.objects.resolve_email.return_value = owner
        MentoringAdminService.create_invite(STAFF, email='sonia@talathrive.com')
        self.assertIs(self.invites.issue.call_args.args[0], owner)

    def test_placeholders_bad_addresses_and_mentors_are_refused(self):
        self.users.objects.filter.return_value.first.return_value = member(email='u123@slack.placeholder.com')
        self.assertEqual(self.code(MentoringAdminService.create_invite, STAFF, user_id=5), 'no_real_email')
        for bad in ('u1@slack.placeholder.com', 'nope', '', 7):
            self.assertEqual(self.code(MentoringAdminService.create_invite, STAFF, email=bad), 'invalid_email', bad)
        self.assertEqual(self.code(MentoringAdminService.create_invite, STAFF), 'invalid_user')
        self.users.objects.filter.return_value.first.return_value = member()
        self.profiles.objects.filter.return_value.exists.return_value = True
        self.assertEqual(self.code(MentoringAdminService.create_invite, STAFF, user_id=5), 'already_a_mentor')
        self.invites.issue.assert_not_called()

    def test_note_is_plain_text_up_to_600_characters(self):
        self.assertEqual(self.code(MentoringAdminService.create_invite, STAFF, user_id=5, note='x' * 601), 'invalid_note')
        self.assertEqual(self.code(MentoringAdminService.create_invite, STAFF, user_id=5, note=['x']), 'invalid_note')
        self.assertEqual(invite_email.clean_note('a\r\n\r\n\r\n\r\nb   c'), 'a\n\nb c')
        self.assertEqual(invite_email.clean_note(None), '')


class ResendTests(Fixture):
    def setUp(self):
        super().setUp()
        self.existing = an_invite(send_count=1, email_log_id=LOG_ID, rotate=MagicMock(return_value='NEW' * 15))
        self.invites.objects.select_for_update.return_value.select_related.return_value \
            .filter.return_value.first.return_value = self.existing

    def test_resend_rotates_the_token_and_numbers_the_send(self):
        with patch.object(invite_email, 'send', return_value={'status': 'sent', 'to': 'x', 'log_id': LOG_ID}) as send:
            data = MentoringAdminService.resend_invite(STAFF, 3, note='Following up')
        self.existing.rotate.assert_called_once_with()
        self.existing.save.assert_any_call(update_fields=['token_hash', 'expires_at'])
        self.assertEqual(send.call_args.kwargs['token'], 'NEW' * 15)
        self.assertEqual(self.existing.send_count, 2)
        self.assertNotIn('link', data)

    def test_used_revoked_and_missing_invites_cannot_be_resent(self):
        self.existing.used_at = NOW
        self.assertEqual(self.code(MentoringAdminService.resend_invite, STAFF, 3), 'invite_used')
        self.existing.used_at, self.existing.revoked_at = None, NOW
        self.assertEqual(self.code(MentoringAdminService.resend_invite, STAFF, 3), 'invite_revoked')
        self.invites.objects.select_for_update.return_value.select_related.return_value \
            .filter.return_value.first.return_value = None
        self.assertEqual(self.code(MentoringAdminService.resend_invite, STAFF, 3), 'invite_not_found')
        invite_email.available.return_value = False
        self.assertEqual(self.code(MentoringAdminService.resend_invite, STAFF, 3), 'invite_email_off')
        self.existing.rotate.assert_not_called()


class PreviewTests(Fixture):
    def test_preview_creates_nothing_and_uses_a_dummy_token(self):
        with patch.object(invite_email, 'preview', return_value={'subject': 'S'}) as preview:
            self.assertEqual(MentoringAdminService.preview_invite(STAFF, user_id=5, note='Hi'), {'subject': 'S'})
        self.invites.issue.assert_not_called()
        self.assertEqual(preview.call_args.kwargs['invited_email'], 'sonia@talathrive.com')
        self.assertEqual(preview.call_args.kwargs['note'], 'Hi')

    def test_preview_needs_the_switch(self):
        invite_email.available.return_value = False
        self.assertEqual(self.code(MentoringAdminService.preview_invite, STAFF, user_id=5), 'invite_email_off')


class SerializeTests(unittest.TestCase):
    def test_email_only_invite_shows_the_address_as_the_person(self):
        data = serialize_invite(an_invite(user=None, user_id=None, email='new@example.com'), NOW)
        self.assertEqual(data['user'], {'id': None, 'name': 'new@example.com', 'email': 'new@example.com'})
        self.assertFalse(data['has_account'])
        self.assertEqual(data['email_status'], 'none')

    def test_email_status_comes_from_the_delivery_log(self):
        row = an_invite(email_log_id=LOG_ID, send_count=1, emailed_at=NOW)
        self.assertEqual(serialize_invite(row, NOW, {LOG_ID: 'delivered'})['email_status'], 'delivered')
        self.assertEqual(serialize_invite(row, NOW, {LOG_ID: 'sending'})['email_status'], 'failed')

    def test_read_only_staff_never_see_the_invited_address(self):
        data = redacted.invite(serialize_invite(an_invite(user=None, user_id=None, email='new@example.com'), NOW))
        self.assertNotIn('new@example.com', str(data))
        self.assertIn('email_status', data)


class HelperTests(unittest.TestCase):
    def test_mask_and_placeholder(self):
        self.assertEqual(mask_email('sonia@talathrive.com'), 's•••@talathrive.com')
        self.assertEqual(mask_email('bad'), '')
        self.assertTrue(is_placeholder_email('U1@Slack.Placeholder.com'))
        self.assertFalse(is_placeholder_email('sonia@talathrive.com'))


@override_settings(SYSTEM_EMAIL_RESEND_API_KEY='k', MLAI_SYSTEM_FROM_EMAIL='MLAI <n@notify.test>',
                   SYSTEM_EMAIL_REPLY_TO='team@mlai.au', COMMUNITY_CHAT_FRONTEND_URL='https://chat.test',
                   CRM_EMAIL_ASSET_BASE_URL='https://api.test/static/crm/email/',
                   MENTOR_INVITE_EXPLAINER_URL='https://mlai.au/mentors')
class EmailTests(SimpleTestCase):
    def context(self, **kw):
        values = dict(invited_user=member(), invited_email='sonia@talathrive.com', inviter=STAFF, token=TOKEN,
                      expires_at=datetime(2026, 11, 30, 2, tzinfo=tz.utc), note='')
        values.update(kw)
        return invite_email.context(**values)

    def test_renders_both_buttons_and_the_facts(self):
        out = render(get_spec('mentor_invite'), self.context())
        self.assertEqual(out.subject, 'Sam invited you to mentor at MLAI')
        self.assertIn(f'href="https://chat.test/mentoring/apply?mentor_invite={TOKEN}"', out.html)
        self.assertIn('href="https://mlai.au/mentors"', out.html)
        self.assertIn('Accept Invite', out.html)
        self.assertIn('Hi Sonia,', out.html)
        self.assertIn('works until 30 November 2026', out.html)
        self.assertIn('https://api.test/static/crm/email/clock.png', out.html)
        self.assertIn('Suite 350, 585 Little Collins St', out.html)
        self.assertIn(f'mentor_invite={TOKEN}', out.text)
        self.assertIn('https://mlai.au/mentors', out.text)
        self.assertIn("Sam Donegan would love you to join MLAI's mentors", out.text)

    def test_note_and_names_are_escaped_in_html_and_plain_in_text(self):
        out = render(get_spec('mentor_invite'), self.context(
            note='<script>x</script> {{ accept_url }}\nsecond line',
            inviter=SimpleNamespace(full_name='O\'Brien <b>', first_name='O\'Brien', email='o@mlai.au')))
        self.assertNotIn('<script>', out.html)
        self.assertIn('&lt;script&gt;', out.html)
        self.assertIn('{{ accept_url }}', out.text)
        self.assertIn('<br>second line', out.html)
        self.assertNotIn('<b>', out.html.split('<body')[1].split('Mentor MLAI founders')[0])

    def test_someone_without_an_account_is_greeted_as_there(self):
        out = render(get_spec('mentor_invite'), self.context(invited_user=None))
        self.assertIn('Hi there,', out.text)

    def test_send_replies_to_the_inviter_and_keys_per_send(self):
        invite = an_invite(send_count=2)
        with patch.object(system_email_api, 'send', return_value=SimpleNamespace(log_id=str(LOG_ID))) as send:
            result = invite_email.send(invite, token=TOKEN, inviter=STAFF, note='')
        self.assertEqual(result['status'], 'sent')
        self.assertEqual(send.call_args.kwargs['reply_to'], 'sam@mlai.au')
        self.assertEqual(send.call_args.kwargs['idempotency_key'], 'mentor-invite/3/3')
        self.assertEqual(send.call_args.kwargs['to'], 'sonia@talathrive.com')

    def test_provider_errors_never_raise(self):
        for exc, status in ((SystemEmailRejected('recipient_suppressed'), 'suppressed'),
                            (SystemEmailRetryable('resend_503'), 'failed')):
            with patch.object(system_email_api, 'send', side_effect=exc):
                self.assertEqual(invite_email.send(an_invite(), token=TOKEN, inviter=STAFF)['status'], status)

    def test_preview_has_no_real_token(self):
        out = invite_email.preview(invited_user=member(), invited_email='sonia@talathrive.com', inviter=STAFF,
                                   expires_at=NOW, note='')
        self.assertIn('mentor_invite=preview', out['html'])
        self.assertEqual(out['reply_to'], 'sam@mlai.au')
        self.assertEqual(out['to'], 'sonia@talathrive.com')

    def test_reply_to_override_reaches_the_payload_and_bad_values_fall_back(self):
        rendered = RenderedEmail('S', '<p>h</p>', 't')
        payload = transport_resend.build_payload(to='a@b.co', rendered=rendered, message_key='mentor_invite',
                                                 entity_ref='1', reply_to='sam@mlai.au')
        self.assertEqual(payload['reply_to'], 'sam@mlai.au')
        payload = transport_resend.build_payload(to='a@b.co', rendered=rendered, message_key='mentor_invite',
                                                 entity_ref='1', reply_to=system_email_api._reply_to('x@y.z\r\nBcc: e@f.g'))
        self.assertEqual(payload['reply_to'], 'team@mlai.au')
        self.assertEqual(system_email_api._reply_to('a@b.co, c@d.co'), '')

    def test_nothing_secret_is_logged(self):
        with self.assertLogs(level=logging.DEBUG) as logs, \
                patch.object(system_email_api, 'send', side_effect=SystemEmailRetryable('resend_503')):
            logging.getLogger('mentoring').debug('marker')
            invite_email.send(an_invite(), token=TOKEN, inviter=STAFF, note='private note')
        joined = '\n'.join(logs.output)
        self.assertNotIn(TOKEN, joined)
        self.assertNotIn('private note', joined)


if __name__ == '__main__':
    unittest.main()
