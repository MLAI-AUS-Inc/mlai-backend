"""Copy author photo links saved before photo storage existed into MLAI storage.

Saving an author profile now copies each new photo link into Firebase Storage,
but links saved earlier (mostly signed ``media.licdn.com`` URLs, which expire
and are blocked by chat.mlai.au's CSP) stay external until someone re-saves the
author list. This copies them in place. A link that can't be copied is kept as
it is, and a row a founder saved while the copy ran is left alone.
"""
import logging
from collections import Counter

from django.core.management.base import BaseCommand

from content_factory.author_photos import needs_copy, store_author_photos
from content_factory.authors import normalize_authors
from content_factory.models import OrganizationContentConfig

logger = logging.getLogger(__name__)


class Command(BaseCommand):
    help = ("List saved author photo links that still point outside MLAI storage. "
            "Use --apply to copy them into Firebase Storage.")

    def add_arguments(self, parser):
        parser.add_argument("--organization-id", type=int)
        parser.add_argument("--apply", action="store_true")

    def handle(self, *args, **options):
        apply = options["apply"]
        configs = OrganizationContentConfig.objects.exclude(authors=[])
        if options["organization_id"] is not None:
            configs = configs.filter(organization_id=options["organization_id"])
        scanned = external_total = copied = changed = failed = 0
        kept = Counter()
        for config in configs.only("pk", "organization_id", "authors").order_by("organization_id").iterator(
                chunk_size=100):
            authors = normalize_authors(config.authors)
            if not authors:
                continue
            scanned += 1
            external = sum(1 for author in authors if needs_copy(author["avatarUrl"]))
            external_total += external
            label = f"org {config.organization_id}"
            self.stdout.write(f"{label}: {len(authors)} authors, {external} external photo links")
            if not apply or not external:
                continue
            skipped = []
            try:
                # previous=authors marks every link as already saved, so a link that
                # can't be copied is kept and logged instead of raising.
                stored = store_author_photos(authors, previous=authors,
                                             company_id=f"org-{config.organization_id}", skipped=skipped)
            except Exception:
                logger.exception("author_photo_backfill_failed organization_id=%s", config.organization_id)
                failed += 1
                self.stderr.write(f"{label}: copy failed; unchanged")
                continue
            kept.update(error.code for error in skipped)
            for error in skipped:
                self.stdout.write(f"{label}: kept author {error.author_id} photo ({error.code})")
            done = sum(1 for before, after in zip(authors, stored) if before["avatarUrl"] != after["avatarUrl"])
            if not done:
                continue
            if OrganizationContentConfig.objects.filter(pk=config.pk, authors=config.authors).update(authors=stored):
                copied += done
                self.stdout.write(f"{label}: copied {done} photos")
            else:
                changed += 1
                self.stderr.write(f"{label}: authors changed during the copy; skipped, run again to retry")

        if not apply:
            self.stdout.write(f"Dry run: scanned {scanned} orgs with authors; {external_total} external photo links "
                              "would be copied. Run again with --apply to copy them.")
            return
        kept_detail = ", ".join(f"{code}: {count}" for code, count in sorted(kept.items()))
        summary = (f"Scanned {scanned} orgs with authors; copied {copied} photos; "
                   f"kept {sum(kept.values())} photos that couldn't be copied"
                   f"{f' ({kept_detail})' if kept_detail else ''}; "
                   f"skipped {changed} rows that changed during the copy")
        if failed:
            summary += f"; {failed} orgs failed and are unchanged"
        self.stdout.write(f"{summary}.")
