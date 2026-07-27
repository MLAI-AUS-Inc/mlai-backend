from datetime import date, timedelta
from decimal import Decimal
from unittest.mock import Mock, patch

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.utils import timezone

from integrations.models import ExternalFinancialRecord, ExternalServiceConnection
from integrations.services.external_connectors import _upsert_xero_repeating_invoices
from integrations.services.finance import _upsert_stripe_subscriptions
from organizations.models import Organization
from org_memory.connectors.registry import MetadataOnlyMemoryConnector, connector_registry
from org_memory.connectors.structured_aggregates import StructuredAggregateMemoryConnector
from org_memory.control_plane import SourceControlError, _validate_scope
from org_memory.extraction import extract_source_version
from org_memory.models import (
    MemoryClaim,
    MemoryClaimKind,
    MemoryConnectionConfiguration,
    MemoryConnectionState,
    MemoryScopeStatus,
    MemorySource,
    MemorySourceLifecycle,
    MemorySourceScope,
    StructuredAggregateArtifact,
    StructuredAggregateState,
)
from org_memory.runtime import _apply_removal, _capture_record
from startup_updates.models import LumaEventSelection


@override_settings(
    ORG_MEMORY_STRUCTURED_PAGE_SIZE=2,
    ORG_MEMORY_STRUCTURED_BACKFILL_DAYS=730,
    ORG_MEMORY_STRUCTURED_STALE_SECONDS=90000,
    ORG_MEMORY_LUMA_TIMEZONE="Australia/Melbourne",
    ORG_MEMORY_STRUCTURED_DEBOUNCE_SECONDS=60,
)
class StructuredAggregateMemoryConnectorTests(TestCase):
    def setUp(self):
        self.organization = Organization.objects.create(
            name="Structured Adapter Org",
            domain="structured-adapter.mlai.test",
        )
        self.user = get_user_model().objects.create_user(
            email="structured-adapter@mlai.test"
        )

    def _configuration(self, provider, account_id, scopes):
        connection = ExternalServiceConnection.objects.create(
            provider=provider,
            user=self.user,
            organization=self.organization,
            external_account_id=account_id,
            account_label=f"{provider.title()} account",
            access_token=f"{provider}-secret-token",
        )
        configuration = MemoryConnectionConfiguration.objects.create(
            organization=self.organization,
            provider=provider,
            external_connection=connection,
            lifecycle_state=MemoryConnectionState.ACTIVE,
            created_by=self.user,
        )
        rows = []
        for scope_type, external_id in scopes:
            rows.append(
                MemorySourceScope.objects.create(
                    configuration=configuration,
                    scope_type=scope_type,
                    external_id=external_id,
                    name=external_id.replace("_", " ").title(),
                    selected=True,
                    status=MemoryScopeStatus.SELECTED,
                    default_classification=(
                        "finance" if provider in {"stripe", "xero"} else "committee"
                    ),
                )
            )
        return connection, configuration, rows

    @staticmethod
    def _finish_backfill(connector, configuration, scopes):
        records = []
        checkpoint = {}
        while True:
            page = connector.backfill(configuration, scopes, checkpoint)
            records.extend(page.records)
            if not page.has_more:
                return page, records
            checkpoint = page.checkpoint

    def _financial_record(
        self,
        connection,
        *,
        record_type,
        external_id,
        amount,
        status,
        transaction_date,
        category="",
        currency="AUD",
        raw_payload=None,
    ):
        return ExternalFinancialRecord.objects.create(
            provider=connection.provider,
            record_type=record_type,
            connection=connection,
            user=self.user,
            organization=self.organization,
            external_record_id=external_id,
            external_account_id=connection.external_account_id,
            currency=currency,
            amount=Decimal(str(amount)),
            status=status,
            transaction_date=transaction_date,
            category=category,
            description="private customer invoice description",
            merchant_name="private.customer@example.com",
            raw_payload=raw_payload
            or {
                "customer_email": "private.customer@example.com",
                "bank_account": "123-456",
            },
        )

    def test_registry_installs_only_explicit_structured_aggregate_adapters(self):
        for provider in ("stripe", "xero", "luma"):
            connector = connector_registry.get(provider)
            self.assertIsInstance(connector, StructuredAggregateMemoryConnector)
            self.assertNotIsInstance(connector, MetadataOnlyMemoryConnector)
            self.assertEqual(connector_registry.validate_conformance(provider), [])

        _connection, configuration, scopes = self._configuration(
            "stripe", "acct_registry", [("aggregate", "mrr")]
        )
        discovery = connector_registry.get("stripe").discover_scopes(configuration)
        self.assertTrue(discovery.scopes)
        self.assertTrue(all(row.scope_type == "aggregate" for row in discovery.scopes))
        self.assertTrue(all(row.metadata["aggregate_only"] for row in discovery.scopes))

        scopes[0].scope_type = "account"
        scopes[0].save(update_fields=("scope_type", "updated_at"))
        with self.assertRaisesMessage(ValueError, "explicit supported aggregate"):
            connector_registry.get("stripe").preview(configuration, scopes, None)
        with self.assertRaisesMessage(SourceControlError, "explicit aggregate"):
            _validate_scope(
                "stripe",
                {"scope_type": "account", "external_id": "acct_registry"},
            )
        with self.assertRaisesMessage(SourceControlError, "Unsupported xero"):
            _validate_scope(
                "xero",
                {"scope_type": "aggregate", "external_id": "bank_balance"},
            )

    def test_upstream_sync_persists_only_the_sanitized_recurring_inputs_needed(self):
        stripe, _stripe_configuration, _stripe_scopes = self._configuration(
            "stripe", "acct-normalization", [("aggregate", "mrr")]
        )
        _upsert_stripe_subscriptions(
            stripe,
            [
                {
                    "id": "sub-yearly",
                    "created": int(timezone.now().timestamp()),
                    "status": "active",
                    "customer": "cus_private",
                    "items": {
                        "data": [
                            {
                                "quantity": 1,
                                "price": {
                                    "unit_amount": 120000,
                                    "currency": "aud",
                                    "recurring": {
                                        "interval": "year",
                                        "interval_count": 1,
                                    },
                                },
                            }
                        ]
                    },
                }
            ],
        )
        stripe_record = ExternalFinancialRecord.objects.get(
            connection=stripe, external_record_id="sub-yearly"
        )
        self.assertEqual(stripe_record.amount, Decimal("100"))
        self.assertEqual(stripe_record.category, "monthly_normalized")
        self.assertEqual(stripe_record.class_name, "subscription_mrr")

        xero, _xero_configuration, _xero_scopes = self._configuration(
            "xero", "tenant-normalization", [("aggregate", "mrr")]
        )
        _upsert_xero_repeating_invoices(
            xero,
            [
                {
                    "RepeatingInvoiceID": "repeat-yearly",
                    "Type": "ACCREC",
                    "Status": "AUTHORISED",
                    "CurrencyCode": "AUD",
                    "Total": "1200.00",
                    "Contact": {"Name": "Private customer"},
                    "Schedule": {
                        "Unit": "YEAR",
                        "Period": 1,
                        "NextScheduledDate": date.today().isoformat(),
                    },
                }
            ],
        )
        xero_record = ExternalFinancialRecord.objects.get(
            connection=xero, external_record_id="repeat-yearly"
        )
        self.assertEqual(xero_record.amount, Decimal("1200"))
        self.assertEqual(xero_record.category, "recurrence:YEAR:1")

    @patch("org_memory.connectors.structured_aggregates.sync_stripe_connection")
    def test_stripe_backfill_emits_sanitized_monthly_metrics_and_reconciles(self, sync):
        connection, configuration, scopes = self._configuration(
            "stripe",
            "acct_stripe",
            [
                ("aggregate", "invoice_revenue"),
                ("aggregate", "cash_collected"),
                ("aggregate", "invoice_count"),
                ("aggregate", "mrr"),
                ("aggregate", "active_subscriptions"),
            ],
        )
        month = date.today().replace(day=1)
        self._financial_record(
            connection,
            record_type="stripe_invoice",
            external_id="in_private",
            amount="1200.00",
            status="paid",
            transaction_date=month,
        )
        self._financial_record(
            connection,
            record_type="stripe_subscription",
            external_id="sub_private",
            amount="250.00",
            status="active",
            transaction_date=date.today() - timedelta(days=900),
            category="monthly_normalized",
        )

        connector = connector_registry.get("stripe")
        final_page, records = self._finish_backfill(connector, configuration, scopes)

        self.assertGreater(len(records), 2)
        self.assertEqual(sync.call_count, 1)
        self.assertIsNotNone(final_page.next_cursor)
        serialized = repr(records)
        self.assertNotIn("private.customer@example.com", serialized)
        self.assertNotIn("123-456", serialized)
        self.assertNotIn("raw_payload", serialized)
        self.assertTrue(all(row["classification"] == "finance" for row in records))
        mrr = next(row for row in records if row["metadata"]["metric_key"] == "mrr")
        self.assertEqual(mrr["metadata"]["value_number"], "250")
        self.assertEqual(mrr["metadata"]["unit"], "AUD")
        self.assertEqual(mrr["metadata"]["dimensions"]["record_count"], 1)
        self.assertNotIn("source_record_hashes", mrr["metadata"]["dimensions"])

        for record in records:
            _capture_record(configuration, record)
        mrr_source = MemorySource.objects.get(
            configuration=configuration,
            external_id__startswith="mrr:",
        )
        extraction_provider = Mock()
        extraction_provider.extract.side_effect = AssertionError(
            "Structured facts must not call the model provider."
        )
        extraction = extract_source_version(
            source_version=mrr_source.current_version,
            provider=extraction_provider,
        )
        extraction_provider.extract.assert_not_called()
        self.assertEqual(extraction["claims_created"], 1)
        claim = MemoryClaim.objects.get(
            extraction_run__source_version=mrr_source.current_version
        )
        self.assertEqual(claim.kind, MemoryClaimKind.METRIC)
        self.assertEqual(claim.epistemic_type, "system_fact")
        self.assertEqual(claim.predicate, "mrr")
        self.assertEqual(claim.object_value, "250")
        self.assertIsNotNone(claim.stale_after)
        previous_stale_after = claim.stale_after

        refresh_page = connector.incremental_sync(
            configuration, final_page.next_cursor
        )
        while True:
            for record in refresh_page.records:
                _capture_record(configuration, record)
            if not refresh_page.has_more:
                break
            refresh_page = connector.incremental_sync(
                configuration, refresh_page.next_cursor
            )
        claim.refresh_from_db()
        self.assertGreater(claim.stale_after, previous_stale_after)
        self.assertIsNotNone(claim.last_confirmed_at)

        mrr_scope = next(row for row in scopes if row.external_id == "mrr")
        mrr_scope.selected = False
        mrr_scope.status = MemoryScopeStatus.EXCLUDED
        mrr_scope.save(update_fields=("selected", "status", "updated_at"))
        removal_page = connector.incremental_sync(configuration, final_page.next_cursor)
        removals = list(removal_page.removals)
        while removal_page.has_more:
            removal_page = connector.incremental_sync(
                configuration, removal_page.next_cursor
            )
            removals.extend(removal_page.removals)
        removal = next(row for row in removals if row["external_id"].startswith("mrr:"))
        self.assertEqual(_apply_removal(configuration, removal), 1)
        self.assertEqual(
            MemorySource.objects.get(external_id=removal["external_id"]).lifecycle_state,
            MemorySourceLifecycle.TOMBSTONED,
        )

    @patch("org_memory.connectors.structured_aggregates.sync_xero_connection")
    def test_xero_mrr_uses_sanitized_cadence_even_when_source_record_is_old(self, sync):
        connection, configuration, scopes = self._configuration(
            "xero",
            "tenant-xero",
            [("aggregate", "mrr"), ("aggregate", "recurring_invoice_count")],
        )
        self._financial_record(
            connection,
            record_type=ExternalFinancialRecord.RECORD_XERO_REPEATING_INVOICE,
            external_id="repeat-private",
            amount="1200.00",
            status="AUTHORISED",
            transaction_date=date.today() - timedelta(days=1200),
            category="recurrence:YEAR:1",
        )

        _page, records = self._finish_backfill(
            connector_registry.get("xero"), configuration, scopes
        )

        sync.assert_called_once_with(connection)
        mrr = next(row for row in records if row["metadata"]["metric_key"] == "mrr")
        self.assertEqual(mrr["metadata"]["value_number"], "100")
        self.assertNotIn("repeat-private", repr(mrr))
        count = next(
            row
            for row in records
            if row["metadata"]["metric_key"] == "recurring_invoice_count"
        )
        self.assertEqual(count["metadata"]["value_number"], "1")

    @patch("org_memory.connectors.structured_aggregates.sync_stripe_connection")
    def test_multi_currency_metrics_do_not_overwrite_counts_or_mrr(self, _sync):
        connection, configuration, scopes = self._configuration(
            "stripe",
            "acct-multicurrency",
            [
                ("aggregate", "invoice_count"),
                ("aggregate", "mrr"),
                ("aggregate", "active_subscriptions"),
            ],
        )
        for currency in ("AUD", "USD"):
            self._financial_record(
                connection,
                record_type="stripe_invoice",
                external_id=f"invoice-{currency}",
                amount="100.00",
                status="paid",
                transaction_date=date.today(),
                currency=currency,
            )
            self._financial_record(
                connection,
                record_type="stripe_subscription",
                external_id=f"subscription-{currency}",
                amount="25.00",
                status="active",
                transaction_date=date.today(),
                category="monthly_normalized",
                currency=currency,
            )

        self._finish_backfill(connector_registry.get("stripe"), configuration, scopes)

        artifacts = StructuredAggregateArtifact.objects.filter(
            configuration=configuration,
            lifecycle_state=StructuredAggregateState.ACTIVE,
        )
        self.assertEqual(artifacts.filter(metric_key="mrr").count(), 2)
        self.assertEqual(artifacts.filter(metric_key="invoice_count").count(), 2)
        active_count = artifacts.get(metric_key="active_subscriptions")
        self.assertEqual(active_count.value_number, Decimal("2"))

        ExternalFinancialRecord.objects.get(
            connection=connection,
            external_record_id="subscription-USD",
        ).delete()
        connector_registry.get("stripe").incremental_sync(configuration, None)
        usd_mrr = StructuredAggregateArtifact.objects.get(
            configuration=configuration,
            metric_key="mrr",
            unit="USD",
        )
        self.assertEqual(usd_mrr.value_number, Decimal("0"))
        active_count.refresh_from_db()
        self.assertEqual(active_count.value_number, Decimal("1"))

    @patch(
        "org_memory.connectors.structured_aggregates."
        "LumaAttendeeReportService.collect_ended_event_attendance"
    )
    def test_luma_uses_exact_events_and_never_emits_guest_pii(self, collect):
        connection, configuration, scopes = self._configuration(
            "luma",
            "calendar-luma",
            [
                ("event", "evt-approved"),
                ("aggregate", "events_run"),
                ("aggregate", "event_registrations"),
                ("aggregate", "event_attendees"),
                ("aggregate", "event_check_in_rate"),
            ],
        )
        start = timezone.now() - timedelta(days=10)
        LumaEventSelection.objects.create(
            connection=connection,
            user=self.user,
            organization=self.organization,
            event_id="evt-approved",
            event_name="MLAI Community Night",
            event_url="https://lu.ma/evt-approved",
            start_at=start,
            selected=True,
            raw_payload={
                "guests": [{"name": "Private Person", "email": "guest@example.com"}],
                "registration_answers": {"phone": "0400000000"},
            },
        )
        collect.return_value = [
            {
                "event": {
                    "id": "evt-approved",
                    "name": "MLAI Community Night",
                    "url": "https://lu.ma/evt-approved",
                    "start_at": start.isoformat(),
                    "end_at": (start + timedelta(hours=2)).isoformat(),
                    "guests": [
                        {"name": "Private Person", "email": "guest@example.com"}
                    ],
                    "registration_answers": {"phone": "0400000000"},
                },
                "start_at": start,
                "registration_count": 8,
                "checked_in_count": 6,
            },
            {
                "event": {
                    "id": "evt-not-approved",
                    "name": "Private event outside scope",
                    "start_at": start.isoformat(),
                },
                "start_at": start,
                "registration_count": 99,
                "checked_in_count": 99,
            },
        ]

        final_page, records = self._finish_backfill(
            connector_registry.get("luma"), configuration, scopes
        )

        collect.assert_called_once_with(event_ids={"evt-approved"})
        serialized = repr(records)
        self.assertNotIn("Private Person", serialized)
        self.assertNotIn("guest@example.com", serialized)
        self.assertNotIn("0400000000", serialized)
        self.assertNotIn("evt-not-approved", serialized)
        event = next(row for row in records if row["source_type"] == "luma_event")
        self.assertFalse(event["metadata"]["attendee_pii_included"])
        attendee_metric = next(
            row
            for row in records
            if row["metadata"]["metric_key"] == "event_attendees"
        )
        self.assertEqual(attendee_metric["metadata"]["value_number"], "6")
        rate = next(
            row
            for row in records
            if row["metadata"]["metric_key"] == "event_check_in_rate"
        )
        self.assertEqual(rate["metadata"]["value_number"], "75")
        revisions = dict(
            StructuredAggregateArtifact.objects.filter(
                configuration=configuration
            ).values_list("external_id", "source_revision")
        )
        connector_registry.get("luma").incremental_sync(
            configuration, final_page.next_cursor
        )
        self.assertEqual(
            revisions,
            dict(
                StructuredAggregateArtifact.objects.filter(
                    configuration=configuration
                ).values_list("external_id", "source_revision")
            ),
        )

    @patch("org_memory.connectors.structured_aggregates.sync_stripe_connection")
    def test_disconnected_connection_revokes_existing_structured_sources(self, sync):
        connection, configuration, scopes = self._configuration(
            "stripe", "acct_access", [("aggregate", "mrr")]
        )
        self._financial_record(
            connection,
            record_type="stripe_subscription",
            external_id="sub-access",
            amount="50.00",
            status="active",
            transaction_date=date.today(),
            category="monthly_normalized",
        )
        connector = connector_registry.get("stripe")
        _page, records = self._finish_backfill(connector, configuration, scopes)
        for record in records:
            _capture_record(configuration, record)

        connection.status = "disconnected"
        connection.save(update_fields=("status", "updated_at"))
        page = connector.incremental_sync(configuration, configuration.sync_cursor)

        self.assertEqual(page.records, ())
        self.assertTrue(page.removals)
        self.assertTrue(all(row["revoke_access"] for row in page.removals))
        for removal in page.removals:
            _apply_removal(configuration, removal)
        source = MemorySource.objects.get(configuration=configuration)
        self.assertEqual(source.lifecycle_state, MemorySourceLifecycle.ACCESS_REVOKED)
        self.assertEqual(
            StructuredAggregateArtifact.objects.get(configuration=configuration).lifecycle_state,
            StructuredAggregateState.ACCESS_LOST,
        )

    def test_finance_and_luma_artifact_changes_schedule_debounced_refreshes(self):
        stripe, stripe_configuration, _scopes = self._configuration(
            "stripe", "acct-wake", [("aggregate", "mrr")]
        )
        started = timezone.now()
        with self.captureOnCommitCallbacks(execute=True):
            self._financial_record(
                stripe,
                record_type="stripe_subscription",
                external_id="sub-wake",
                amount="25.00",
                status="active",
                transaction_date=date.today(),
                category="monthly_normalized",
            )
        stripe_configuration.refresh_from_db()
        self.assertGreaterEqual(
            stripe_configuration.next_scheduled_sync_at,
            started + timedelta(seconds=55),
        )

        luma, luma_configuration, _scopes = self._configuration(
            "luma", "calendar-wake", [("event", "evt-wake")]
        )
        with self.captureOnCommitCallbacks(execute=True):
            LumaEventSelection.objects.create(
                connection=luma,
                user=self.user,
                organization=self.organization,
                event_id="evt-wake",
                event_name="Wake event",
                start_at=timezone.now() - timedelta(days=1),
                selected=True,
            )
        luma_configuration.refresh_from_db()
        self.assertIsNotNone(luma_configuration.next_scheduled_sync_at)

    @patch("org_memory.connectors.structured_aggregates.sync_stripe_connection")
    def test_memory_initiated_finance_sync_does_not_schedule_itself(self, sync):
        connection, configuration, scopes = self._configuration(
            "stripe", "acct-no-loop", [("aggregate", "mrr")]
        )
        sync.side_effect = lambda _connection: self._financial_record(
            connection,
            record_type="stripe_subscription",
            external_id="sub-no-loop",
            amount="30.00",
            status="active",
            transaction_date=date.today(),
            category="monthly_normalized",
        )

        with self.captureOnCommitCallbacks(execute=True):
            connector_registry.get("stripe").backfill(configuration, scopes, {})

        configuration.refresh_from_db()
        self.assertIsNone(configuration.next_scheduled_sync_at)
