import logging
import time
from django.core.management.base import BaseCommand, CommandError
from django.db import OperationalError, transaction
from core.models import User
from core.actor_ids import actor_ids_for_user
from core.slack_founder_links import (
    invalidate_unused_slack_founder_link_requests,
    user_participates_in_slack_founder_link,
)
from roo.models import (
    PointsAccount, Ledger, Task, TaskSubmission, 
    CoworkingBooking, RewardRedemption, PointsAdmin, BoostPostAdmission
)
from roo.services import CoworkingService, PointsService
from integrations.services import SlackService

logger = logging.getLogger(__name__)

MERGE_TRANSACTION_RETRY_ATTEMPTS = 3


def _retryable_transaction_error(exc):
    cause = getattr(exc, '__cause__', None)
    code = (
        getattr(exc, 'pgcode', None)
        or getattr(exc, 'sqlstate', None)
        or getattr(cause, 'pgcode', None)
        or getattr(cause, 'sqlstate', None)
    )
    return code in {'40P01', '40001'}

class Command(BaseCommand):
    help = 'Clean up duplicate users by merging Slack users into Email users'

    @staticmethod
    def _scalar_model_references_actor(model, field, actor_ids):
        """Match runtime actor resolution, whose Python strip handles all whitespace."""
        return any(
            str(value or '').strip() in actor_ids
            for value in model.objects.values_list(field, flat=True).iterator(
                chunk_size=500
            )
        )

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
        commit_failures = []
        
        for slack_user in slack_users:
            slack_id = slack_user.slack_id
            current_email = slack_user.email

            if user_participates_in_slack_founder_link(slack_user):
                self.stdout.write(
                    self.style.WARNING(
                        f"Skipping linked account {slack_user.pk}; manual support required."
                    )
                )
                skipped_count += 1
                continue
            
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
                        with transaction.atomic():
                            locked_slack_user = User.objects.select_for_update().get(
                                pk=slack_user.pk
                            )
                            if user_participates_in_slack_founder_link(
                                locked_slack_user
                            ):
                                self.stdout.write(
                                    self.style.WARNING(
                                        f"Skipping linked account {slack_user.pk}; manual support required."
                                    )
                                )
                                skipped_count += 1
                                continue
                            locked_slack_user.email = real_email
                            locked_slack_user.save(update_fields=["email"])
                    updated_count += 1
                    continue
                
                # Conflict exists: Merge slack_user INTO target_user
                if target_user.pk == slack_user.pk:
                     continue

                self.stdout.write(self.style.SUCCESS(
                    f"MERGE REQUIRED: Source(id={slack_user.id}, email={slack_user.email}, slack={slack_id}) -> Target(id={target_user.id}, email={target_user.email}, slack={target_user.slack_id})"
                ))
                
                if commit:
                    self.merge_users_with_retry(
                        source_id=slack_user.pk,
                        target_id=target_user.pk,
                    )
                
                merged_count += 1
                
            except Exception as e:
                self.stdout.write(self.style.ERROR(f"Error processing {slack_user.email}: {e}"))
                skipped_count += 1
                if commit:
                    commit_failures.append(
                        f"{slack_id}: {e.__class__.__name__}"
                    )

        self.stdout.write(self.style.SUCCESS(f"Done. Updated Emails: {updated_count}, Merged Users: {merged_count}, Skipped/Error: {skipped_count}"))
        if commit_failures:
            raise CommandError(
                "User cleanup left unresolved committed merges: "
                + "; ".join(commit_failures[:10])
            )

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

        self.merge_users_with_retry(
            source_id=source.pk,
            target_id=target.pk,
        )
        self.stdout.write(self.style.SUCCESS('Merge complete'))

    def merge_users_with_retry(self, *, source_id, target_id):
        """Retry only whole merge transactions aborted by PostgreSQL."""
        for attempt in range(MERGE_TRANSACTION_RETRY_ATTEMPTS):
            try:
                with transaction.atomic():
                    principals = {
                        user.pk: user
                        for user in User.objects.select_for_update()
                        .filter(pk__in=sorted([source_id, target_id]))
                        .order_by('pk')
                    }
                    if len(principals) != 2:
                        raise CommandError('Both merge principals must still exist')
                    self.merge_users(
                        source=principals[source_id],
                        target=principals[target_id],
                    )
                return
            except OperationalError as exc:
                if (
                    not _retryable_transaction_error(exc)
                    or attempt + 1 >= MERGE_TRANSACTION_RETRY_ATTEMPTS
                ):
                    raise
                time.sleep(0.05 * (2 ** attempt))

    def report_related(self, user):
        """List every row still pointing at this user, so nothing is merged blind."""
        for label, _field_name, count in self.remaining_related(user):
            self.stdout.write(f"  {label}: {count}")

    def remaining_related(self, user):
        """Return every durable relation that would be changed by deleting user.

        The merge intentionally handles only relations whose conflict and
        accounting semantics are known.  This repository has many additional
        CASCADE, SET_NULL and PROTECT user relations; silently relying on their
        ``on_delete`` behavior would either destroy history, orphan ownership,
        or make the merge fail late.  Introspection makes new user relations
        fail closed until their merge semantics are explicitly implemented.
        """
        remaining = []
        for rel in User._meta.related_objects:
            field_name = rel.field.name
            count = rel.related_model._base_manager.filter(
                **{field_name: user}
            ).count()
            if count:
                remaining.append(
                    (rel.related_model._meta.label, field_name, count)
                )
        for field in User._meta.many_to_many:
            through_model = field.remote_field.through
            source_field_name = field.m2m_field_name()
            count = through_model._base_manager.filter(
                **{source_field_name: user}
            ).count()
            if count:
                remaining.append((User._meta.label, field.name, count))
        return remaining

    def assert_no_unhandled_relations(self, user):
        remaining = self.remaining_related(user)
        if not remaining:
            return
        details = ", ".join(
            f"{label}.{field_name}={count}"
            for label, field_name, count in remaining
        )
        raise CommandError(
            "Refusing to delete merge source because durable user relations "
            f"remain unhandled: {details}"
        )

    @staticmethod
    def _payload_references_actor(value, actor_ids, *, actor_field=False):
        """Return whether identity-bearing JSON refers to a source actor."""
        identity_keys = {
            "actor_id",
            "connected_slack_user_id",
            "requested_by_slack_user_id",
            "slack_user_id",
        }
        if actor_field and isinstance(value, str):
            return value.strip() in actor_ids
        if isinstance(value, list):
            return any(
                Command._payload_references_actor(item, actor_ids)
                for item in value
            )
        if isinstance(value, dict):
            return any(
                Command._payload_references_actor(
                    item,
                    actor_ids,
                    actor_field=key in identity_keys,
                )
                for key, item in value.items()
            )
        return False

    def _assert_no_external_identity_state(self, source):
        """Fail closed instead of deleting or orphaning external authority.

        Content Factory ownership is partly relational and partly stored as
        actor-id strings. Safely combining credential bundles and historical
        ownership requires a dedicated, operator-reviewed reconciliation; a
        generic duplicate-user cleanup must not guess which grant wins.
        """
        from content_factory.models import (
            ContentFactoryJob,
            OrganizationContentConfig,
            ScheduledDiscoveryDispatch,
        )
        from integrations.models import GitHubInstallation, UserIntegration
        from workflow_runs.models import ContentFactoryRun

        actor_ids = set(actor_ids_for_user(source))
        references = []
        if GitHubInstallation.objects.filter(user=source).exists():
            references.append("GitHub installations")
        if self._scalar_model_references_actor(
            UserIntegration, 'slack_user_id', actor_ids
        ):
            references.append("integration credentials")
        if self._scalar_model_references_actor(
            OrganizationContentConfig, 'connected_slack_user_id', actor_ids
        ):
            references.append("Content Factory organization ownership")
        if self._scalar_model_references_actor(
            ScheduledDiscoveryDispatch, 'slack_user_id', actor_ids
        ):
            references.append("scheduled discovery dispatches")

        job_reference = self._scalar_model_references_actor(
            ContentFactoryJob, 'slack_user_id', actor_ids
        )
        if not job_reference:
            job_reference = any(
                self._payload_references_actor(payload, actor_ids)
                for payload in ContentFactoryJob.objects.values_list(
                    "request_meta", flat=True
                ).iterator(chunk_size=500)
            )
        if job_reference:
            references.append("Content Factory jobs")

        run_reference = self._scalar_model_references_actor(
            ContentFactoryRun, 'slack_user_id', actor_ids
        )
        if not run_reference:
            run_reference = any(
                self._payload_references_actor(payload, actor_ids)
                for payload in ContentFactoryRun.objects.values_list(
                    "run_request", flat=True
                ).iterator(chunk_size=500)
            )
        if run_reference:
            references.append("Content Factory runs")

        if references:
            raise CommandError(
                "Cannot merge an account with external identity state "
                f"({', '.join(references)}); manual support is required."
            )

    @transaction.atomic
    def merge_users(self, source, target):
        """
        Merges source (the duplicates/slack-only user) INTO target (the email user).
        target keeps its email.
        target gains source's slack_id (if target didn't have one).
        target gains source's points data.
        source is deleted.
        """
        
        locked_users = {
            user.pk: user
            for user in User.objects.select_for_update()
            .filter(pk__in=sorted({source.pk, target.pk}))
            .order_by("pk")
        }
        source = locked_users[source.pk]
        target = locked_users[target.pk]
        if user_participates_in_slack_founder_link(
            source
        ) or user_participates_in_slack_founder_link(target):
            raise CommandError(
                "Cannot merge an account with an explicit Roo-Founder Tools link; "
                "manual support is required."
            )
        self._assert_no_external_identity_state(source)
        invalidate_unused_slack_founder_link_requests(source, target)

        # Validate and move durable booking/Office Manager ownership before
        # touching balances or identity fields. Any ambiguity aborts this
        # entire account merge instead of cascading away an active owner.
        moved_bookings, moved_assignments = (
            CoworkingService.transfer_user_ownership_for_merge(
                source=source,
                target=target,
            )
        )

        # 1. Update Basic Info on Target
        if not target.slack_id:
            transferred_slack_id = source.slack_id
            if transferred_slack_id:
                # ``slack_id`` is unique. Release it from the source before
                # assigning it to the target; the outer atomic merge restores
                # both rows if any later ownership transfer fails.
                source.slack_id = None
                source.save(update_fields=['slack_id'])
            target.slack_id = transferred_slack_id
            self.stdout.write(f"  Transferred slack_id {transferred_slack_id} to target")
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
        # PointsAccount. Follow the global user -> booking -> account lock order
        # and lock both accounts deterministically before changing balances.
        accounts = {
            account.user_id: account
            for account in PointsAccount.objects.select_for_update()
            .filter(user_id__in=sorted([source.pk, target.pk]))
            .order_by('user_id')
        }
        try:
            source_account = accounts[source.pk]
            target_account = accounts.get(target.pk)
            if target_account is None:
                PointsAccount.objects.create(user=target)
                target_account = PointsAccount.objects.select_for_update().get(
                    user=target
                )
            
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
        except KeyError:
            self.stdout.write("  No source PointsAccount to merge.")
            
        # Ledger
        count = Ledger.objects.filter(user=source).update(user=target)
        self.stdout.write(f"  moved {count} Ledger entries")
        
        # 3. Merge Tasks and Submissions
        count = Task.objects.filter(assigned_user=source).update(assigned_user=target)
        self.stdout.write(f"  moved {count} assigned Tasks")
        
        count = TaskSubmission.objects.filter(user=source).update(user=target)
        self.stdout.write(f"  moved {count} TaskSubmissions")
        
        # 4. Coworking and Office Manager ownership moved together above.
        self.stdout.write(f"  moved {moved_bookings} CoworkingBookings")
        self.stdout.write(
            f"  moved {moved_assignments} OfficeManagerAssignments"
        )
        
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

        # 8. Delete Source. Fail closed if any relation was not deliberately
        # transferred or reconciled above. The surrounding atomic transaction
        # rolls back all earlier mutations, including Slack identity and
        # balance changes, when this guard fires.
        self.assert_no_unhandled_relations(source)
        self.stdout.write(f"  Deleting source user {source.id}")
        source.delete()
