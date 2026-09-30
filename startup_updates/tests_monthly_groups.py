"""Monthly identity and owner read contracts without a database or provider access."""
from datetime import date
from types import SimpleNamespace as Obj
from unittest.mock import patch
from uuid import uuid4

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

    def resolve(self, *, current=None, requested=None, **kwargs):
        with patch.object(update_identity.Organization, "objects") as orgs, patch.object(
            update_identity.MonthlyUpdateDraft, "objects"
        ) as drafts:
            query = drafts.filter.return_value
            query.filter.return_value.first.return_value = requested
            query.filter.return_value.order_by.return_value.first.return_value = current
            query.monthly_slots.return_value.get_or_create.return_value = (Obj(pk=10, month=self.month), True)
            result = update_identity.resolve_update.__wrapped__(self.organization, month=self.month, **kwargs)
            orgs.select_for_update.return_value.get.assert_called_once_with(pk=7)
            drafts.filter.assert_called_once_with(organization=self.organization)
            return result, query

    def test_different_creation_keys_resume_existing_month_without_inserting(self):
        current = Obj(pk=19, month=self.month)
        for key in (str(uuid4()), str(uuid4()), None):
            result, query = self.resolve(current=current, creation_key=key, update_date=date(2026, 10, 3))
            self.assertEqual(result, (current, False))
            query.monthly_slots.assert_not_called()
            self.assertIn((((), {"month": self.month})), [(call.args, call.kwargs) for call in query.filter.call_args_list])

    def test_new_month_uses_existing_unique_month_slot(self):
        (_, created), query = self.resolve(creation_key=str(uuid4()))
        self.assertTrue(created)
        query.monthly_slots.return_value.get_or_create.assert_called_once_with(organization=self.organization, month=self.month)

    def test_editing_earlier_record_cannot_overwrite_newer_month_content(self):
        with self.assertRaises(RevisionConflict):
            self.resolve(current=Obj(pk=19), requested=Obj(pk=12, month=self.month), update_id=12)

    def test_foreign_update_is_not_resolved_by_month(self):
        with self.assertRaises(NotFound):
            self.resolve(current=Obj(pk=19), update_id=999)

    def test_invalid_compatibility_key_is_rejected(self):
        with self.assertRaises(ValidationError):
            self.resolve(creation_key="invalid")

    def test_month_queries_require_month_precision_and_preserve_year(self):
        for value in ("2026-09", "2026-09-01"):
            self.assertEqual(requested_month(value), self.month)
        self.assertEqual(requested_month("2025-09"), date(2025, 9, 1))
        for value in ("2026-09-26", "September", "2026-13", "20260901", "2026-09-01-extra"):
            with self.subTest(value=value), self.assertRaises(ValidationError):
                requested_month(value)


class MonthlyOwnerReadTests(SimpleTestCase):
    def test_month_filter_and_grouping_happen_before_pagination(self):
        request = Obj(query_params={"month": "2026-09", "offset": "50"})
        view = views.UpdatesView()
        view.company = Obj(organization=Obj(pk=7))
        with patch.object(views.MonthlyUpdateDraft, "objects") as drafts, patch.object(views, "monthly_representatives") as group:
            grouped = group.return_value.order_by.return_value
            grouped.__getitem__.return_value = []
            response = view.get(request)
            drafts.filter.assert_called_once_with(organization=view.company.organization)
            base = drafts.filter.return_value.select_related.return_value
            base.filter.assert_called_once_with(month=date(2026, 9, 1))
            group.assert_called_once_with(base.filter.return_value)
            grouped.__getitem__.assert_called_once_with(slice(50, 101))
        self.assertEqual(response.data, {"updates": [], "nextOffset": None})

    def test_old_owner_link_opens_month_and_retains_prior_contents(self):
        old = Obj(pk=12, month=date(2026, 9, 1))
        latest = Obj(pk=19, month=old.month)
        view = views.UpdateView()
        view.company = Obj(organization=Obj(pk=7))
        with patch.object(views, "get_object_or_404", return_value=old), patch.object(
            views.MonthlyUpdateDraft, "objects"
        ) as drafts, patch.object(views, "latest_monthly_draft", return_value=latest), patch.object(
            views, "update_payload", side_effect=lambda row, **kwargs: {"id": row.pk}
        ):
            siblings = drafts.filter.return_value.select_related.return_value
            siblings.exclude.return_value.order_by.return_value = [old]
            response = view.get(Obj(query_params={}), update_id=12)
        self.assertEqual(response.data["update"]["id"], 19)
        self.assertEqual(response.data["previousUpdates"], [{"id": 12}])

    def test_community_projection_groups_only_approved_visible_rows(self):
        with patch.object(views.MonthlyUpdateDraft, "objects") as drafts, patch.object(views, "monthly_representatives") as group:
            group.return_value.__getitem__.return_value = []
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
            self.assertEqual(group.call_args.kwargs, {"published": True})
