"""Database-free contracts for source selection, deletion and completion receipts."""
from types import SimpleNamespace as Obj
from unittest.mock import MagicMock, patch

from django.http import Http404
from django.test import SimpleTestCase
from rest_framework.exceptions import ValidationError

from community_chat.startups import lifecycle
from community_chat.startups import connections
from startup_updates import completion
from startup_updates.revisions import RevisionConflict


class SourceCapabilityTests(SimpleTestCase):
    def test_every_supported_provider_has_an_honest_connect_mode(self):
        for provider in lifecycle.UPDATE_PROVIDERS:
            source = lifecycle.source_capabilities({"key": provider, "configured": True, "status": "not_connected"})
            self.assertEqual(source["connectMode"], "api_key" if provider in {"luma", "humanitix"} else "oauth")
            self.assertTrue(source["canConnect"])
            self.assertFalse(source["canDisconnect"])
            self.assertFalse(source["usableForUpdates"])

    def test_automatic_resource_scope_does_not_require_manual_selections(self):
        source = lifecycle.source_capabilities({"key": "google_analytics", "status": "connected", "connectionId": 8, "selected": False})
        self.assertTrue(source["usableForUpdates"])
        self.assertTrue(source["enabled"])
        self.assertTrue(source["canDisconnect"])
        unavailable = lifecycle.source_capabilities({"key": "slack", "configured": False, "status": "unavailable"})
        self.assertFalse(unavailable["canConnect"])

    def test_empty_or_unknown_selection_cannot_fall_back_to_gmail(self):
        for sources in (None, [], "slack", ["github"], [{}]):
            with self.subTest(sources=sources), self.assertRaises(ValidationError):
                lifecycle.validate_generation_sources({"inputSources": sources})
        lifecycle.validate_generation_sources({"inputSources": ["slack", "manual_documents"]})

    def test_notes_only_generation_uses_manual_source_without_gmail(self):
        from vibe_raising.views import _include_manual_source_if_needed
        lifecycle.validate_generation_sources({"inputSources": [], "manualSummary": "We shipped today."})
        self.assertEqual(_include_manual_source_if_needed([], manual_document_ids=[], manual_summary="We shipped today."), ["manual_documents"])


class DeleteUpdateTests(SimpleTestCase):
    def invoke(self, *, current_id=9, revision_id=9, revision_hash="current", active=False, foreign=False):
        organization = Obj(pk=5, domain="owned.example")
        draft = MagicMock(current_revision=Obj(pk=current_id, content_hash="current") if current_id else None)
        draft.revisions.values_list.return_value = [12, 13]
        with patch.object(lifecycle.Organization, "objects"), patch.object(lifecycle.ContentFactoryRun, "objects") as runs, patch.object(lifecycle, "get_object_or_404") as lookup, patch.object(lifecycle.MonthlyEvidenceSnapshot, "objects") as snapshots:
            runs.filter.return_value.exists.return_value = active
            lookup.side_effect = Http404 if foreign else None
            lookup.return_value = draft
            lifecycle.delete_update.__wrapped__(organization=organization, update_id=42, revision_id=revision_id, revision_hash=revision_hash)
        return draft, lookup, snapshots

    def test_delete_is_scoped_and_only_removes_unreferenced_snapshots(self):
        draft, lookup, snapshots = self.invoke()
        self.assertEqual(lookup.call_args.kwargs["pk"], 42)
        self.assertEqual(lookup.call_args.kwargs["organization"].domain, "owned.example")
        draft.delete.assert_called_once()
        self.assertTrue(snapshots.filter.call_args.kwargs["monthlyupdaterevision__isnull"])
        self.assertEqual(snapshots.filter.call_args.kwargs["pk__in"], [12, 13])

    def test_stale_revision_and_active_writer_are_conflicts(self):
        for overrides in ({"revision_id": 8}, {"revision_hash": "stale"}, {"active": True}):
            with self.subTest(overrides=overrides), self.assertRaises(RevisionConflict):
                self.invoke(**overrides)

    def test_foreign_update_is_hidden(self):
        with self.assertRaises(Http404):
            self.invoke(foreign=True)

    def test_legacy_update_can_be_deleted_without_a_revision(self):
        self.invoke(current_id=None, revision_id=None, revision_hash=None)


class ConnectionActionsTests(SimpleTestCase):
    def test_each_oauth_provider_gets_a_scoped_browser_ticket(self):
        from django.core import signing
        from urllib.parse import parse_qs, urlsplit
        view = connections.ConnectView()
        view.company = Obj(pk="company")
        request = Obj(user=Obj(pk=7), auth=Obj(pk="session"), build_absolute_uri=lambda path: "https://api.example" + path)
        for provider in connections.PROVIDERS:
            response = view.post(request, provider)
            ticket = parse_qs(urlsplit(response.data["authorizationUrl"]).query)["ticket"][0]
            payload = signing.loads(ticket, salt=connections.SALT)
            self.assertEqual((payload["company"], payload["provider"], payload["session"]), ("company", provider, "session"))

    def test_api_key_connectors_delegate_to_existing_validated_scoped_views(self):
        view = connections.ConnectView()
        request = Obj(data={"companyId": "owned", "apiKey": "synthetic-test-key"})
        for provider, name in (("luma", "LumaConnectView"), ("humanitix", "HumanitixConnectView")):
            with patch.object(connections, name) as handler:
                view.post(request, provider)
                handler.return_value.post.assert_called_once_with(request)

    def test_disconnect_cannot_touch_a_sibling_startup(self):
        view = connections.DisconnectView()
        view.company = Obj(organization=Obj(pk="owned"))
        request = Obj(user=Obj(pk=7))
        with patch.object(connections.ExternalServiceConnection, "objects") as manager, patch.object(connections, "disconnect_external_connection") as disconnect:
            manager.filter.return_value.exclude.return_value.values_list.return_value = [12]
            response = view.delete(request, "slack")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(manager.filter.call_args.kwargs, {"user": request.user, "organization": view.company.organization, "provider": "slack"})
        disconnect.assert_called_once_with(request.user, 12)


class SavedSourceSelectionTests(SimpleTestCase):
    def test_save_selection_can_be_explicitly_empty_without_changing_evidence(self):
        from vibe_raising.serializers import VibeRaisingMonthlyUpdateUpsertSerializer
        from vibe_raising.views import _build_manual_structured_memo
        for sources in ([], ["slack", "gmail"]):
            serializer = VibeRaisingMonthlyUpdateUpsertSerializer(data={"month": "September", "year": 2026, "inputSources": sources})
            self.assertTrue(serializer.is_valid(), serializer.errors)
            memo = _build_manual_structured_memo(serializer.validated_data)
            self.assertEqual(memo["selected_input_sources"], sources)
            self.assertNotIn("source_providers", memo)


class CompletionReceiptTests(SimpleTestCase):
    def test_receipt_is_deduplicated_and_never_claims_push_delivery(self):
        run = Obj(pk=1, run_id="run-1", workflow="startup_monthly_update", status="completed", result={}, save=MagicMock())
        with patch.object(completion.ContentFactoryRun, "objects") as runs, patch.object(completion.MonthlyUpdateDraft, "objects") as drafts:
            runs.select_for_update.return_value.get.return_value = run
            drafts.filter.return_value.order_by.return_value.first.return_value = Obj(pk=42, current_revision_id=9)
            receipt = completion.record_completion.__wrapped__(run)
            again = completion.record_completion.__wrapped__(run)
        self.assertEqual(receipt, again)
        self.assertEqual(receipt["updateId"], 42)
        self.assertEqual(receipt["deliveryState"], "unavailable")
        run.save.assert_called_once()
        self.assertNotIn("summary", receipt)

    def test_failed_run_never_records_a_ready_receipt(self):
        self.assertIsNone(completion.record_completion.__wrapped__(Obj(workflow="startup_monthly_update", status="failed")))
