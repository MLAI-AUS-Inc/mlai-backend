"""Prepare a narrowly scoped repair with an explicit verified destination."""
import json
from django.core.management.base import BaseCommand, CommandError
from mentoring.models import MentorSession
from mentoring.meeting_repair import prepare, process
from mentoring.service import MentoringError
from mentoring.calendars.errors import CalendarError


class Command(BaseCommand):
    help = 'Inspect one missing-link session; --apply queues a repair without charging or moving it.'

    def add_arguments(self, parser):
        parser.add_argument('session_id')
        parser.add_argument('--connection-id', type=int)
        parser.add_argument('--calendar-id')
        parser.add_argument('--apply', action='store_true')

    def handle(self, *args, **options):
        try:
            session = MentorSession.objects.get(pk=options['session_id'])
            if options['apply']:
                if not options['connection_id'] or not options['calendar_id']:
                    raise CommandError('Supply the explicitly verified --connection-id and --calendar-id.')
                session = prepare(session.pk, connection_id=options['connection_id'], calendar_id=options['calendar_id'])
                process(session.pk)
                session.refresh_from_db()
            self.stdout.write(json.dumps({'session_id': str(session.pk), 'status': session.status,
                'booking_mode': session.booking_mode, 'meeting_status': session.meeting_status,
                'has_calendar_event': bool(session.calendar_event_id), 'has_meet_link': bool(session.meet_link),
                'has_charge': bool(session.charge_ledger_entry_id),
                'capture_verified': bool(session.meeting_provisioning.get('verified'))}))
        except (MentorSession.DoesNotExist, ValueError):
            raise CommandError('Session not found.') from None
        except (CalendarError, MentoringError) as exc:
            raise CommandError(exc.code) from None
