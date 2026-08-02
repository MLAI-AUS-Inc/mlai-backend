import time

from django.core.management.base import BaseCommand, CommandError

from core.password_delivery import process_password_reset_deliveries


class Command(BaseCommand):
    help = 'Process the durable MLAI password reset email outbox.'

    def add_arguments(self, parser):
        parser.add_argument('--once', action='store_true')
        parser.add_argument('--poll-seconds', type=float, default=2.0)
        parser.add_argument('--batch-size', type=int, default=25)

    def handle(self, *args, **options):
        poll_seconds = options['poll_seconds']
        batch_size = options['batch_size']
        if poll_seconds <= 0:
            raise CommandError('--poll-seconds must be positive.')
        if batch_size <= 0 or batch_size > 250:
            raise CommandError('--batch-size must be between 1 and 250.')

        while True:
            counts = process_password_reset_deliveries(limit=batch_size)
            if options['once']:
                self.stdout.write(
                    'Password reset outbox: '
                    + ', '.join(f'{key}={value}' for key, value in counts.items())
                )
                return
            if not any(counts.values()):
                time.sleep(poll_seconds)
