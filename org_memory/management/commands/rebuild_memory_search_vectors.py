from django.core.management.base import BaseCommand
from django.db import connection

from org_memory.models import MemoryChunk
from org_memory.search import refresh_search_vectors


class Command(BaseCommand):
    help = "Rebuild stored PostgreSQL full-text search vectors for memory chunks."

    def add_arguments(self, parser):
        parser.add_argument("--batch-size", type=int, default=1000)

    def handle(self, *args, **options):
        batch_size = options["batch_size"]
        if batch_size < 1:
            raise ValueError("--batch-size must be positive")
        updated = 0
        if connection.vendor == "postgresql":
            last_pk = None
            while True:
                chunk_ids = MemoryChunk.objects.order_by("pk").values_list(
                    "pk", flat=True
                )
                if last_pk is not None:
                    chunk_ids = chunk_ids.filter(pk__gt=last_pk)
                batch = list(chunk_ids[:batch_size])
                if not batch:
                    break
                updated += refresh_search_vectors(chunk_ids=batch)
                last_pk = batch[-1]
        self.stdout.write(self.style.SUCCESS(f"Refreshed {updated} memory search vectors."))
