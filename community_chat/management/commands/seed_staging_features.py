"""Explicit additive staging fixture operation; dry-run by default."""
import json,os
from pathlib import Path
from django.core.management.base import BaseCommand,CommandError
from community_chat.volunteer.staging_fixtures import seed_features
class Command(BaseCommand):
    help='Seed isolated synthetic Volunteer and private startup examples.'
    def add_arguments(self,parser):
        parser.add_argument('--channel-receipt',required=True)
        parser.add_argument('--apply',action='store_true')
    def handle(self,*args,**options):
        path=Path(options['channel_receipt']);info=path.stat()
        if info.st_mode & 0o077 or info.st_uid!=os.getuid():raise CommandError('Receipt must be owner-only.')
        try:result=seed_features(json.loads(path.read_text()),apply=options['apply'])
        except ValueError as exc:raise CommandError(str(exc)) from exc
        self.stdout.write(json.dumps(result,sort_keys=True))
