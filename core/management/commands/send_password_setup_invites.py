from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError

from core.password_auth import issue_password_reset


class Command(BaseCommand):
    help = 'Send password-setup links to eligible active passwordless MLAI users.'

    def add_arguments(self, parser):
        parser.add_argument('--dry-run', action='store_true')
        parser.add_argument('--send', action='store_true')
        parser.add_argument('--batch-size', type=int, default=100)

    def handle(self, *args, **options):
        if options['dry_run'] == options['send']:
            raise CommandError('Choose exactly one of --dry-run or --send.')
        batch_size = options['batch_size']
        if batch_size < 1 or batch_size > 1000:
            raise CommandError('--batch-size must be between 1 and 1000.')

        User = get_user_model()
        candidates = (
            User.objects.filter(is_active=True, password__startswith='!')
            .exclude(email__iendswith='@slack.placeholder.com')
            .order_by('id')[:batch_size]
        )
        candidate_ids = list(candidates.values_list('id', flat=True))
        if options['dry_run']:
            self.stdout.write(
                self.style.SUCCESS(
                    f'Dry run: {len(candidate_ids)} eligible accounts would receive setup links.'
                )
            )
            return

        sent = 0
        for user in User.objects.filter(id__in=candidate_ids).iterator():
            if issue_password_reset(user.email):
                sent += 1
        self.stdout.write(self.style.SUCCESS(f'Queued password setup for {sent} accounts.'))
