from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone

from hospital.models import HospitalCompetitionRound
from hospital.world_views import WORLD_CACHE_KEY


class Command(BaseCommand):
    help = (
        'Archive the active hospital competition round and open a fresh '
        'HealthHack round. Runs as a read-only dry run unless --execute is set.'
    )

    def add_arguments(self, parser):
        parser.add_argument(
            '--execute',
            action='store_true',
            help='Apply the archive after validating the expected record counts.',
        )
        parser.add_argument('--new-slug', default='healthhack-2026')
        parser.add_argument('--new-name', default='HealthHack 2026')
        parser.add_argument('--expected-teams', type=int)
        parser.add_argument('--expected-submissions', type=int)
        parser.add_argument('--expected-announcements', type=int)
        parser.add_argument('--archived-by-email')
        parser.add_argument(
            '--notes',
            default='Archived before the HealthHack 2026 competition round.',
        )

    def handle(self, *args, **options):
        with transaction.atomic():
            try:
                active_round = (
                    HospitalCompetitionRound.objects.select_for_update()
                    .get(status=HospitalCompetitionRound.STATUS_ACTIVE)
                )
            except HospitalCompetitionRound.DoesNotExist as exc:
                raise CommandError('No active hospital competition round exists.') from exc
            except HospitalCompetitionRound.MultipleObjectsReturned as exc:
                raise CommandError(
                    'Multiple active hospital competition rounds exist; repair the data first.'
                ) from exc

            if active_round.slug == options['new_slug']:
                self.stdout.write(
                    self.style.SUCCESS(
                        f"Round '{options['new_slug']}' is already active; nothing to do."
                    )
                )
                return

            team_count = active_round.teams.count()
            submission_count = active_round.submissions.count()
            announcement_count = active_round.announcements.count()
            team_names = list(
                active_round.teams.order_by('team_id').values_list(
                    'team_name',
                    flat=True,
                )
            )

            self.stdout.write(
                f"Active round: {active_round.name} ({active_round.slug})"
            )
            self.stdout.write(f'Teams to archive: {team_count}')
            self.stdout.write(f'Submissions to archive: {submission_count}')
            self.stdout.write(f'Announcements to archive: {announcement_count}')
            if team_names:
                self.stdout.write('Teams: ' + ', '.join(team_names))

            if not options['execute']:
                self.stdout.write(
                    self.style.WARNING(
                        'Dry run only. Re-run with --execute and both expected counts.'
                    )
                )
                return

            if (
                options['expected_teams'] is None
                or options['expected_submissions'] is None
                or options['expected_announcements'] is None
            ):
                raise CommandError(
                    '--expected-teams, --expected-submissions, and '
                    '--expected-announcements are required with --execute.'
                )
            if options['expected_teams'] != team_count:
                raise CommandError(
                    f"Expected {options['expected_teams']} teams but found {team_count}; no changes made."
                )
            if options['expected_submissions'] != submission_count:
                raise CommandError(
                    f"Expected {options['expected_submissions']} submissions but found "
                    f'{submission_count}; no changes made.'
                )
            if options['expected_announcements'] != announcement_count:
                raise CommandError(
                    f"Expected {options['expected_announcements']} announcements but found "
                    f'{announcement_count}; no changes made.'
                )
            if HospitalCompetitionRound.objects.filter(slug=options['new_slug']).exists():
                raise CommandError(
                    f"A round with slug '{options['new_slug']}' already exists; no changes made."
                )

            archived_by = None
            if options['archived_by_email']:
                User = get_user_model()
                try:
                    archived_by = User._default_manager.get(
                        email__iexact=options['archived_by_email'],
                    )
                except User.DoesNotExist as exc:
                    raise CommandError(
                        f"No user found for '{options['archived_by_email']}'; no changes made."
                    ) from exc

            active_round.status = HospitalCompetitionRound.STATUS_ARCHIVED
            active_round.archived_at = timezone.now()
            active_round.archived_by = archived_by
            active_round.notes = options['notes']
            active_round.save(
                update_fields=['status', 'archived_at', 'archived_by', 'notes'],
            )

            new_round = HospitalCompetitionRound.objects.create(
                slug=options['new_slug'],
                name=options['new_name'],
                status=HospitalCompetitionRound.STATUS_ACTIVE,
            )
            transaction.on_commit(lambda: cache.delete(WORLD_CACHE_KEY))

        self.stdout.write(
            self.style.SUCCESS(
                f"Archived '{active_round.slug}' ({team_count} teams, "
                f"{submission_count} submissions, {announcement_count} announcements) "
                f"and opened '{new_round.slug}'."
            )
        )
