"""Invite-only mentor application, cancel-after-start, brief questions and the 0002
migration. ORM and transactions are mocked: no database, no network."""
from datetime import datetime, timedelta, timezone as tz
import importlib
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, patch

from django.contrib import admin

from mentoring import admin as mentoring_admin, service
from mentoring.models import MentorInvite, MentorProfile, hash_invite_token
from mentoring.service import (
    MentoringError, MentoringService, effective_brief_questions, validate_brief_questions,
)

NOW = datetime(2026, 11, 2, tzinfo=tz.utc)
TOKEN = 'T' * 43
WINDOWS = [{'weekday': 1, 'start': '09:00', 'end': '12:00'}]


def form(**kw):
    base = dict(token=TOKEN, headline='Founder coach', bio='I help.', topics=['pricing'],
                session_minutes=30, location_type='video', price_roo=40, availability=WINDOWS)
    base.update(kw)
    return base


class MigrationTests(unittest.TestCase):
    def test_0002_extends_0001_without_touching_it(self):
        mod = importlib.import_module('mentoring.migrations.0002_invites_brief_questions_followup')
        self.assertEqual(mod.Migration.dependencies[0], ('mentoring', '0001_initial'))
        ops = {(type(o).__name__, getattr(o, 'model_name', None) or getattr(o, 'name', None), getattr(o, 'name', None))
               for o in mod.Migration.operations}
        self.assertIn(('AddField', 'mentorprofile', 'brief_questions'), ops)
        self.assertIn(('AddField', 'mentorsession', 'brief_answers'), ops)
        self.assertIn(('AddField', 'mentorsession', 'meeting_claimed_at'), ops)
        self.assertIn(('CreateModel', 'MentorInvite', 'MentorInvite'), ops)
        for name in ('approved_at', 'rejected_at', 'paused_at', 'reviewed_by'):
            self.assertIn(('AddField', 'mentorprofile', name), ops)
        self.assertEqual(len(mod.Migration.operations), 8)

    def test_0001_still_has_none_of_the_followup_schema(self):
        text = (Path(__file__).resolve().parents[1] / 'migrations' / '0001_initial.py').read_text()
        for name in ('meeting_claimed_at', 'brief_answers', 'brief_questions', 'MentorInvite'):
            self.assertNotIn(name, text)

    def test_only_one_new_migration(self):
        names = sorted(p.name for p in (Path(__file__).resolve().parents[1] / 'migrations').glob('0*.py'))
        self.assertEqual(names, ['0001_initial.py', '0002_invites_brief_questions_followup.py'])


class InviteFixture(unittest.TestCase):
    def setUp(self):
        self.user = SimpleNamespace(pk=5)
        self.invite = SimpleNamespace(
            user_id=5, expires_at=NOW + timedelta(days=14), used_at=None, revoked_at=None, save=MagicMock(),
        )
        self.profile = SimpleNamespace(pk=9, is_active=False, save=MagicMock(), availability_rules=MagicMock())
        self.invites = MagicMock()
        self.invites.objects.get.return_value = self.invite
        self.invites.DoesNotExist = MentorInvite.DoesNotExist
        self.invites.objects.select_for_update.return_value = self.invites.objects
        self.profiles = MagicMock(return_value=self.profile)
        self.profiles.objects.filter.return_value.first.return_value = None
        self.profiles.objects.select_for_update.return_value.filter.return_value.first.return_value = None
        self.rules = MagicMock()
        for p in (
            patch.object(service, 'MentorInvite', self.invites),
            patch.object(service, 'MentorProfile', self.profiles),
            patch.object(service, 'MentorAvailabilityRule', self.rules),
            patch.object(service, 'transaction', MagicMock()),
            patch.object(service.timezone, 'now', return_value=NOW),
        ):
            p.start()
            self.addCleanup(p.stop)

    def code(self, fn, *a):
        with self.assertRaises(MentoringError) as ctx:
            fn(*a)
        return ctx.exception.code


class InviteLifecycleTests(InviteFixture):
    def test_token_is_stored_hashed_and_unguessable(self):
        self.assertEqual(len(hash_invite_token(TOKEN)), 64)
        self.assertNotEqual(hash_invite_token(TOKEN), TOKEN)
        created = {}
        with patch.object(MentorInvite.objects, 'create', side_effect=lambda **kw: created.update(kw) or SimpleNamespace(**kw)):
            invite, token = MentorInvite.issue(self.user)
        self.assertEqual(created['token_hash'], hash_invite_token(token))
        self.assertGreaterEqual(len(token), 40)
        self.assertNotIn(token, str(created))
        self.assertNotEqual(MentorInvite.issue.__func__, None)

    def test_default_expiry_is_14_days(self):
        created = {}
        with patch.object(MentorInvite.objects, 'create', side_effect=lambda **kw: created.update(kw) or SimpleNamespace(**kw)), \
                patch('mentoring.models.timezone.now', return_value=NOW):
            MentorInvite.issue(self.user)
        self.assertEqual(created['expires_at'], NOW + timedelta(days=14))

    def test_check_ready_for_the_invited_user(self):
        out = MentoringService.check_invite(self.user, TOKEN)
        self.assertEqual(out['state'], 'ready')
        self.assertEqual(out['default_price_roo'], 40)

    def test_unknown_token_is_invalid(self):
        self.invites.objects.get.side_effect = MentorInvite.DoesNotExist
        self.assertEqual(self.code(MentoringService.check_invite, self.user, TOKEN), 'invite_invalid')
        self.assertEqual(self.code(MentoringService.check_invite, self.user, 'short'), 'invite_invalid')
        self.assertEqual(self.code(MentoringService.check_invite, self.user, None), 'invite_invalid')

    def test_wrong_user_is_refused_on_check_and_submit(self):
        other = SimpleNamespace(pk=6)
        self.assertEqual(self.code(MentoringService.check_invite, other, TOKEN), 'invite_wrong_user')
        self.assertEqual(self.code(MentoringService.submit_application, other, form()), 'invite_wrong_user')
        self.profile.save.assert_not_called()
        self.invite.save.assert_not_called()

    def test_expired_invite_is_refused(self):
        self.invite.expires_at = NOW - timedelta(seconds=1)
        self.assertEqual(self.code(MentoringService.check_invite, self.user, TOKEN), 'invite_expired')
        self.assertEqual(self.code(MentoringService.submit_application, self.user, form()), 'invite_expired')
        self.profile.save.assert_not_called()

    def test_revoked_invite_is_refused(self):
        self.invite.revoked_at = NOW
        self.assertEqual(self.code(MentoringService.submit_application, self.user, form()), 'invite_revoked')

    def test_submit_creates_an_inactive_profile_and_uses_the_invite(self):
        out = MentoringService.submit_application(self.user, form())
        self.assertEqual(out, {'state': 'submitted'})
        self.assertFalse(self.profile.is_active)
        self.assertEqual((self.profile.headline, self.profile.price_roo, self.profile.topics),
                         ('Founder coach', 40, ['pricing']))
        self.profile.save.assert_called_once()
        self.assertEqual(self.invite.used_at, NOW)
        self.rules.objects.bulk_create.assert_called_once()

    def test_resubmitting_starts_a_fresh_review(self):
        self.profile.rejected_at = NOW
        self.profile.approved_at = NOW
        self.profile.paused_at = NOW
        self.profile.reviewed_by = object()
        self.profiles.objects.select_for_update.return_value.filter.return_value.first.return_value = self.profile
        MentoringService.submit_application(self.user, form())
        self.assertIsNone(self.profile.rejected_at)
        self.assertIsNone(self.profile.approved_at)
        self.assertIsNone(self.profile.paused_at)
        self.assertIsNone(self.profile.reviewed_by)

    def test_price_defaults_to_40(self):
        body = form()
        del body['price_roo']
        MentoringService.submit_application(self.user, body)
        self.assertEqual(self.profile.price_roo, 40)

    def test_reuse_is_refused_and_a_used_invite_shows_submitted(self):
        self.invite.used_at = NOW - timedelta(days=1)
        self.assertEqual(self.code(MentoringService.submit_application, self.user, form()), 'invite_used')
        self.assertEqual(MentoringService.check_invite(self.user, TOKEN)['state'], 'submitted')
        self.profile.is_active = True
        self.profiles.objects.filter.return_value.first.return_value = self.profile
        self.assertEqual(MentoringService.check_invite(self.user, TOKEN)['state'], 'approved')

    def test_bad_form_does_not_consume_the_invite(self):
        for body in (form(price_roo=0), form(price_roo=1001), form(headline=''), form(topics=[]),
                     form(availability=[]), form(availability=None), form(session_minutes=7),
                     form(location_type='moon'), form(bio='')):
            with self.subTest(body=body), self.assertRaises(MentoringError):
                MentoringService.submit_application(self.user, body)
        self.assertIsNone(self.invite.used_at)
        self.profile.save.assert_not_called()

    def test_applicant_cannot_self_activate_or_set_away_dates(self):
        MentoringService.submit_application(self.user, form(is_active=True, away_dates=[{'from': 'x'}]))
        self.assertFalse(self.profile.is_active)

    def test_already_active_mentor_is_refused(self):
        active = SimpleNamespace(pk=9, is_active=True, save=MagicMock())
        self.profiles.objects.select_for_update.return_value.filter.return_value.first.return_value = active
        self.assertEqual(self.code(MentoringService.submit_application, self.user, form()), 'already_a_mentor')


class AdminTests(unittest.TestCase):
    def test_staff_activate_by_ticking_is_active(self):
        model_admin = admin.site._registry[MentorProfile]
        self.assertIn('is_active', model_admin.list_editable)
        self.assertTrue(MentorProfile._meta.get_field('is_active').default is False)

    def test_invite_admin_is_registered_and_revokes(self):
        model_admin = admin.site._registry[MentorInvite]
        self.assertIn('revoke_invites', model_admin.actions)
        qs = MagicMock()
        qs.filter.return_value.update.return_value = 2
        model_admin.message_user = MagicMock()
        model_admin.revoke_invites(MagicMock(), qs)
        qs.filter.assert_called_once_with(used_at__isnull=True, revoked_at__isnull=True)

    def test_invite_link_shape(self):
        with self.settings_patch():
            link = mentoring_admin.invite_link('abc')
        self.assertTrue(link.endswith('/mentoring/apply?invite=abc'))

    def settings_patch(self):
        return patch.object(service.settings, 'COMMUNITY_CHAT_FRONTEND_URL', 'https://chat.example/', create=True)


class BriefQuestionTests(unittest.TestCase):
    def test_empty_means_the_default_four(self):
        qs = effective_brief_questions([])
        self.assertEqual([q['id'] for q in qs], ['stage', 'struggle', 'outcome', 'link'])
        self.assertEqual([q['required'] for q in qs], [True, True, True, False])

    def test_validation_limits(self):
        ok = validate_brief_questions([{'text': ' Q1 ', 'required': False}, {'id': 'keep', 'text': 'Q2'}])
        self.assertEqual(ok[0]['text'], 'Q1')
        self.assertFalse(ok[0]['required'])
        self.assertEqual(ok[1]['id'], 'keep')
        self.assertTrue(ok[1]['required'])
        self.assertEqual(validate_brief_questions([]), [])
        too_many = [{'text': f'q{i}'} for i in range(9)]
        for bad in (too_many, 'x', [{'text': ''}], [{'text': 'x' * 201}], [{'text': 'a', 'required': 'yes'}], ['a']):
            with self.subTest(bad=bad), self.assertRaises(MentoringError):
                validate_brief_questions(bad)
        self.assertEqual(len(validate_brief_questions([{'text': f'q{i}'} for i in range(8)])), 8)

    def test_duplicate_or_bad_ids_are_replaced(self):
        ok = validate_brief_questions([{'id': 'a', 'text': 'x'}, {'id': 'a', 'text': 'y'}, {'id': 'BAD ID', 'text': 'z'}])
        self.assertEqual(len({q['id'] for q in ok}), 3)

    def test_old_sessions_stay_readable(self):
        old = SimpleNamespace(brief_answers=[], brief_stage='s', brief_struggle='t', brief_outcome='o', brief_link='')
        answers = service.session_brief_answers(old)
        self.assertEqual([a['answer'] for a in answers], ['s', 't', 'o', ''])
        self.assertEqual(answers[0]['question'], 'Where is your startup at?')

    def test_legacy_dict_still_validates_for_default_questions(self):
        fields = service.validate_brief({'stage': 'a', 'struggle': 'b', 'outcome': 'c'}, [])
        self.assertEqual(fields['brief_stage'], 'a')
        with self.assertRaises(MentoringError):
            service.validate_brief({'stage': 'a', 'struggle': 'b', 'link': 'http://x'}, [])
