from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone
from django.utils.dateparse import parse_datetime

from community_chat.models import CommunityChatAccountSession


class Command(BaseCommand):
    help = 'Preview or revoke browser Chat sessions created before an approved cookie cutover.'

    def add_arguments(self, parser):
        parser.add_argument('--before', required=True, help='Explicit UTC ISO-8601 cutoff.')
        parser.add_argument('--apply', action='store_true', help='Revoke matching sessions; default is dry-run.')

    def handle(self, *args, **options):
        cutoff = parse_datetime(options['before'])
        if not cutoff or timezone.is_naive(cutoff) or cutoff.utcoffset().total_seconds() != 0:
            raise CommandError('--before must be a timezone-aware UTC timestamp.')
        if cutoff > timezone.now():
            raise CommandError('The cutoff cannot be in the future.')
        sessions = CommunityChatAccountSession.objects.filter(
            client_id='mlai-chat-web', created_at__lt=cutoff, revoked_at__isnull=True)
        count = sessions.update(revoked_at=timezone.now()) if options['apply'] else sessions.count()
        self.stdout.write(f"{'Revoked' if options['apply'] else 'Would revoke'} {count} browser sessions.")
