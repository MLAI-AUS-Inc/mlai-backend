"""The author photo backfill command with fake rows and photo storage."""
from io import StringIO
from types import SimpleNamespace as Obj
from unittest.mock import patch

from django.core.management import call_command
from django.test import SimpleTestCase

from content_factory import author_photos as photos
from content_factory.management.commands import copy_author_photos as command
from content_factory.models import OrganizationContentConfig

LINKEDIN_PHOTO = ("https://media.licdn.com/dms/image/v2/C5603AQ/profile-displayphoto-shrink_200_200/"
                  "0/1579726625398?e=1760000000&v=beta&t=signature")


def stored_url(name="photo"):
    return f"{photos._storage_url_prefix()}author-photos%2Forg-11%2F{name}.jpg?alt=media&token=t"


def config(pk, organization_id, *avatar_urls):
    authors = [{"id": f"author-{index}", "name": f"Author {index}", "avatarUrl": url}
               for index, url in enumerate(avatar_urls, start=1)]
    return Obj(pk=pk, organization_id=organization_id, authors=authors)


def copy_all(authors, *, previous, company_id, skipped):
    return [{**author, "avatarUrl": stored_url(author["id"])} if photos.needs_copy(author["avatarUrl"]) else author
            for author in authors]


def keep_all(authors, *, previous, company_id, skipped):
    for author in authors:
        if photos.needs_copy(author["avatarUrl"]):
            skipped.append(photos.AuthorPhotoError("author_photo_unavailable", "expired", author_id=author["id"]))
    return authors


class Rows:
    """Just enough of a queryset: records lookups and answers each update in turn."""

    def __init__(self, *configs, updates=()):
        self.configs = list(configs)
        self.updates = list(updates)
        self.lookups = []
        self.writes = []

    def exclude(self, **lookup):
        self.lookups.append(("exclude", lookup))
        return self

    def filter(self, **lookup):
        self.lookups.append(("filter", lookup))
        return self

    def only(self, *fields):
        return self

    def order_by(self, *fields):
        return self

    def iterator(self, chunk_size=None):
        return iter(self.configs)

    def update(self, **values):
        self.writes.append((self.lookups[-1][1], values))
        return self.updates.pop(0) if self.updates else 1


class CopyAuthorPhotosCommandTests(SimpleTestCase):
    def run_command(self, rows, *args, store=None):
        out, err = StringIO(), StringIO()
        with patch.object(OrganizationContentConfig, "objects", rows), \
             patch.object(command, "store_author_photos", side_effect=store) as store_photos:
            call_command("copy_author_photos", *args, stdout=out, stderr=err)
        return out.getvalue(), err.getvalue(), store_photos

    def test_dry_run_lists_each_org_and_changes_nothing(self):
        rows = Rows(config(1, 11, LINKEDIN_PHOTO, stored_url(), "/team/sam.jpg"), config(2, 12, LINKEDIN_PHOTO))
        out, _err, store_photos = self.run_command(rows)
        self.assertIn("org 11: 3 authors, 1 external photo links", out)
        self.assertIn("org 12: 1 authors, 1 external photo links", out)
        self.assertIn("Dry run: scanned 2 orgs with authors; 2 external photo links would be copied", out)
        self.assertEqual(rows.lookups, [("exclude", {"authors": []})])
        store_photos.assert_not_called()
        self.assertEqual(rows.writes, [])

    def test_apply_copies_links_with_an_optimistic_update(self):
        row = config(1, 11, LINKEDIN_PHOTO, "/team/sam.jpg")
        original = [dict(author) for author in row.authors]
        rows = Rows(row)
        out, _err, store_photos = self.run_command(rows, "--apply", store=copy_all)
        authors = store_photos.call_args.args[0]
        self.assertEqual(store_photos.call_args.kwargs["previous"], authors)
        self.assertEqual(store_photos.call_args.kwargs["company_id"], "org-11")
        (lookup, values), = rows.writes
        self.assertEqual(lookup, {"pk": 1, "authors": original})
        self.assertEqual([author["avatarUrl"] for author in values["authors"]],
                         [stored_url("author-1"), "/team/sam.jpg"])
        self.assertIn("org 11: copied 1 photos", out)
        self.assertIn("Scanned 1 orgs with authors; copied 1 photos; kept 0 photos that couldn't be copied; "
                      "skipped 0 rows that changed during the copy.", out)

    def test_links_that_cant_be_copied_are_kept_with_their_code(self):
        rows = Rows(config(1, 11, LINKEDIN_PHOTO, LINKEDIN_PHOTO))
        out, _err, _store = self.run_command(rows, "--apply", store=keep_all)
        self.assertEqual(rows.writes, [])
        self.assertIn("org 11: kept author author-1 photo (author_photo_unavailable)", out)
        self.assertIn("copied 0 photos; kept 2 photos that couldn't be copied (author_photo_unavailable: 2)", out)

    def test_row_saved_during_the_copy_is_skipped(self):
        rows = Rows(config(1, 11, LINKEDIN_PHOTO), config(2, 12, LINKEDIN_PHOTO), updates=[0, 1])
        out, err, _store = self.run_command(rows, "--apply", store=copy_all)
        self.assertEqual(len(rows.writes), 2)
        self.assertIn("org 11: authors changed during the copy; skipped", err)
        self.assertIn("copied 1 photos", out)
        self.assertIn("skipped 1 rows that changed during the copy", out)

    def test_orgs_without_external_links_are_not_copied(self):
        rows = Rows(config(1, 11, stored_url(), "", "/team/sam.jpg"), Obj(pk=2, organization_id=12, authors=[{}]))
        out, _err, store_photos = self.run_command(rows, "--apply")
        store_photos.assert_not_called()
        self.assertEqual(rows.writes, [])
        self.assertIn("org 11: 3 authors, 0 external photo links", out)
        self.assertNotIn("org 12", out)
        self.assertIn("Scanned 1 orgs with authors; copied 0 photos", out)

    def test_organization_id_limits_the_scan(self):
        rows = Rows(config(1, 11, LINKEDIN_PHOTO))
        self.run_command(rows, "--organization-id", "11")
        self.assertIn(("filter", {"organization_id": 11}), rows.lookups)

    def test_an_unexpected_failure_leaves_that_org_unchanged_and_continues(self):
        def flaky(authors, *, previous, company_id, skipped):
            if company_id == "org-11":
                raise RuntimeError("storage client misconfigured")
            return copy_all(authors, previous=previous, company_id=company_id, skipped=skipped)

        rows = Rows(config(1, 11, LINKEDIN_PHOTO), config(2, 12, LINKEDIN_PHOTO))
        with self.assertLogs(command.logger, "ERROR"):
            out, err, _store = self.run_command(rows, "--apply", store=flaky)
        self.assertEqual([lookup["pk"] for lookup, _values in rows.writes], [2])
        self.assertIn("org 11: copy failed; unchanged", err)
        self.assertIn("copied 1 photos", out)
        self.assertIn("1 orgs failed and are unchanged", out)


class StoreAuthorPhotosSkippedTests(SimpleTestCase):
    def test_kept_links_are_reported_and_new_links_still_raise(self):
        authors = [{"id": "gisele", "name": "Gisele", "avatarUrl": LINKEDIN_PHOTO}]
        expired = photos.AuthorPhotoError("author_photo_unavailable", "expired")
        skipped = []
        with patch.object(photos, "copy_photo", side_effect=expired):
            kept = photos.store_author_photos(authors, previous=authors, company_id="org-11", skipped=skipped)
            with self.assertRaises(photos.AuthorPhotoError):
                photos.store_author_photos(authors, previous=[], company_id="org-11", skipped=skipped)
        self.assertEqual(kept, authors)
        self.assertEqual([(error.code, error.author_id) for error in skipped],
                         [("author_photo_unavailable", "gisele")])
