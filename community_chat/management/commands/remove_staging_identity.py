"""Explicit pilot removal; fail closed locally even if remote cleanup needs retry."""
from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand,CommandError
from django.db import transaction
from django.utils import timezone
from community_chat import adapter
from community_chat.federation_models import ChatFederationIdentity
from community_chat.models import CommunityChatAccountSession,CommunityChatBootstrapToken,CommunityChatDevice,CommunityChatInviteAudit,DeviceBindingStatus,CommunityMemberProfile


class Command(BaseCommand):
    help='Preview or remove a federated staging identity and its staging relay memberships.'

    def add_arguments(self,parser):
        identity=parser.add_mutually_exclusive_group(required=True)
        identity.add_argument('--subject')
        identity.add_argument('--email')
        parser.add_argument('--apply',action='store_true')

    def handle(self,*args,**options):
        if settings.ROOT_URLCONF != 'mlai.urls_staging_chat' or settings.COMMUNITY_CHAT_FRONTEND_URL != 'https://chat-staging.mlai.au':
            raise CommandError('This command requires the isolated staging Chat profile.')
        if options.get('email'):
            if settings.SETTINGS_MODULE!='mlai.settings_staging_chat' or settings.APP_ENV!='chat-staging':
                raise CommandError('Pilot removal requires the isolated staging settings/environment.')
            account=get_user_model().objects.filter(email__iexact=options['email']).first()
            profile=CommunityMemberProfile.objects.filter(user=account).first() if account else None
            if not profile or 'synthetic_staging_pilot' not in profile.review_reasons:
                raise CommandError('Synthetic staging pilot not found.')
            user_id=account.pk
        else:
            mapping=ChatFederationIdentity.objects.filter(issuer=settings.COMMUNITY_CHAT_FEDERATION_ISSUER,subject=options['subject']).first()
            if not mapping:raise CommandError('Staging identity not found.')
            user_id=mapping.user_id
        devices=CommunityChatDevice.objects.filter(user_id=user_id,revoked_at__isnull=True)
        count=devices.count()
        if not options['apply']:
            self.stdout.write(f'Would disable one staging account and remove {count} memberships. No changes made.')
            return
        with transaction.atomic():
            user=get_user_model().objects.select_for_update().get(pk=user_id)
            user.is_active=False
            user.save(update_fields=['is_active'])
            CommunityChatAccountSession.objects.filter(user=user,revoked_at__isnull=True).update(revoked_at=timezone.now())
            CommunityChatBootstrapToken.objects.filter(user=user,revoked_at__isnull=True).update(revoked_at=timezone.now())
        failures=0
        for device in devices:
            try:
                for audit in CommunityChatInviteAudit.objects.filter(device=device,confirmed_at__isnull=True):
                    adapter.revoke_member_invite(audit.adapter_invite_id)
                adapter.revoke_relay_membership(device.public_key)
            except (adapter.MembershipAdapterUnavailable,adapter.MembershipAdapterConflict):
                failures+=1
                continue
            device.status=DeviceBindingStatus.REVOKED
            device.revoked_at=timezone.now()
            device.save(update_fields=['status','revoked_at','updated_at'])
        self.stdout.write(f'Staging account disabled; {count-failures} memberships removed; {failures} pending.')
        if failures:
            raise CommandError('Membership cleanup incomplete. Account access is disabled; retry this command after adapter recovery.')
