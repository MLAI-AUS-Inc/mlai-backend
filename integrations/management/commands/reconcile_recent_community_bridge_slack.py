"""Temporary CLI compatibility for the retired reconciler container."""
import json
from django.core.management.base import BaseCommand


class Command(BaseCommand):
    help = "Retired reconciler compatibility; only seed the normal durable worker."

    def add_arguments(self, parser):
        # Accept the old container invocation through its retirement release.
        for name in ("lookback-seconds", "max-roots-per-channel", "maximum-history-messages", "wait-seconds"):
            parser.add_argument("--" + name, type=int, default=0)

    def handle(self, *args, **options):
        from integrations.services.message_sync.inbox import enabled
        from integrations.services.message_sync.history import seed_states
        active = enabled()
        if active:
            seed_states()
        self.stdout.write(json.dumps({"scheduler": "durable", "enabled": active, "legacy_scan_skipped": True}))
