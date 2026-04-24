from datetime import date, timedelta
from decimal import Decimal
from unittest.mock import patch
import urllib.parse

from django.contrib.auth import get_user_model
from django.test import Client, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone
from rest_framework import status
from rest_framework.test import APIClient

from core.models import ContentFactoryRun, Organization
from integrations.models import (
    ExternalFinancialRecord,
    FinancialConnection,
    FinancialProvider,
    FinancialRecordType,
    MonthlyRevenueSnapshot,
    StartupMetricObservation,
    UserStartupBinding,
)
from integrations.services.finance import (
    calculate_and_publish_monthly_revenue,
    prepare_connection_for_financial_run,
    sync_next_financial_page,
)

User = get_user_model()


class _JsonResponse:
    def __init__(self, payload):
        self.payload = payload

    def json(self):
        return self.payload

    def raise_for_status(self):
        return None


@override_settings(
    STRIPE_CONNECT_CLIENT_ID="ca_test_client",
    STRIPE_SECRET_KEY="sk_test_secret",
    STRIPE_OAUTH_REDIRECT_URI="http://localhost:8000/integrations/callback/stripe",
    XERO_CLIENT_ID="xero-client-id",
    XERO_CLIENT_SECRET="xero-secret",
    XERO_OAUTH_REDIRECT_URI="http://localhost:8000/integrations/callback/xero",
    DEFAULT_FRONTEND_URL="http://localhost:5173",
    VIBE_RAISING_URL="http://localhost:5173",
)
class FinancialOAuthTests(TestCase):
    def setUp(self):
        self.client = Client()
        self.user = User.objects.create_user(email="founder@example.com", role="participant")
        self.organization = Organization.objects.create(name="Acme", domain="acme.com")

    def test_stripe_connect_stores_state_domain_and_validated_next(self):
        self.client.force_login(self.user)

        response = self.client.get(
            reverse("stripe_connect"),
            {
                "domain": self.organization.domain,
                "next": "http://localhost:5173/settings?tab=finance",
            },
        )

        self.assertEqual(response.status_code, 302)
        parsed = urllib.parse.urlparse(response.url)
        params = urllib.parse.parse_qs(parsed.query)
        self.assertEqual(parsed.netloc, "connect.stripe.com")
        self.assertEqual(params["client_id"], ["ca_test_client"])
        self.assertEqual(params["scope"], ["read_only"])
        self.assertEqual(params["redirect_uri"], ["http://localhost:8000/integrations/callback/stripe"])
        self.assertEqual(self.client.session["stripe_oauth_domain"], self.organization.domain)
        self.assertEqual(self.client.session["stripe_oauth_next"], "http://localhost:5173/settings?tab=finance")
        self.assertEqual(params["state"], [self.client.session["stripe_oauth_state"]])

    @patch("integrations.views.enqueue_financial_sync_run")
    @patch("integrations.views.exchange_stripe_oauth_code")
    def test_stripe_callback_creates_financial_connection(self, mock_exchange, mock_enqueue):
        self.client.force_login(self.user)
        session = self.client.session
        session["stripe_oauth_state"] = "stripe-state"
        session["stripe_oauth_domain"] = self.organization.domain
        session["stripe_oauth_next"] = "/settings?tab=finance"
        session.save()
        mock_exchange.return_value = {
            "access_token": "stripe-access-token",
            "stripe_user_id": "acct_123",
            "scope": "read_only",
            "livemode": False,
        }

        response = self.client.get(reverse("stripe_callback"), {"state": "stripe-state", "code": "stripe-code"})

        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.url, "/settings?tab=finance")
        connection = FinancialConnection.objects.get(
            organization=self.organization,
            provider=FinancialProvider.STRIPE,
            external_account_id="acct_123",
        )
        self.assertEqual(connection.access_token, "stripe-access-token")
        self.assertEqual(connection.scopes, ["read_only"])
        self.assertTrue(UserStartupBinding.objects.filter(user=self.user, organization=self.organization).exists())
        mock_enqueue.assert_called_once()

    @patch("integrations.views.enqueue_financial_sync_run")
    @patch("integrations.views.fetch_xero_connections")
    @patch("integrations.views.exchange_xero_oauth_code")
    def test_xero_callback_creates_tenant_connections(self, mock_exchange, mock_fetch_tenants, mock_enqueue):
        self.client.force_login(self.user)
        session = self.client.session
        session["xero_oauth_state"] = "xero-state"
        session["xero_oauth_domain"] = self.organization.domain
        session.save()
        mock_exchange.return_value = {
            "access_token": "xero-access-token",
            "refresh_token": "xero-refresh-token",
            "expires_in": 1800,
            "scope": "openid offline_access accounting.transactions.read",
        }
        mock_fetch_tenants.return_value = [
            {
                "id": "connection-id",
                "tenantId": "tenant-123",
                "tenantName": "Acme Demo",
                "tenantType": "ORGANISATION",
            }
        ]

        response = self.client.get(reverse("xero_callback"), {"state": "xero-state", "code": "xero-code"})

        self.assertEqual(response.status_code, 302)
        connection = FinancialConnection.objects.get(
            organization=self.organization,
            provider=FinancialProvider.XERO,
            external_account_id="tenant-123",
        )
        self.assertEqual(connection.access_token, "xero-access-token")
        self.assertEqual(connection.refresh_token, "xero-refresh-token")
        self.assertEqual(connection.display_name, "Acme Demo")
        mock_fetch_tenants.assert_called_once_with("xero-access-token")
        mock_enqueue.assert_called_once()


class FinancialRevenueCalculationTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(email="founder@example.com", role="participant")
        self.organization = Organization.objects.create(name="Acme", domain="acme.com")
        self.stripe_connection = FinancialConnection.objects.create(
            organization=self.organization,
            user=self.user,
            provider=FinancialProvider.STRIPE,
            external_account_id="acct_123",
            display_name="Stripe",
        )
        self.xero_connection = FinancialConnection.objects.create(
            organization=self.organization,
            user=self.user,
            provider=FinancialProvider.XERO,
            external_account_id="tenant-123",
            display_name="Xero",
        )

    def test_calculates_mrr_growth_and_cash_collected_from_normalized_records(self):
        MonthlyRevenueSnapshot.objects.create(
            organization=self.organization,
            month=date(2026, 2, 1),
            currency="USD",
            mrr_amount=Decimal("100.0000"),
        )
        ExternalFinancialRecord.objects.create(
            organization=self.organization,
            connection=self.stripe_connection,
            provider=FinancialProvider.STRIPE,
            object_type=FinancialRecordType.SUBSCRIPTION,
            external_id="sub_mar",
            source_status="active",
            period_start=date(2026, 3, 1),
            amount=Decimal("150.0000"),
            currency="USD",
            raw_payload={"id": "sub_mar", "status": "active"},
        )
        ExternalFinancialRecord.objects.create(
            organization=self.organization,
            connection=self.stripe_connection,
            provider=FinancialProvider.STRIPE,
            object_type=FinancialRecordType.INVOICE,
            external_id="in_mar",
            source_status="paid",
            period_start=date(2026, 3, 1),
            amount=Decimal("200.0000"),
            currency="USD",
            raw_payload={"id": "in_mar", "status": "paid"},
        )
        ExternalFinancialRecord.objects.create(
            organization=self.organization,
            connection=self.xero_connection,
            provider=FinancialProvider.XERO,
            object_type=FinancialRecordType.REPEATING_INVOICE,
            external_id="xero_repeat_1",
            source_status="AUTHORISED",
            period_start=date(2026, 3, 1),
            amount=Decimal("50.0000"),
            currency="USD",
            raw_payload={"Type": "ACCREC", "Status": "AUTHORISED"},
        )
        ExternalFinancialRecord.objects.create(
            organization=self.organization,
            connection=self.xero_connection,
            provider=FinancialProvider.XERO,
            object_type=FinancialRecordType.INVOICE,
            external_id="xero_stripe_invoice",
            source_status="PAID",
            period_start=date(2026, 3, 1),
            amount=Decimal("999.0000"),
            currency="USD",
            raw_payload={"Type": "ACCREC", "Status": "PAID", "Reference": "Stripe payout"},
        )

        run = ContentFactoryRun.objects.create(
            run_id="financial-test-run",
            workflow="financial_monthly_metrics",
            domain=self.organization.domain,
            run_request={"organization_id": self.organization.id},
        )

        result = calculate_and_publish_monthly_revenue(run=run)

        self.assertEqual(result["snapshot_count"], 1)
        snapshot = MonthlyRevenueSnapshot.objects.get(organization=self.organization, month=date(2026, 3, 1))
        self.assertEqual(snapshot.mrr_amount, Decimal("200.0000"))
        self.assertEqual(snapshot.cash_collected_amount, Decimal("200.0000"))
        self.assertEqual(snapshot.mrr_delta, Decimal("100.0000"))
        self.assertEqual(snapshot.mrr_growth_rate, Decimal("1.000000"))
        self.assertEqual(snapshot.source_mix["stripe_subscriptions"], 1)
        self.assertEqual(snapshot.source_mix["xero_repeating_invoices"], 1)

        mrr_metric = StartupMetricObservation.objects.get(
            organization=self.organization,
            metric_key="mrr",
            period_month=date(2026, 3, 1),
        )
        self.assertEqual(mrr_metric.value_number, Decimal("200.0000"))
        growth_metric = StartupMetricObservation.objects.get(
            organization=self.organization,
            metric_key="revenue_growth_rate",
            period_month=date(2026, 3, 1),
        )
        self.assertEqual(growth_metric.value_text, "100.00%")

    @override_settings(STRIPE_SECRET_KEY="sk_test_secret", STRIPE_API_VERSION="2026-02-25.clover")
    @patch("integrations.services.finance.requests.get")
    def test_stripe_provider_sync_normalizes_subscription_mrr(self, mock_get):
        run = ContentFactoryRun.objects.create(
            run_id="financial-stripe-sync",
            workflow="financial_monthly_metrics",
            domain=self.organization.domain,
            run_request={"organization_id": self.organization.id, "connection_ids": [self.stripe_connection.id]},
        )
        prepare_connection_for_financial_run(self.stripe_connection, run=run)
        mock_get.return_value = _JsonResponse(
            {
                "has_more": False,
                "data": [
                    {
                        "id": "sub_123",
                        "status": "active",
                        "customer": "cus_123",
                        "created": 1772323200,
                        "current_period_start": 1772323200,
                        "current_period_end": 1775001600,
                        "items": {
                            "data": [
                                {
                                    "quantity": 2,
                                    "price": {
                                        "unit_amount": 5000,
                                        "currency": "usd",
                                        "recurring": {"interval": "month", "interval_count": 1},
                                    },
                                }
                            ]
                        },
                    }
                ],
            }
        )

        result = sync_next_financial_page(run=run)

        self.assertEqual(result["synced_count"], 1)
        record = ExternalFinancialRecord.objects.get(external_id="sub_123")
        self.assertEqual(record.amount, Decimal("100.0000"))
        self.assertEqual(record.currency, "USD")
        _args, kwargs = mock_get.call_args
        self.assertEqual(kwargs["headers"]["Stripe-Version"], "2026-02-25.clover")
        self.assertEqual(kwargs["headers"]["Stripe-Account"], "acct_123")

    @patch("integrations.services.finance.requests.get")
    def test_xero_provider_sync_sends_tenant_header_and_normalizes_annual_repeating_invoice(self, mock_get):
        self.xero_connection.access_token = "xero-access-token"
        self.xero_connection.expires_at = timezone.now() + timedelta(hours=1)
        self.xero_connection.save(update_fields=["access_token", "expires_at"])
        run = ContentFactoryRun.objects.create(
            run_id="financial-xero-sync",
            workflow="financial_monthly_metrics",
            domain=self.organization.domain,
            run_request={"organization_id": self.organization.id, "connection_ids": [self.xero_connection.id]},
        )
        prepare_connection_for_financial_run(self.xero_connection, run=run)
        mock_get.return_value = _JsonResponse(
            {
                "RepeatingInvoices": [
                    {
                        "RepeatingInvoiceID": "repeat_123",
                        "Type": "ACCREC",
                        "Status": "AUTHORISED",
                        "Total": "1200.00",
                        "CurrencyCode": "USD",
                        "Date": "2026-03-01",
                        "Schedule": {"Unit": "YEARLY", "Period": 1},
                    }
                ]
            }
        )

        result = sync_next_financial_page(run=run)

        self.assertEqual(result["synced_count"], 1)
        record = ExternalFinancialRecord.objects.get(external_id="repeat_123")
        self.assertEqual(record.amount, Decimal("100.0000"))
        self.assertEqual(record.currency, "USD")
        _args, kwargs = mock_get.call_args
        self.assertEqual(kwargs["headers"]["Xero-Tenant-Id"], "tenant-123")
        self.assertEqual(kwargs["params"], {"page": 1, "pageSize": 100})


class FinancialApiTests(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.api_key = "finance-api-key"
        self.headers = {"HTTP_X_API_KEY": self.api_key}
        self.user = User.objects.create_user(email="founder@example.com", role="participant")
        self.organization = Organization.objects.create(name="Acme", domain="acme.com")
        UserStartupBinding.objects.create(user=self.user, organization=self.organization)
        self.connection = FinancialConnection.objects.create(
            organization=self.organization,
            user=self.user,
            provider=FinancialProvider.STRIPE,
            external_account_id="acct_123",
        )

    @patch("integrations.api_views_finance.notify_valley_run_created")
    def test_manual_financial_sync_creates_run(self, mock_notify):
        self.client.force_authenticate(self.user)

        with self.captureOnCommitCallbacks(execute=True):
            response = self.client.post(
                reverse("financial_sync"),
                {"domain": self.organization.domain},
                format="json",
            )

        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertTrue(response.data["run_id"].startswith("financial-metrics-"))
        mock_notify.assert_called_once_with(response.data["run_id"])

    @patch("integrations.api_views_finance.notify_valley_run_created")
    def test_internal_financial_run_create_uses_api_key(self, mock_notify):
        with self.captureOnCommitCallbacks(execute=True):
            with self.settings(INTERNAL_API_KEY=self.api_key):
                response = self.client.post(
                    reverse("financial_run_create"),
                    {
                        "domain": self.organization.domain,
                        "user_id": self.user.id,
                    },
                    format="json",
                    **self.headers,
                )

        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(response.data["run"]["workflow"], "financial_monthly_metrics")
        self.assertEqual(response.data["run"]["run_request"]["connection_ids"], [self.connection.id])
        mock_notify.assert_called_once_with(response.data["run_id"])
