"""Account isolation, explicit report choice and verified Gmail reconnects."""
from unittest.mock import patch
from django.contrib.auth import get_user_model
from django.test import TestCase
from integrations.models import ExternalServiceConnection, GoogleConnection
from integrations.services.external_connectors import _upsert_connection, google_connection_for_org, _serialize_external_source
from integrations.services.source_accounts import report_account_id, select_report_account, store_google_account
from organizations.models import Organization


class SourceAccountsTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(email="accounts@example.invalid", password="synthetic")
        self.other = get_user_model().objects.create_user(email="other@example.invalid", password="synthetic")
        self.org = Organization.objects.create(name="Alpha", domain="alpha.invalid")
        self.other_org = Organization.objects.create(name="Beta", domain="beta.invalid")

    def external(self, identity, **overrides):
        values = dict(user=self.user, organization=self.org, provider="notion", external_account_id=identity,
            access_token="token-"+identity, refresh_token="", token_type="Bearer", token_expires_at=None,
            scopes=[], account_label=identity)
        values.update(overrides)
        return _upsert_connection(**values)

    def gmail(self, subject, email, token="token", **overrides):
        values = dict(user=self.user, organization=self.org, userinfo={"sub":subject,"email":email,"email_verified":True},
            token_data={"refresh_token":token,"scope":"openid https://www.googleapis.com/auth/gmail.readonly"})
        values.update(overrides)
        return store_google_account(**values)

    def test_add_account_preserves_report_default_and_metadata(self):
        a = self.external("a")
        a.provider_metadata = {"selected_pages": ["page-a"]}
        a.save()
        b = self.external("b")
        self.assertEqual(report_account_id(self.user, self.org, "notion"), a.pk)
        self.assertEqual(_serialize_external_source(self.user, "notion", self.org)["connectionId"], a.pk)
        select_report_account(self.user, self.org, "notion", b.pk)
        self.external("a")
        result = _serialize_external_source(self.user, "notion", self.org)
        self.assertEqual(result["connectionId"], b.pk)
        self.assertEqual(len(result["accounts"]), 2)
        self.assertNotIn("token", str(result))
        a.refresh_from_db()
        self.assertEqual(a.provider_metadata["selected_pages"], ["page-a"])

    def test_reconnect_pins_legacy_default_before_touching_an_older_account(self):
        for identity in ("legacy-a", "legacy-b"):
            ExternalServiceConnection.objects.create(user=self.user, organization=self.org,
                provider="notion", external_account_id=identity, status="connected")
        current = ExternalServiceConnection.objects.get(external_account_id="legacy-b")
        self.external("legacy-a")
        self.assertEqual(report_account_id(self.user, self.org, "notion"), current.pk)

    def test_disconnected_report_choice_never_falls_back_to_sibling(self):
        a = self.external("a")
        b = self.external("b")
        select_report_account(self.user, self.org, "notion", b.pk)
        b.status = "disconnected"
        b.save()
        result = _serialize_external_source(self.user, "notion", self.org)
        self.assertIsNone(result["connectionId"])
        self.assertTrue(result["reportAccountMissing"])
        self.assertEqual([r["connectionId"] for r in result["accounts"]], [a.pk])

    def test_foreign_user_org_provider_and_bool_selections_rejected(self):
        rows = [self.external("other-user", user=self.other), self.external("other-org", organization=self.other_org),
                self.external("other-provider", provider="stripe")]
        for value in [True, "1", 0, *[r.pk for r in rows]]:
            with self.subTest(value=value), self.assertRaises(ValueError):
                select_report_account(self.user, self.org, "notion", value)

    def test_gmail_legacy_adoption_and_new_account_do_not_borrow_tokens(self):
        legacy = GoogleConnection.objects.create(user=self.user, organization=self.org, google_email="first@example.invalid", refresh_token="legacy")
        adopted = self.gmail("sub-a", "FIRST@example.invalid", token="")
        self.assertEqual(adopted.pk, legacy.pk)
        self.assertEqual(adopted.refresh_token, "legacy")
        with self.assertRaises(ValueError):
            self.gmail("sub-b", "second@example.invalid", token="")
        second = self.gmail("sub-b", "second@example.invalid", token="second")
        self.assertEqual(google_connection_for_org(self.user, self.org).pk, legacy.pk)
        select_report_account(self.user, self.org, "gmail", second.pk)
        reconnected = self.gmail("sub-a", "renamed@example.invalid", token="")
        self.assertEqual(reconnected.pk, legacy.pk)
        self.assertEqual(reconnected.refresh_token, "legacy")
        self.assertEqual(google_connection_for_org(self.user, self.org).pk, second.pk)

    def test_gmail_unverified_identity_is_rejected(self):
        for info in [{"email":"one@example.invalid"}, {"sub":"a","email":"one@example.invalid","email_verified":False}]:
            with self.subTest(info=info), self.assertRaises(ValueError):
                self.gmail("ignored", "ignored@example.invalid", userinfo=info)
        self.assertEqual(GoogleConnection.objects.count(), 0)

    def test_disconnect_requires_a_specific_account_and_preserves_sibling(self):
        from types import SimpleNamespace
        from community_chat.startups.connections import DisconnectView
        from rest_framework.exceptions import ValidationError
        a, b = self.external("a"), self.external("b")
        view = DisconnectView()
        view.company = SimpleNamespace(organization=self.org)
        request = SimpleNamespace(user=self.user, data={})
        with self.assertRaises(ValidationError):
            view.delete(request, "notion")
        request.data = {"connectionId": b.pk}
        view.delete(request, "notion")
        a.refresh_from_db(); b.refresh_from_db()
        self.assertEqual(a.access_token, "token-a")
        self.assertEqual(a.status, "connected")
        self.assertEqual(b.status, "disconnected")
        self.assertEqual(b.access_token, "")

    def test_xero_tenants_are_separate_and_rotate_only_their_shared_grant(self):
        from datetime import timedelta
        from django.utils import timezone
        from integrations.services.external_connectors import _store_xero_connection, _refresh_xero_connection
        unrelated = self.external("unrelated", provider="xero", refresh_token="separate")
        with patch("integrations.services.external_connectors._fetch_xero_connections", return_value=[
            {"tenantId":"tenant-a","tenantName":"A"}, {"tenantId":"tenant-b","tenantName":"B"}]), patch("integrations.services.external_connectors._xero_oauth_scope_list", return_value=[]):
            _store_xero_connection(self.user, self.org, {"access_token":"shared-access", "refresh_token":"shared-refresh", "expires_in":3600})
        a = ExternalServiceConnection.objects.get(external_account_id="tenant-a")
        b = ExternalServiceConnection.objects.get(external_account_id="tenant-b")
        a.token_expires_at = b.token_expires_at = timezone.now()-timedelta(minutes=1)
        a.save(); b.save()
        with patch("integrations.services.external_connectors.requests.post") as post:
            post.return_value.json.return_value = {"access_token":"rotated-access", "refresh_token":"rotated-refresh", "expires_in":3600}
            self.assertEqual(_refresh_xero_connection(a), "rotated-access")
            self.assertEqual(_refresh_xero_connection(b), "rotated-access")
            post.assert_called_once()
        unrelated.refresh_from_db()
        self.assertEqual(unrelated.refresh_token, "separate")
        self.assertEqual(report_account_id(self.user, self.org, "xero"), unrelated.pk)

    def test_crm_selection_requires_admin_and_never_changes_report_choice(self):
        from community_chat.admin_access import AdminError
        from crm.source_accounts import select_crm_source, selected_luma_sources
        first, second = self.external("cal-a", provider="luma"), self.external("cal-b", provider="luma")
        self.assertEqual(selected_luma_sources(), [])
        with self.assertRaises(AdminError):
            select_crm_source(user=self.user, organization=self.org, provider="luma", connection_id=second.pk, enabled=True)
        self.user.is_superuser = True
        self.user.save()
        select_crm_source(user=self.user, organization=self.org, provider="luma", connection_id=second.pk, enabled=True)
        self.assertEqual([row.pk for row in selected_luma_sources()], [second.pk])
        self.assertEqual(report_account_id(self.user, self.org, "luma"), first.pk)
        self.user.is_superuser = False
        self.user.save()
        self.assertEqual(selected_luma_sources(), [])

    def test_luma_metrics_do_not_replace_another_calendar(self):
        from datetime import datetime, timezone
        from integrations.services.luma_sync import publish_luma_event_metrics
        from startup_updates.models import StartupMetricObservation
        a, b = self.external("cal-a", provider="luma"), self.external("cal-b", provider="luma")
        item = {"event":{"id":"evt-a"}, "start_at":datetime(2026,10,1, tzinfo=timezone.utc),
            "registration_count":5, "checked_in_count":3}
        publish_luma_event_metrics(organization=self.org, connection_id=a.pk, events=[item])
        publish_luma_event_metrics(organization=self.org, connection_id=b.pk, events=[{**item,"registration_count":7}])
        self.assertEqual(StartupMetricObservation.objects.filter(source_provider="luma").count(),8)
        self.assertEqual(StartupMetricObservation.objects.get(source_metadata__connection_id=a.pk, metric_key="eventRegistrations").value_number,5)

    def test_existing_run_uses_its_pin_after_default_changes_and_fails_closed(self):
        from types import SimpleNamespace
        from integrations.services.source_accounts import connection_for_run, report_account_scope
        a, b = self.external("a"), self.external("b")
        run = SimpleNamespace(run_request={"source_connection_pins": {"notion": a.pk}})
        select_report_account(self.user, self.org, "notion", b.pk)
        self.assertEqual(connection_for_run(run, self.user, self.org, "notion").pk, a.pk)
        with report_account_scope(self.user, self.org, run.run_request["source_connection_pins"]):
            self.assertEqual(report_account_id(self.user, self.org, "notion"), a.pk)
            self.assertEqual(report_account_id(self.user, self.org, "xero"), 0)
            self.assertIsNone(report_account_id(self.other, self.org, "notion"))
        self.assertEqual(report_account_id(self.user, self.org, "notion"), b.pk)
        a.status = "disconnected"
        a.save()
        self.assertIsNone(connection_for_run(run, self.user, self.org, "notion"))

    def test_legacy_implicit_organization_is_unambiguous_and_never_overrides_explicit_scope(self):
        a = self.external("a")
        self.assertEqual(_serialize_external_source(self.user, "notion")["connectionId"], a.pk)
        self.assertIsNone(_serialize_external_source(self.user, "notion", None)["connectionId"])
        self.external("b", organization=self.other_org)
        self.assertIsNone(_serialize_external_source(self.user, "notion")["connectionId"])
        self.assertEqual(_serialize_external_source(self.user, "notion", self.org)["connectionId"], a.pk)

    def test_legacy_metrics_are_not_adopted_when_a_sibling_identity_exists(self):
        from integrations.services.source_accounts import metric_account_filter
        from startup_updates.models import StartupMetricObservation
        a = self.external("cal-a", provider="luma")
        metric = StartupMetricObservation.objects.create(organization=self.org, metric_key="eventsRun",
            source_provider="luma", value_text="5", period_month="2026-10-01")
        queryset = StartupMetricObservation.objects.filter(organization=self.org, source_provider="luma")
        self.assertTrue(queryset.filter(metric_account_filter(self.org, "luma", a.pk)).filter(pk=metric.pk).exists())
        b = self.external("cal-b", provider="luma")
        b.status = "disconnected"
        b.save()
        self.assertFalse(queryset.filter(metric_account_filter(self.org, "luma", a.pk)).filter(pk=metric.pk).exists())

    def test_preflight_workers_receive_the_saved_account_choices(self):
        from startup_updates.preflight import require_source_preflight
        a, b = self.external("a"), self.external("b")
        select_report_account(self.user, self.org, "notion", b.pk)
        def probe(provider, *, user, organization):
            return {"connectionId": report_account_id(user, organization, provider)}
        with patch("startup_updates.preflight.probe_source", side_effect=probe):
            checks = require_source_preflight(user=self.user, organization=self.org,
                input_sources=["notion"], connection_pins={"notion": a.pk})
        self.assertEqual(checks[0]["connectionId"], a.pk)
        self.assertEqual(report_account_id(self.user, self.org, "notion"), b.pk)

    def test_refresh_context_keeps_existing_notion_account_after_default_switch(self):
        from datetime import date
        from startup_updates.models import UserStartupBinding
        from startup_updates.services import refresh_startup_update_run_source_context
        from workflow_runs.models import ContentFactoryRun
        a, b = self.external("a"), self.external("b")
        binding = UserStartupBinding.objects.create(user=self.user, organization=self.org)
        run = ContentFactoryRun.objects.create(run_id="account-pinned-refresh", workflow="startup_monthly_update",
            organization=self.org, domain=self.org.domain, run_request={"binding_id": binding.pk,
                "input_sources": ["notion"], "notion_connection_id": a.pk,
                "source_connection_pins": {"notion": a.pk}})
        select_report_account(self.user, self.org, "notion", b.pk)
        refresh_startup_update_run_source_context(run=run, organization=self.org, input_sources=["notion"],
            start_date=date(2026,10,1), end_date=date(2026,10,31))
        run.refresh_from_db()
        self.assertEqual(run.run_request["notion_connection_id"], a.pk)
        self.assertEqual(run.run_request["external_context"]["notion"]["connection_id"], a.pk)
