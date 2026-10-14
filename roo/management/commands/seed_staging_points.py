import json
from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand,CommandError
from django.db import transaction
from community_chat.management.commands.provision_staging_pilot import private_file
from roo.staging_fixtures import EMAILS,require_fixture_database,require_fixture,seed_user,seed_catalogue
class Command(BaseCommand):
    help='Preview/apply versioned synthetic staging ledger fixtures, preserving existing data.'
    def add_arguments(self,parser):
        parser.add_argument('--manifest',required=True);parser.add_argument('--apply',action='store_true')
    def handle(self,*args,**options):
        try:
            require_fixture_database();rows=json.loads(private_file(options['manifest']))
            if not isinstance(rows,list) or len(rows)!=8 or {row['email'] for row in rows}!=set(EMAILS):raise ValueError('Exactly the eight approved aliases are required.')
            with transaction.atomic():
                users=list(get_user_model().objects.select_for_update().filter(email__in=EMAILS).order_by('pk'))
                if len(users)!=8:raise ValueError('Provision the eight marked accounts first.')
                for user in users:require_fixture(user)
                if options['apply']:
                    for user in users:seed_user(user)
                    seed_catalogue()
        except (ValueError,KeyError,TypeError) as exc:raise CommandError(str(exc)) from None
        self.stdout.write(('Seeded/preserved' if options['apply'] else 'Would seed/preserve')+' eight synthetic account ledgers and room/reward examples. No credentials printed.')
