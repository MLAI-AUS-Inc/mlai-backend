"""Independent monthly identities and owner reads without database/provider access."""
from datetime import date
from types import SimpleNamespace as Obj
from unittest.mock import MagicMock, patch
from uuid import UUID, uuid4

from django.test import SimpleTestCase
from rest_framework.exceptions import NotFound, ValidationError

from community_chat.startups import views
from startup_updates import update_identity
from startup_updates.monthly_groups import requested_month
from startup_updates.revisions import RevisionConflict


class MonthlyIdentityTests(SimpleTestCase):
    def setUp(self):
        self.organization = Obj(pk=7)
        self.month = date(2026, 9, 1)

    def resolve(self, *, found=None, current=None, allocated=0, **kwargs):
        with patch.object(update_identity.Organization, "objects") as orgs, patch.object(
            update_identity.MonthlyUpdateDraft, "objects"
        ) as drafts, patch.object(update_identity, "allocate_monthly_titles", return_value=allocated), patch.object(
            update_identity, "latest_monthly_draft", return_value=current
        ):
            query = drafts.filter.return_value
            query.filter.return_value.first.return_value = found
            query.create.return_value = Obj(pk=10, month=self.month)
            result = update_identity.resolve_update.__wrapped__(self.organization, month=self.month, **kwargs)
            orgs.select_for_update.return_value.get.assert_called_once_with(pk=7)
            drafts.filter.assert_called_once_with(organization=self.organization)
            return result, query

    def test_different_creation_key_allocates_numbered_independent_copy(self):
        key = str(uuid4())
        (_, created), query = self.resolve(current=Obj(pk=19), allocated=2, creation_key=key)
        self.assertTrue(created)
        query.create.assert_called_once_with(organization=self.organization, month=self.month,
            creation_key=UUID(key), update_date=None, title="September update #3",
            structured_memo={"_month_sequence": 3})

    def test_creation_retry_returns_same_identity_without_inserting(self):
        draft = Obj(pk=19, month=self.month, refresh_from_db=MagicMock())
        (result, created), query = self.resolve(found=draft, creation_key=str(uuid4()))
        self.assertEqual(result, draft)
        self.assertFalse(created)
        query.create.assert_not_called()

    def test_creation_key_cannot_be_reused_for_another_month(self):
        with self.assertRaises(RevisionConflict):
            self.resolve(found=Obj(pk=19, month=date(2026, 8, 1)), creation_key=str(uuid4()))

    def test_month_only_compatibility_clients_resume_current_copy(self):
        draft = Obj(pk=19, month=self.month)
        (result, created), query = self.resolve(current=draft)
        self.assertEqual(result, draft)
        self.assertFalse(created)
        query.create.assert_not_called()

    def test_editing_earlier_record_targets_that_id(self):
        requested = Obj(pk=12, month=self.month, refresh_from_db=MagicMock())
        (result, created), query = self.resolve(found=requested, current=Obj(pk=19), update_id=12)
        self.assertEqual(result.pk, 12)
        self.assertFalse(created)
        query.create.assert_not_called()

    def test_foreign_update_is_not_resolved_by_month(self):
        with self.assertRaises(NotFound):
            self.resolve(current=Obj(pk=19), update_id=999)

    def test_invalid_creation_key_is_rejected(self):
        with self.assertRaises(ValidationError):
            self.resolve(creation_key="invalid")

    def test_month_queries_require_month_precision_and_preserve_year(self):
        for value in ("2026-09", "2026-09-01"):
            self.assertEqual(requested_month(value), self.month)
        self.assertEqual(requested_month("2025-09"), date(2025, 9, 1))
        for value in ("2026-09-26", "September", "2026-13", "20260901", "2026-09-01-extra"):
            with self.subTest(value=value), self.assertRaises(ValidationError):
                requested_month(value)

    def test_worker_updates_its_earlier_id_even_when_newer_sibling_exists(self):
        draft = Obj(pk=12, month=self.month)
        run = Obj(run_request={"organization_id": 7, "update_id": 12})
        with patch.object(update_identity.MonthlyUpdateDraft, "objects") as rows:
            rows.filter.return_value.filter.return_value.first.return_value = draft
            self.assertIs(update_identity.run_update.__wrapped__(run, self.month), draft)
            with self.assertRaises(RevisionConflict):
                update_identity.run_update.__wrapped__(run, date(2026, 8, 1))


class MonthlySequenceTests(SimpleTestCase):
    def row(self, pk, sequence=None):
        return Obj(pk=pk, month=date(2026, 10, 1), title="Old title", save=MagicMock(),
            structured_memo={"summary": "Keep this", **({"_month_sequence": sequence} if sequence is not None else {})})

    def test_legacy_numbers_follow_id_order_and_avoid_allocated_numbers(self):
        rows = [self.row(8), self.row(10, 2), self.row(12), self.row(15, 5)]
        self.assertEqual(update_identity.legacy_month_sequences(rows), {8: 1, 10: 2, 12: 3, 15: 5})

    def test_invalid_metadata_is_never_treated_as_a_sequence(self):
        for value in (False, True, "2", 0, -1, 1.5):
            self.assertIsNone(update_identity.saved_month_sequence(self.row(1, value)))

    def test_allocating_legacy_titles_preserves_content_and_existing_numbers(self):
        rows = [self.row(8), self.row(10, 2), self.row(12)]
        query = MagicMock()
        query.filter.return_value.select_for_update.return_value.order_by.return_value = rows
        self.assertEqual(update_identity.allocate_monthly_titles(query, rows[0].month), 3)
        query.filter.return_value.select_for_update.assert_called_once_with()
        self.assertEqual(rows[0].title, "October update")
        self.assertEqual(rows[2].title, "October update #3")
        self.assertEqual(rows[0].structured_memo, {"summary": "Keep this", "_month_sequence": 1})
        rows[1].save.assert_not_called()

    def test_manual_or_generated_memo_cannot_change_the_allocated_number(self):
        draft = self.row(12, 3)
        incoming = {"_month_sequence": 1, "highlights": ["Revised update"]}
        self.assertEqual(update_identity.memo_with_month_identity(draft, incoming),
            {"_month_sequence": 3, "highlights": ["Revised update"]})
        self.assertEqual(incoming["_month_sequence"], 1)

    def test_cancellation_of_older_backup_keeps_current_allocated_number(self):
        from startup_updates import services
        draft = self.row(12, 3)
        draft.current_revision_id = None
        snapshot = {"draft_id": 12, "month": "2026-10-01", "creation_key": str(uuid4()),
            "structured_memo": {"summary": "Before generation"}, "title": "Older title"}
        with patch.object(services.MonthlyUpdateDraft, "objects") as drafts, patch.object(
            services.ContentFactoryRun, "objects"
        ):
            drafts.filter.return_value.first.return_value = draft
            self.assertEqual(services._restore_cancelled_run_drafts(organization=Obj(pk=7), backups={"one": snapshot}), 1)
            defaults = drafts.update_or_create.call_args.kwargs["defaults"]
        self.assertEqual(defaults["structured_memo"], {"summary": "Before generation", "_month_sequence": 3})
        self.assertEqual(defaults["title"], "October update #3")
        self.assertNotIn("_month_sequence", snapshot["structured_memo"])

    def test_identity_does_not_change_after_lower_numbered_sibling_deletion(self):
        draft = self.row(12, 3)
        draft.update_date = draft.first_published_at = None
        draft.creation_key = None
        draft.published_at = None
        draft.published_revision_id = None
        with patch.object(update_identity.MonthlyUpdateDraft, "objects") as rows:
            payload = update_identity.identity_payload(draft)
            rows.filter.assert_not_called()
        self.assertEqual(payload["monthSequence"], 3)
        self.assertEqual(payload["updateTitle"], "October update #3")


class MonthlyOwnerReadTests(SimpleTestCase):
    def test_archive_retains_every_copy_and_filters_month_before_pagination(self):
        request = Obj(query_params={"month": "2026-09", "offset": "50"})
        view = views.UpdatesView()
        view.company = Obj(organization=Obj(pk=7))
        with patch.object(views.MonthlyUpdateDraft, "objects") as drafts:
            base = drafts.filter.return_value.select_related.return_value
            ordered = base.filter.return_value.order_by.return_value
            ordered.__getitem__.return_value = []
            response = view.get(request)
            drafts.filter.assert_called_once_with(organization=view.company.organization)
            base.filter.assert_called_once_with(month=date(2026, 9, 1))
            ordered.__getitem__.assert_called_once_with(slice(50, 101))
        self.assertEqual(response.data, {"updates": [], "nextOffset": None})

    def test_old_owner_link_opens_exact_update(self):
        old = Obj(pk=12, month=date(2026, 9, 1))
        newer = Obj(pk=19, month=old.month)
        view = views.UpdateView()
        view.company = Obj(organization=Obj(pk=7))
        with patch.object(views, "get_object_or_404", return_value=old), patch.object(
            views.MonthlyUpdateDraft, "objects"
        ) as drafts, patch.object(views, "update_payload", side_effect=lambda row, **kwargs: {"id": row.pk}):
            siblings = drafts.filter.return_value.select_related.return_value
            siblings.exclude.return_value.order_by.return_value = [newer]
            response = view.get(Obj(query_params={}), update_id=12)
        self.assertEqual(response.data["update"]["id"], 12)
        self.assertEqual(response.data["previousUpdates"], [{"id": 19}])

    def test_community_keeps_all_independently_approved_visible_updates(self):
        with patch.object(views.MonthlyUpdateDraft, "objects") as drafts:
            drafts.filter.return_value.select_related.return_value.order_by.return_value.__getitem__.return_value = []
            response = views.CommunityView().get(Obj(query_params={}))
            self.assertEqual(response.status_code, 200)
            filters = drafts.filter.call_args.kwargs
            disclosure = drafts.filter.call_args.args[0]
            clauses = [child.children for child in disclosure.children if hasattr(child, "children")]
            self.assertEqual(len(clauses), 2)
            for audience, clause in zip(("community", "public"), clauses):
                self.assertIn(("published_revision__audience", audience), clause)
                self.assertIn(("published_revision__approval__audience_visibility", [audience]), clause)
            self.assertIs(filters["published_at__isnull"], False)
            self.assertEqual(filters["published_revision__approval__content_hash"].name, "published_revision__content_hash")
