"""Transactional, fail-closed account repair with preserved history and aliases."""

import json
import os
from pathlib import Path

from django.apps import apps
from django.contrib.admin.models import CHANGE, LogEntry
from django.contrib.contenttypes.models import ContentType
from django.core import serializers
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone

from core.management.commands.cleanup_users import Command as LegacyMerge
from core.models import User, UserEmailAlias
from core.slack_founder_links import (
    assign_direct_slack_identity, invalidate_unused_slack_founder_link_requests,
    user_participates_in_slack_founder_link,
)
from roo.models import Ledger, PointsAccount
from roo.services import PointsService


MOVABLE = {
    ('community_chat.CommunityMemberConsent', 'user'),
    ('community_chat.CommunityChatDevice', 'user'),
    ('community_chat.CommunityChatDevice', 'revoked_by'),
    ('community_chat.CommunityChatChallenge', 'user'),
    ('community_chat.CommunityChatEmailCodeChallenge', 'user'),
    ('community_chat.CommunityChatAccountSession', 'user'),
    ('roo.Ledger', 'user'),
    ('roo.BoostPostAdmission', 'user'),
    ('roo.CoworkingBooking', 'user'),
}
PROFILES = {'community_chat.CommunityMemberProfile', 'founder_tools.VibeRaisingProfile'}
SPECIAL = {(label, 'user') for label in PROFILES | {'roo.PointsAccount'}} | {
    ('roo.CoworkingBookingOperation', 'subjects'),
}


class Command(BaseCommand):
    help = 'Merge reviewed duplicates into a primary user; dry run unless --commit.'

    def add_arguments(self, parser):
        parser.add_argument('--target-email', required=True)
        parser.add_argument('--source-email', action='append', required=True)
        parser.add_argument('--slack-id', required=True)
        parser.add_argument('--balance', required=True, type=int)
        parser.add_argument('--actor-email', required=True)
        parser.add_argument('--repair-key', required=True)
        parser.add_argument('--backup-dir', required=True)
        parser.add_argument('--commit', action='store_true')

    @transaction.atomic
    def handle(self, *args, **options):
        from core.slack_users import validate_slack_id

        if not validate_slack_id(options['slack_id'])[0] or options['slack_id'] == 'system':
            raise CommandError('A real Slack user ID is required.')
        if options['balance'] < 0:
            raise CommandError('Balance must be nonnegative.')
        if not options['repair_key'] or len(options['repair_key']) > 150:
            raise CommandError('Provide a bounded unique repair key.')
        target_email = User.objects.normalize_email(options['target_email'])
        source_emails = {User.objects.normalize_email(email) for email in options['source_email']}
        if target_email in source_emails or len(source_emails) != len(options['source_email']):
            raise CommandError('Source and target addresses must be distinct.')
        actor = User.objects.get(email=User.objects.normalize_email(options['actor_email']), is_superuser=True, is_active=True)
        key = 'account-merge:' + options['repair_key']
        if Ledger.objects.filter(idempotency_key=key).exists():
            target = User.objects.get(email=target_email)
            if (target.slack_id == options['slack_id']
                and set(target.email_aliases.values_list('email', flat=True)) >= source_emails
                and not User.objects.filter(email__in=source_emails).exists()):
                self.stdout.write('Repair already committed; no changes made.')
                return
            raise CommandError('Repair key exists but identity postconditions do not match.')

        users = list(User.objects.select_for_update().filter(email__in=source_emails | {target_email}).order_by('pk'))
        if len(users) != len(source_emails) + 1:
            raise CommandError('Exact source and target accounts must exist.')
        target = next(user for user in users if user.email == target_email)
        sources = [user for user in users if user.pk != target.pk]
        if not target.is_active or any(user.is_staff or user.is_superuser for user in users):
            raise CommandError('Inactive targets and privileged accounts require a separate review.')
        if any(user_participates_in_slack_founder_link(user) for user in users):
            raise CommandError('Explicit founder links require a separate review.')
        if User.objects.filter(slack_id=options['slack_id']).exclude(pk=target.pk).exists():
            raise CommandError('Requested Slack identity is owned by another account.')
        if apps.get_model('core', 'AccountBan').objects.filter(user__in=users, revoked_at__isnull=True).exists():
            raise CommandError('Banned accounts cannot be merged.')

        recovery = {('core.User', user.pk): user for user in users}
        related_before = {}
        profile_snapshots = []
        legacy = LegacyMerge()
        for user in users:
            for rel in User._meta.related_objects:
                rows = list(rel.related_model.objects.filter(**{rel.field.name: user}))
                if not rows:
                    continue
                relation = (rel.related_model._meta.label, rel.field.name)
                related_before[f'{user.pk}:{relation[0]}:{relation[1]}'] = [str(row.pk) for row in rows]
                if user in sources and relation not in MOVABLE | SPECIAL:
                    raise CommandError(f'Unreviewed source relation: {relation}')
                for row in rows:
                    recovery[(row._meta.label, row.pk)] = row
            if user in sources:
                legacy._assert_no_external_identity_state(user)
                for label in PROFILES:
                    model = apps.get_model(label)
                    profile = model.objects.filter(user=user).first()
                    if profile is None:
                        continue
                    for rel in model._meta.related_objects:
                        if rel.related_model.objects.filter(**{rel.field.name: profile}).exists():
                            raise CommandError(f'Source profile has dependent data: {label}')
                    primary = model.objects.filter(user=target).first()
                    if primary:
                        if label == 'founder_tools.VibeRaisingProfile':
                            if profile.role != primary.role or profile.active_company_id or profile.organization_name:
                                raise CommandError('Source founder profile needs manual reconciliation.')
                        else:
                            fields = ('status', 'policy_version', 'city', 'interests', 'marketing_opt_in')
                            if any(getattr(profile, f) != getattr(primary, f) for f in fields):
                                raise CommandError('Source community profile differs from the primary.')
                        profile_snapshots.append(json.loads(serializers.serialize('json', [profile]))[0])

        accounts = list(PointsAccount.objects.select_for_update().filter(user__in=users).order_by('user_id'))
        if len(accounts) != len(users) or any(not a.microroo_initialized for a in accounts):
            raise CommandError('All accounts must have initialized precision balances.')
        if any(a.purchased_topup_balance_microroo or a.digital_balance_microroo or a.digital_refund_debt_microroo for a in accounts):
            raise CommandError('Purchased/digital balances require a separate reviewed allocation.')
        source_ids = [source.pk for source in sources]
        original_ledger_ids = set(Ledger.objects.filter(user__in=users).values_list('pk', flat=True))
        final_microroo = options['balance'] * 1_000_000
        combined = sum(a.balance_microroo for a in accounts)
        adjustment = final_microroo - combined
        self.stdout.write(json.dumps({'target_id': target.pk, 'source_ids': source_ids, 'ledger_entries_preserved': len(original_ledger_ids), 'combined_balance_microroo': combined, 'adjustment_microroo': adjustment, 'final_balance_microroo': final_microroo, 'slack_id': options['slack_id'], 'commit': options['commit']}))
        now = timezone.now()
        backup_path = None
        if options['commit']:
            directory = Path(options['backup_dir'])
            directory.mkdir(mode=0o700, parents=True, exist_ok=True)
            backup_path = directory / f"merge-{target.pk}-{now.strftime('%Y%m%dT%H%M%S%f')}.json"
            descriptor = os.open(backup_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(descriptor, 'w') as out:
                json.dump({'repair_key': key, 'relations': related_before, 'objects': json.loads(serializers.serialize('json', list(recovery.values())))}, out)
            self.stdout.write(f'Recovery snapshot saved: {backup_path}')

        # Use the existing authority revocation boundary before transferring old
        # bindings. A dry run never contacts the relay or sends any message.
        Device = apps.get_model('community_chat', 'CommunityChatDevice')
        if options['commit']:
            from community_chat.adapter import revoke_member_invite, revoke_relay_membership
            from community_chat.device_revocation import revoke_device_authority
            for source in sources:
                for device in Device.objects.filter(user=source).order_by('id'):
                    revoke_device_authority(source, device_id=device.pk, public_key=device.public_key,
                        reason='account_merge', allow_already_revoked=True,
                        revoke_member_invite_callback=revoke_member_invite,
                        revoke_relay_membership_callback=revoke_relay_membership)

        invalidate_unused_slack_founder_link_requests(*users)
        Device.objects.filter(user__in=sources).update(status='revoked', revoked_at=now, revoked_by=actor, revocation_reason='account_merge')
        apps.get_model('community_chat', 'CommunityChatAccountSession').objects.filter(user__in=sources).update(revoked_at=now)
        apps.get_model('community_chat', 'CommunityChatChallenge').objects.filter(user__in=sources, used_at__isnull=True).update(used_at=now)
        apps.get_model('community_chat', 'CommunityChatEmailCodeChallenge').objects.filter(user__in=sources).update(invalidated_at=now, encrypted_signup_email='')

        for source in sources:
            for label in PROFILES:
                model = apps.get_model(label)
                profile = model.objects.filter(user=source).first()
                if profile:
                    if model.objects.filter(user=target).exists():
                        profile.delete()  # Complete historical copy is in backup and admin audit.
                    else:
                        model.objects.filter(pk=profile.pk).update(user=target)
            for label, field in MOVABLE:
                apps.get_model(label).objects.filter(**{field: source}).update(**{field: target})
            for receipt in apps.get_model('roo', 'CoworkingBookingOperation').objects.filter(subjects=source):
                receipt.subjects.add(target)
                receipt.subjects.remove(source)

        account = next(a for a in accounts if a.user_id == target.pk)
        for field in ('balance_microroo', 'earned_balance_microroo', 'purchased_topup_balance_microroo', 'lifetime_earned_microroo', 'lifetime_purchased_topup_microroo', 'lifetime_spent_microroo', 'expired_or_reversed_microroo'):
            setattr(account, field, sum(getattr(a, field) for a in accounts))
        account.balance_microroo = final_microroo
        account.earned_balance_microroo = final_microroo
        if adjustment < 0:
            account.expired_or_reversed_microroo += -adjustment
        elif adjustment > 0:
            account.lifetime_earned_microroo += adjustment
        PointsService._sync_legacy_account(account)
        account.save()
        Ledger.objects.create(user=target, delta=adjustment // 1_000_000, delta_microroo=adjustment,
            kind='ADJUST', source='MANUAL', reference_type='ACCOUNT_MERGE', reference_id=options['repair_key'],
            description=f'Administrator-approved account merge; final balance {options["balance"]} Roo; source users {source_ids}.',
            created_by_slack_id=f'mlai_user:{actor.pk}', idempotency_key=key)
        PointsAccount.objects.filter(user__in=sources).delete()
        for source in sources:
            for rel in User._meta.related_objects:
                if rel.related_model.objects.filter(**{rel.field.name: source}).exists():
                    raise CommandError(f'Remaining source reference: {rel.related_model._meta.label}.{rel.field.name}')
            old_email, verified = source.email, source.email_verified_at
            source.delete()
            UserEmailAlias.objects.create(user=target, email=old_email,
                verified_at=None if old_email.endswith('@slack.placeholder.com') else verified or now)
        target = assign_direct_slack_identity(target, options['slack_id'], allow_reassignment=True)
        if not target.avatar_url:
            avatar = next((u.avatar_url for u in sources if u.avatar_url), None)
            if avatar:
                target.avatar_url = avatar
                target.save(update_fields=('avatar_url', 'updated_at'))
        audit = {'repair_key': key, 'source_user_ids': source_ids, 'target_user_id': target.pk,
                 'aliases': sorted(source_emails), 'slack_id': target.slack_id,
                 'adjustment_microroo': adjustment, 'final_balance_microroo': final_microroo,
                 'related_records_before': related_before, 'reconciled_source_profiles': profile_snapshots,
                 'backup_path': str(backup_path) if backup_path else None}
        LogEntry.objects.log_action(user_id=actor.pk, content_type_id=ContentType.objects.get_for_model(User).pk,
            object_id=str(target.pk), object_repr=target.email, action_flag=CHANGE,
            change_message=json.dumps(audit, default=str))
        if set(Ledger.objects.filter(user=target).values_list('pk', flat=True)) - {Ledger.objects.get(idempotency_key=key).pk} != original_ledger_ids:
            raise CommandError('Ledger preservation check failed.')
        if any(User.objects.resolve_email(email).pk != target.pk for email in source_emails | {target_email}):
            raise CommandError('Alias identity check failed.')
        if PointsService.get_balance(target)['balance_microroo'] != final_microroo:
            raise CommandError('Final balance check failed.')
        self.stdout.write('All merge postconditions passed.')
        if not options['commit']:
            transaction.set_rollback(True)
            self.stdout.write('Dry run rolled back; production records unchanged.')
