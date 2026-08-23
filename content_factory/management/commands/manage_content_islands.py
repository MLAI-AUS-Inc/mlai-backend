from __future__ import annotations

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone

from content_factory.content_islands import rebuild_island_edges
from content_factory.models import ContentIsland, ContentIslandEdge, ContentIslandStatus
from organizations.models import Organization


class Command(BaseCommand):
    help = (
        "Inspect and hand-correct an organization's content islands. "
        "--list is read-only; --restore-island / --archive-island move one island "
        "between states and recompute the org's edge set."
    )

    def add_arguments(self, parser):
        parser.add_argument("--domain", required=True, help="Organization domain (e.g. mlai.au).")
        parser.add_argument("--list", action="store_true", help="List the org's islands and edges.")
        parser.add_argument(
            "--restore-island",
            default="",
            metavar="SLUG",
            help="Bring an archived island back as emerging and reset its miss counter.",
        )
        parser.add_argument(
            "--archive-island",
            default="",
            metavar="SLUG",
            help="Archive an island so it leaves the graph.",
        )

    def handle(self, *args, **options):
        domain = str(options["domain"] or "").strip().lower()
        restore_slug = str(options["restore_island"] or "").strip()
        archive_slug = str(options["archive_island"] or "").strip()
        do_list = bool(options["list"])

        org = Organization.objects.filter(domain__iexact=domain).first()
        if not org:
            raise CommandError(f"No organization for domain {domain}")

        if not (do_list or restore_slug or archive_slug):
            do_list = True

        if restore_slug:
            self._restore(org, restore_slug)
        if archive_slug:
            self._archive(org, archive_slug)
        if do_list:
            self._list(org)

    def _island_or_fail(self, org, slug):
        island = ContentIsland.objects.filter(organization=org, slug=slug).first()
        if not island:
            raise CommandError(f"No island '{slug}' for {org.domain}")
        return island

    def _restore(self, org, slug):
        island = self._island_or_fail(org, slug)
        with transaction.atomic():
            island.status = ContentIslandStatus.EMERGING
            island.archived_at = None
            island.consecutive_misses = 0
            island.last_missed_on = None
            island.save(
                update_fields=[
                    "status", "archived_at", "consecutive_misses", "last_missed_on", "updated_at"
                ]
            )
            rebuild_island_edges(org)
        self.stdout.write(self.style.SUCCESS(f"Restored {slug} -> emerging (misses reset)"))

    def _archive(self, org, slug):
        island = self._island_or_fail(org, slug)
        with transaction.atomic():
            island.status = ContentIslandStatus.ARCHIVED
            island.archived_at = timezone.now()
            island.save(update_fields=["status", "archived_at", "updated_at"])
            rebuild_island_edges(org)
        self.stdout.write(self.style.SUCCESS(f"Archived {slug}"))

    def _list(self, org):
        islands = list(ContentIsland.objects.filter(organization=org).order_by("status", "-opportunity_score", "slug"))
        if not islands:
            self.stdout.write(f"{org.domain}: no islands yet")
            return

        self.stdout.write(f"{org.domain}: {len(islands)} island(s)")
        for island in islands:
            self.stdout.write(
                "  {status:<9} {slug:<40} kw={keyword_count:<4} vol={total_volume:<7} "
                "opp={opportunity_score:<10.1f} ai={ai_search_volume:<6} articles={articles_written} "
                "misses={consecutive_misses} color={color_key} icon={icon_key} origin={origin}".format(
                    status=island.status,
                    slug=island.slug,
                    keyword_count=island.keyword_count,
                    total_volume=island.total_volume,
                    opportunity_score=island.opportunity_score,
                    ai_search_volume=island.ai_search_volume,
                    articles_written=island.articles_written,
                    consecutive_misses=island.consecutive_misses,
                    color_key=island.color_key,
                    icon_key=island.icon_key,
                    origin=island.origin,
                )
            )

        edges = list(
            ContentIslandEdge.objects.filter(organization=org)
            .select_related("island_a", "island_b")
            .order_by("-similarity")
        )
        self.stdout.write(f"{org.domain}: {len(edges)} edge(s)")
        for edge in edges:
            self.stdout.write(
                f"  {edge.island_a.slug} ~ {edge.island_b.slug} ({edge.similarity:.3f})"
            )
