import logging
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction, IntegrityError
from core.models import User
from roo.models import (
    PointsAccount, Ledger, Task, TaskSubmission, 
    CoworkingBooking, RewardRedemption, PointsAdmin, BoostPostAdmission
)
from roo.services import PointsService
from integrations.services import SlackService

logger = logging.getLogger(__name__)

class Command(BaseCommand):
    help = 'Clean up duplicate users by merging Slack users into Email users'

    def add_arguments(self, parser):
        parser.add_argument(
            '--commit',
            action='store_true',
            help='Actually commit changes to the database',
        )
        parser.add_argument(
            '--source-slack-id',
            help='Merge just this user into --target-slack-id, skipping the Slack-wide sweep',
        )
        parser.add_argument(
            '--target-slack-id',
            help='The surviving user for a targeted --source-slack-id merge',
        )

    def handle(self, *args, **options):
        commit = options['commit']

        source_slack_id = options.get('source_slack_id')
        target_slack_id = options.get('target_slack_id')
        if source_slack_id or target_slack_id:
            self.merge_pair(source_slack_id, target_slack_id, commit)
            return

        self.stdout.write(f"Starting cleanup... (Commit: {commit})")

        # Get all users with Slack IDs
        slack_users = User.objects.filter(slack_id__isnull=False).exclude(slack_id='')
        
        merged_count = 0
        updated_count = 0
        skipped_count = 0
        
        for slack_user in slack_users:
            slack_id = slack_user.slack_id
            current_email = slack_user.email
            
            try:
                # Fetch profile from Slack
                profile = SlackService.get_user_profile(slack_id)
                if not profile:
                    self.stdout.write(self.style.WARNING(f"Could not fetch profile for {slack_id} (email: {current_email}). Skipping."))
                    skipped_count += 1
                    continue
                    
                real_email = profile.get('email')
                if not real_email:
                    self.stdout.write(self.style.WARNING(f"No email in Slack profile for {slack_id} (current: {current_email}). Skipping."))
                    skipped_count += 1
                    continue
                
                # Normalize email using the User manager or simple lowercasing if manager not available easily
                # The custom manager in models.py uses normalize_email
                real_email = User.objects.normalize_email(real_email)
                
                if current_email == real_email:
                    # Already correct
                    continue
                
                # Check if there is a conflict (another user with the real email)
                try:
                    target_user = User.objects.get(email=real_email)
                except User.DoesNotExist:
                    # No conflict, just update the email of the current user
                    old_email = slack_user.email
                    self.stdout.write(f"UPDATE EMAIL: SlackUser({slack_id}) {old_email} -> {real_email}")
                    if commit:
                        slack_user.email = real_email
                        slack_user.save()
                    updated_count += 1
                    continue
                
                # Conflict exists: Merge slack_user INTO target_user
                if target_user.pk == slack_user.pk:
                     continue

                self.stdout.write(self.style.SUCCESS(
                    f"MERGE REQUIRED: Source(id={slack_user.id}, email={slack_user.email}, slack={slack_id}) -> Target(id={target_user.id}, email={target_user.email}, slack={target_user.slack_id})"
                ))
                
                if commit:
                    self.merge_users(source=slack_user, target=target_user)
                
                merged_count += 1
                
            except Exception as e:
                self.stdout.write(self.style.ERROR(f"Error processing {slack_user.email}: {e}"))
                skipped_count += 1

        self.stdout.write(self.style.SUCCESS(f"Done. Updated Emails: {updated_count}, Merged Users: {merged_count}, Skipped/Error: {skipped_count}"))

    @transaction.atomic
    def merge_pair(self, source_slack_id, target_slack_id, commit):
        """Merge one known duplicate pair, without consulting the Slack API."""
        if not source_slack_id or not target_slack_id:
            raise CommandError('--source-slack-id and --target-slack-id must be given together')
        if source_slack_id == target_slack_id:
            raise CommandError('Source and target are the same user')

        try:
            source = User.objects.get(slack_id=source_slack_id)
        except User.DoesNotExist:
            raise CommandError(f'No user with slack_id {source_slack_id}')
        try:
            target = User.objects.get(slack_id=target_slack_id)
        except User.DoesNotExist:
            raise CommandError(f'No user with slack_id {target_slack_id}')

        self.stdout.write(f"Merging {source.email} ({source_slack_id}) into {target.email} ({target_slack_id})")
        if not commit:
            self.stdout.write(self.style.WARNING('Dry run - re-run with --commit to apply'))
            self.report_related(source)
            return

        with transaction.atomic():
            self.merge_users(source=source, target=target)
        self.stdout.write(self.style.SUCCESS('Merge complete'))

    def report_related(self, user):
        """List every row still pointing at this user, so nothing is merged blind."""
        for rel in User._meta.related_objects:
            count = rel.related_model.objects.filter(**{rel.field.name: user}).count()
            if count:
                self.stdout.write(f"  {rel.related_model._meta.label}: {count}")

    def merge_users(self, source, target):
        """
        Merges source (the duplicates/slack-only user) INTO target (the email user).
        target keeps its email.
        target gains source's slack_id (if target didn't have one).
        target gains source's points data.
        source is deleted.
        """
        
        # 1. Update Basic Info on Target
        if not target.slack_id:
            target.slack_id = source.slack_id
            self.stdout.write(f"  Transferred slack_id {source.slack_id} to target")
        elif target.slack_id != source.slack_id:
             self.stdout.write(self.style.WARNING(f"  Target already has slack_id {target.slack_id} (Source has {source.slack_id}). Keeping Target's. Source's slack_id will be lost/unlinked."))

        if not target.first_name and source.first_name:
            target.first_name = source.first_name
        if not target.last_name and source.last_name:
            target.last_name = source.last_name
        if not target.avatar_url and source.avatar_url:
            target.avatar_url = source.avatar_url
            
        target.save()

        # 2. Merge Points Data
        # PointsAccount
        try:
            source_account = PointsAccount.objects.get(user=source)
            target_account, created = PointsAccount.objects.get_or_create(user=target)
            
            # Add exact balances; legacy whole-Roo projections are derived.
            PointsService._ensure_microroo_account(source_account)
            PointsService._ensure_microroo_account(target_account)
            target_account.balance_microroo += source_account.balance_microroo
            target_account.earned_balance_microroo += source_account.earned_balance_microroo
            target_account.purchased_topup_balance_microroo += source_account.purchased_topup_balance_microroo
            target_account.lifetime_earned_microroo += source_account.lifetime_earned_microroo
            target_account.lifetime_purchased_topup_microroo += source_account.lifetime_purchased_topup_microroo
            target_account.lifetime_spent_microroo += source_account.lifetime_spent_microroo
            target_account.expired_or_reversed_microroo += source_account.expired_or_reversed_microroo
            PointsService._sync_legacy_account(target_account)
            target_account.save()
            
            self.stdout.write(f"  Merged PointsAccount: +{source_account.balance} pts to target. New balance: {target_account.balance}")
            source_account.delete() 
        except PointsAccount.DoesNotExist:
            self.stdout.write("  No source PointsAccount to merge.")
            
        # Ledger
        count = Ledger.objects.filter(user=source).update(user=target)
        self.stdout.write(f"  moved {count} Ledger entries")
        
        # 3. Merge Tasks and Submissions
        count = Task.objects.filter(assigned_user=source).update(assigned_user=target)
        self.stdout.write(f"  moved {count} assigned Tasks")
        
        count = TaskSubmission.objects.filter(user=source).update(user=target)
        self.stdout.write(f"  moved {count} TaskSubmissions")
        
        # 4. Coworking
        # Handle conflicts for unique constraint (user, date)
        source_bookings = CoworkingBooking.objects.filter(user=source)
        moved_bookings = 0
        for booking in source_bookings:
            # Check if target already has a booking for this date
            if CoworkingBooking.objects.filter(user=target, date=booking.date, status='booked').exists():
                 self.stdout.write(self.style.WARNING(f"  Skipping duplicate booking for date {booking.date}"))
                 # Maybe delete the duplicate if it's redundant?
                 # If we don't move it, and we delete source, it gets deleted (cascade).
                 # That's probably fine if target already has one.
                 pass
            else:
                booking.user = target
                try:
                    booking.save()
                    moved_bookings += 1
                except IntegrityError:
                     self.stdout.write(self.style.WARNING(f"  IntegrityError moving booking {booking.date}"))

        self.stdout.write(f"  moved {moved_bookings} CoworkingBookings")
        
        # 5. Rewards
        count = RewardRedemption.objects.filter(user=source).update(user=target)
        self.stdout.write(f"  moved {count} RewardRedemptions")
        
        # 6. Points Admin
        if PointsAdmin.objects.filter(user=source).exists():
            if not PointsAdmin.objects.filter(user=target).exists():
                PointsAdmin.objects.filter(user=source).update(user=target)
                self.stdout.write("  moved PointsAdmin role")
            else:
                self.stdout.write("  Target is already PointsAdmin, deleting source admin role")
                PointsAdmin.objects.filter(user=source).delete()
        
        # 7. Boost posts
        count = BoostPostAdmission.objects.filter(user=source).update(user=target)
        self.stdout.write(f"  moved {count} BoostPostAdmissions")

        # 8. Delete Source
        # Anything still pointing at source is about to be cascaded away or
        # orphaned by SET_NULL, so name it rather than lose it quietly.
        self.report_related(source)
        self.stdout.write(f"  Deleting source user {source.id}")
        source.delete()
