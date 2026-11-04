"""Database-free tests; configure only a synthetic local cache and mock HTTP."""
import importlib.util
from pathlib import Path
import sys
from types import ModuleType, SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from django.conf import settings

if not settings.configured:
    settings.configure(
        SECRET_KEY="synthetic-unit-test-key",
        XERO_CLIENT_ID="test-app",
        CACHES={"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}},
    )

from django.core.cache import cache
import requests
from integrations import http_client


def response(status=200, retry=None, payload=None):
    import json
    result = requests.Response()
    result.status_code = status
    result.url = "https://api.xero.com/api.xro/2.0/Accounts"
    result._content = json.dumps(payload or {}).encode()
    if retry is not None:
        result.headers["Retry-After"] = str(retry)
    return result


class HttpBackoffTests(unittest.TestCase):
    def setUp(self):
        cache.clear()
        self.url = "https://api.xero.com/api.xro/2.0/Accounts"
        self.headers = {"Xero-Tenant-Id": "tenant-a", "Authorization": "Bearer synthetic-not-a-token"}

    def test_retry_after_blocks_other_endpoints_and_writes_for_same_tenant(self):
        with patch.object(http_client.requests, "request", return_value=response(429, 39249)) as request:
            first = http_client.get(self.url, headers=self.headers)
            second = http_client.post(self.url.replace("Accounts", "BankTransactions"), headers=self.headers)
        self.assertEqual(first.status_code, 429)
        self.assertEqual(second.status_code, 429)
        self.assertGreater(int(second.headers["Retry-After"]), 39000)
        self.assertEqual(request.call_count, 1)
        self.assertNotIn("synthetic-not-a-token", second.text)

    def test_another_tenant_is_not_blocked(self):
        with patch.object(http_client.requests, "request", side_effect=[response(429, 100), response()]) as request:
            http_client.get(self.url, headers=self.headers)
            other = http_client.get(self.url, headers={"Xero-Tenant-Id": "tenant-b"})
        self.assertEqual(other.status_code, 200)
        self.assertEqual(request.call_count, 2)

    def test_non_xero_provider_is_unaffected(self):
        with patch.object(http_client.requests, "request", side_effect=[response(429, 100), response()]) as request:
            http_client.get(self.url, headers=self.headers)
            other = http_client.get("https://example.test/Accounts", headers=self.headers)
        self.assertEqual(other.status_code, 200)
        self.assertEqual(request.call_count, 2)

    def test_expired_backoff_allows_request(self):
        key = http_client._xero_cooldown_key(self.url, self.headers)
        cache.set(key, 100, 60)
        with patch.object(http_client.time, "time", return_value=101), patch.object(http_client.requests, "request", return_value=response()) as request:
            result = http_client.get(self.url, headers=self.headers)
        self.assertEqual(result.status_code, 200)
        request.assert_called_once()

    def test_malformed_retry_after_uses_short_cooldown(self):
        with patch.object(http_client.requests, "request", return_value=response(429, "bad")) as request:
            http_client.get(self.url, headers=self.headers)
            result = http_client.get(self.url, headers=self.headers)
        self.assertEqual(request.call_count, 1)
        self.assertIn(int(result.headers["Retry-After"]), (59,60))


class CatalogueTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        models = ModuleType("integrations.models")
        models.ReconciliationProfile = object
        reconciliation = ModuleType("integrations.services.xero_reconciliation")
        reconciliation.XERO_API_URL = "https://api.xero.com/api.xro/2.0"
        reconciliation.ReconciliationValidationError = type("ReconciliationValidationError", (RuntimeError,), {})
        reconciliation._xero_headers = lambda connection: {"Xero-Tenant-Id": connection.external_account_id}
        name = "isolated_bank_account_catalog"
        spec = importlib.util.spec_from_file_location(name, Path(__file__).resolve().parents[1] / "integrations/services/reconciliation_bank_accounts.py")
        cls.catalog = importlib.util.module_from_spec(spec)
        with patch.dict(sys.modules, {"integrations.models": models, "integrations.services.xero_reconciliation": reconciliation}):
            spec.loader.exec_module(cls.catalog)

    def setUp(self):
        cache.clear()
        self.profile = SimpleNamespace(organization_id=1, xero_connection=SimpleNamespace(pk=2, external_account_id="tenant-a"))
        self.payload = {"Accounts": [{"Type":"BANK","Status":"ACTIVE","AccountID":"bank-a","Name":"Bank A"}, {"Type":"BANK","Status":"ARCHIVED","AccountID":"old","Name":"Old"}]}

    def test_reuses_verified_catalogue_during_all_account_capture(self):
        with patch.object(http_client, "get", return_value=response(payload=self.payload)) as request:
            first = self.catalog.fetch_active_xero_bank_accounts(self.profile)
            first[0]["name"] = "caller mutation"
            second = self.catalog.fetch_active_xero_bank_accounts(self.profile)
        self.assertEqual(second, [{"bank_account_id":"bank-a","name":"Bank A"}])
        self.assertEqual(request.call_count, 1)

    def test_different_tenant_does_not_reuse_catalogue(self):
        other = SimpleNamespace(organization_id=1, xero_connection=SimpleNamespace(pk=2, external_account_id="tenant-b"))
        with patch.object(http_client, "get", return_value=response(payload=self.payload)) as request:
            self.catalog.fetch_active_xero_bank_accounts(self.profile)
            self.catalog.fetch_active_xero_bank_accounts(other)
        self.assertEqual(request.call_count, 2)

    def test_rate_limit_reports_wait_and_never_caches_unverified_accounts(self):
        with patch.object(http_client, "get", side_effect=[response(429, 39249), response(payload=self.payload)]) as request:
            with self.assertRaisesRegex(RuntimeError, "retry after 39249 seconds"):
                self.catalog.fetch_active_xero_bank_accounts(self.profile)
            accounts = self.catalog.fetch_active_xero_bank_accounts(self.profile)
        self.assertEqual(len(accounts), 1)
        self.assertEqual(request.call_count, 2)

    def test_inactive_and_duplicate_bank_accounts_fail_closed(self):
        with self.assertRaisesRegex(RuntimeError, "no active BANK"):
            self.catalog._normalise_accounts([])
        rows = self.payload["Accounts"][:1] * 2
        with self.assertRaisesRegex(RuntimeError, "duplicate"):
            self.catalog._normalise_accounts(rows)


if __name__ == "__main__":
    unittest.main()
