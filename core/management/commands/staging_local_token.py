"""Explicit private local credential; never usable as a hosted staging session."""
import os
from pathlib import Path
from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError
from django.db import connection, transaction
from community_chat.models import CommunityMemberProfile
from core.refresh_sessions import issue_refresh_token

class Command(BaseCommand):
    help='Write a short-lived local developer token to a new private file.'
    def add_arguments(self,parser):
        parser.add_argument('--user',required=True)
        parser.add_argument('--output',required=True)
        parser.add_argument('--create',action='store_true',help='Explicitly create an ordinary synthetic local fixture if absent.')
    def handle(self,*args,**options):
        database=settings.DATABASES['default']
        expected=getattr(settings,'STAGING_LOCAL_DB_NAME','')
        if (settings.SETTINGS_MODULE!='mlai.settings_staging_local' or settings.APP_ENV!='staging-local'
            or database.get('HOST') not in ('localhost','127.0.0.1','::1')
            or not expected or expected=='staging_account_api' or database.get('NAME')!=expected):
            raise CommandError('Only the isolated loopback local backend may issue this token.')
        with connection.cursor() as cursor:
            cursor.execute('select current_database()')
            if cursor.fetchone()[0]!=expected:raise CommandError('Unexpected database target.')
        from django.db.migrations.executor import MigrationExecutor
        executor=MigrationExecutor(connection)
        if executor.migration_plan(executor.loader.graph.leaf_nodes()):raise CommandError('Pending schema; explicit schema approval is required.')
        email=options['user'].strip().lower()
        if not email.endswith('@staging.mlai.au') or len(email)>254:raise CommandError('Use a synthetic staging alias.')
        output=Path(options['output'])
        if not output.parent.is_dir() or output.exists() or output.is_symlink():raise CommandError('Output must be a new file in an existing private directory.')
        with transaction.atomic():
            user=get_user_model().objects.select_for_update().filter(email__iexact=email).first()
            if user is None and options['create']:
                user=get_user_model().objects.create_user(email=email,password=None,is_staff=False,is_superuser=False)
                CommunityMemberProfile.objects.create(user=user,status='approved',review_reasons=['synthetic_staging_local'])
            profile=CommunityMemberProfile.objects.filter(user=user).first() if user else None
            if not user or not user.is_active or user.is_staff or user.is_superuser or not profile or 'synthetic_staging_local' not in profile.review_reasons:
                raise CommandError('Only marked ordinary active local fixtures are allowed.')
            token=issue_refresh_token(user).access_token
            from datetime import timedelta
            token.set_exp(lifetime=timedelta(minutes=15))
            try:
                with os.fdopen(os.open(output,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600),'w') as file:
                    file.write(str(token)+'\n')
            except OSError:raise CommandError('Private token file could not be created.') from None
        self.stdout.write('Local access token saved privately; expires in 15 minutes. No hosted credentials issued.')
