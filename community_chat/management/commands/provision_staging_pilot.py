"""Preview/apply ordinary local accounts from separately protected password files."""
import json
import os
from pathlib import Path
from django.contrib.auth import get_user_model
from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError, ImproperlyConfigured
from django.core.validators import validate_email
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction, IntegrityError
from community_chat.staging_password import require_password_pilot
from community_chat.models import CommunityMemberProfile,CommunityChatDevice
from rest_framework.exceptions import NotFound

def private_file(path):
    path=Path(path)
    if path.is_symlink() or not path.is_file() or path.stat().st_uid!=os.geteuid() or path.stat().st_mode & 0o077:
        raise CommandError('Pilot files must be owner-only regular files owned by the operator.')
    if path.stat().st_size>65536:raise CommandError('Pilot file is too large.')
    return path.read_text()

class Command(BaseCommand):
    help='Preview or explicitly create independent ordinary staging pilot accounts; never reset existing accounts.'
    def add_arguments(self,parser):
        parser.add_argument('--manifest',required=True)
        parser.add_argument('--apply',action='store_true')
        parser.add_argument('--reactivate',action='store_true',help='Explicitly reactivate a disabled synthetic pilot after completed membership cleanup; never reset its password.')
    def handle(self,*args,**options):
        try:require_password_pilot()
        except (ImproperlyConfigured,NotFound):raise CommandError('This command requires the enabled isolated password pilot.') from None
        try:
            rows=json.loads(private_file(options['manifest']))
            if not isinstance(rows,list) or not 1<=len(rows)<=20:raise ValueError()
            checked=[];seen=set()
            for row in rows:
                if not isinstance(row,dict) or set(row)!={'email','first_name','last_name','password_file'}:raise ValueError()
                if any(not isinstance(value,str) for value in row.values()):raise ValueError()
                email=row['email'].strip().lower();validate_email(email)
                if email in seen or len(row['first_name'])>150 or len(row['last_name'])>150:raise ValueError()
                seen.add(email)
                password=private_file(row['password_file']).rstrip('\r\n')
                validate_password(password)
                checked.append((email,row['first_name'],row['last_name'],password))
        except (ValueError,ValidationError,OSError):raise CommandError('Invalid pilot manifest or password; no account changes made.') from None
        if options['reactivate']:
            with transaction.atomic():
                users=[]
                for email,first,last,password in checked:
                    user=get_user_model().objects.select_for_update().filter(email__iexact=email).first()
                    profile=CommunityMemberProfile.objects.filter(user=user).first() if user else None
                    if (not user or user.is_active or user.is_staff or user.is_superuser or not user.check_password(password)
                        or not profile or 'synthetic_staging_pilot' not in profile.review_reasons
                        or CommunityChatDevice.objects.filter(user=user,revoked_at__isnull=True).exists()):
                        raise CommandError('Reactivation requires a disabled ordinary synthetic pilot, its unchanged password and completed device cleanup.')
                    users.append(user)
                if options['apply']:
                    for user in users:
                        user.is_active=True;user.save(update_fields=['is_active'])
            self.stdout.write(f'{"Reactivated" if options["apply"] else "Would reactivate"} {len(users)} synthetic pilot account(s). Passwords unchanged.'+(' No changes made.' if not options['apply'] else ''))
            return
        if any(get_user_model().objects.filter(email__iexact=row[0]).exists() for row in checked):
            raise CommandError('An account already exists; takeover and password reset are refused.')
        if not options['apply']:
            self.stdout.write(f'Would create {len(checked)} ordinary staging account(s). No changes made.');return
        try:
            with transaction.atomic():
                for email,first,last,password in checked:
                    user=get_user_model().objects.create_user(email=email,password=password,first_name=first,last_name=last,is_staff=False,is_superuser=False)
                    CommunityMemberProfile.objects.create(user=user,status=CommunityMemberProfile.Status.APPROVED,review_reasons=['synthetic_staging_pilot'])
        except IntegrityError:raise CommandError('Account creation conflict; all changes rolled back.') from None
        self.stdout.write(f'Created {len(checked)} ordinary staging account(s). Passwords were not printed or emailed.')
