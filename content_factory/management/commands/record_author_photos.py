"""Record the SHA-256 of author photos saved before AuthorPhoto records existed.

Article review accepts the author card's photo only when its bytes match a
record. Photos already in MLAI storage are fetched once, hashed exactly as
served, and recorded as ``legacy``; the author profile then carries the
record's ID and hash. External links are listed for ``copy_author_photos``,
which now records its copies. A row a founder saved meanwhile is left alone.
"""
import logging

from django.core.management.base import BaseCommand

from content_factory.author_photos import (AuthorPhotoError, attach_photo_records, fetch_photo, needs_copy,
                                           record_photo, storage_path_for)
from content_factory.authors import normalize_authors
from content_factory.models import AuthorPhoto, OrganizationContentConfig

logger = logging.getLogger(__name__)


class Command(BaseCommand):
    help = "List author photos without a record. Use --apply to hash and record them."

    def add_arguments(self, parser):
        parser.add_argument("--organization-id", type=int)
        parser.add_argument("--apply", action="store_true")

    def handle(self, *args, **options):
        apply = options["apply"]
        configs = OrganizationContentConfig.objects.exclude(authors=[])
        if options["organization_id"] is not None:
            configs = configs.filter(organization_id=options["organization_id"])
        missing_total = recorded = updated = external = failed = changed = 0
        for config in configs.only("pk", "organization_id", "authors").order_by("organization_id").iterator(chunk_size=100):
            authors = normalize_authors(config.authors)
            org = config.organization_id
            known = set(AuthorPhoto.objects.filter(organization_id=org).values_list("url", flat=True))
            missing = [a for a in authors if a["avatarUrl"] and storage_path_for(a["avatarUrl"]) and a["avatarUrl"] not in known]
            outside = [a for a in authors if needs_copy(a["avatarUrl"])]
            external += len(outside)
            missing_total += len(missing)
            for author in outside:
                self.stdout.write(f"org {org}: author {author['id']} photo is an external link; run copy_author_photos first")
            if not apply:
                if missing:
                    self.stdout.write(f"org {org}: {len(missing)} stored photos would be recorded")
                continue
            for author in missing:
                try:
                    data = fetch_photo(author["avatarUrl"])
                except AuthorPhotoError as exc:
                    failed += 1
                    self.stderr.write(f"org {org}: author {author['id']} photo couldn't be fetched ({exc.code})")
                    continue
                record_photo(data, url=author["avatarUrl"], organization_id=org, source="legacy")
                recorded += 1
            attached = attach_photo_records(authors, organization_id=org)
            if attached == authors:
                continue
            if OrganizationContentConfig.objects.filter(pk=config.pk, authors=config.authors).update(authors=attached):
                updated += 1
                self.stdout.write(f"org {org}: linked author photos to their records")
            else:
                changed += 1
                self.stderr.write(f"org {org}: authors changed meanwhile; skipped, run again to retry")
        if not apply:
            self.stdout.write(f"Dry run: {missing_total} stored photos without a record; {external} external links "
                              "need copy_author_photos. Run again with --apply to record them.")
            return
        self.stdout.write(f"Recorded {recorded} of {missing_total} photos; updated {updated} orgs; "
                          f"{failed} fetch failures; {changed} rows changed meanwhile; "
                          f"{external} external links need copy_author_photos.")
