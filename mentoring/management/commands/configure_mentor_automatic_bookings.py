"""Scoped, identity-checked activation through the same validated settings service."""
import json
from django.core.management.base import BaseCommand, CommandError
from mentoring.models import MentorProfile
from mentoring.service import MentoringService, MentoringError
from mentoring.meeting_policy import REQUIRED_POLICY
from mentoring.calendars import service as calendars
from mentoring.calendars.errors import CalendarError


class Command(BaseCommand):
    help = 'Inspect one mentor; --apply opts them into automatic booking with transcription and Gemini notes.'

    def add_arguments(self, parser):
        parser.add_argument('mentor_id', type=int)
        parser.add_argument('--expected-email', required=True)
        parser.add_argument('--apply', action='store_true')

    def handle(self, *args, **options):
        try:
            profile = MentorProfile.objects.select_related('user').get(pk=options['mentor_id'])
            if profile.user.email.casefold() != options['expected_email'].casefold():
                raise CommandError('Mentor identity does not match the expected email.')
            if options['apply']:
                MentoringService.update_profile(profile.user, {'booking_revision': profile.booking_revision,
                    'accepting_bookings': True, 'meeting_artifact_policy': dict(REQUIRED_POLICY)})
                profile.refresh_from_db()
            self.stdout.write(json.dumps({'mentor_id': profile.pk, 'booking_mode': profile.booking_mode,
                'accepting_bookings': profile.accepting_bookings,
                'meeting_artifact_policy': profile.meeting_artifact_policy,
                'readiness': calendars.readiness(profile)}))
        except MentorProfile.DoesNotExist:
            raise CommandError('Mentor not found.') from None
        except (CalendarError, MentoringError) as exc:
            raise CommandError(exc.code) from None
