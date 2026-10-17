"""Capture leases and provider result fencing, with real guards and no database."""

from contextlib import nullcontext
from copy import deepcopy
from datetime import timedelta
from unittest.mock import Mock, patch
from uuid import UUID, uuid4

from django.contrib.auth import get_user_model
from django.db.models import Q
from django.test import SimpleTestCase, override_settings
from django.utils import timezone

from founder_tools.models import VibeRaisingCompany
from organizations.models import Organization
from . import cms_capture as capture
from . import cms_operation_scope as scope
from .cms_api_transport import CmsApiError
from .models import OrganizationContentConfig
from .website_contract import WebsiteAuthorityError
from .website_connections import contract_for
from .website_models import WebsiteConnectionOperation
from . import tests_cms_connection_authority_unit as authority_fixtures
from . import tests_cms_connection_contract_unit as keys
from .tests_cms_inventory_contract_unit import wordpress_post


def matches(row, predicate):
    if isinstance(predicate, Q):
        values = [matches(row, child) for child in predicate.children]
        result = all(values) if predicate.connector == "AND" else any(values)
        return not result if predicate.negated else result
    key, expected = predicate
    parts = key.split("__")
    op = parts.pop() if parts[-1] in {"isnull", "in", "has_key"} else "exact"
    actual = row
    for part in parts:
        actual = actual.get(part) if isinstance(actual, dict) else getattr(actual, part, None)
    if op == "isnull": return (actual is None) == expected
    if op == "in": return actual in expected
    if op == "has_key": return isinstance(actual, dict) and expected in actual
    foreign_pk = "__" not in key and key.endswith("_id") and type(actual) is int and isinstance(expected, str)
    return str(actual) == str(expected) if key == "pk" or isinstance(actual, UUID) or foreign_pk else actual == expected


class Rows:
    def __init__(self, rows, selected=None):
        self.rows = rows
        self.selected = rows if selected is None else selected

    def select_for_update(self, *args, **kwargs): return self

    def select_related(self, *args, **kwargs): return self

    def __iter__(self): return iter(self.selected)

    def exists(self): return bool(self.selected)

    def filter(self, *predicates, **values):
        return Rows(self.rows, [row for row in self.selected if all(matches(row, item) for item in [*predicates, *values.items()])])

    def first(self): return self.selected[0] if self.selected else None

    def exclude(self, *predicates, **values):
        return Rows(self.rows, [row for row in self.selected if not all(matches(row, item) for item in [*predicates, *values.items()])])

    def delete(self):
        for row in list(self.selected):
            if row in self.rows: self.rows.remove(row)

    def order_by(self, *fields):
        selected = list(self.selected)
        for name in reversed(fields):
            selected.sort(key=lambda row: str(getattr(row, name.lstrip("-"), None) or ""), reverse=name.startswith("-"))
        return Rows(self.rows, selected)

    def update(self, **values):
        for row in self.selected:
            for key, value in values.items(): setattr(row, key, deepcopy(value))
        return len(self.selected)

    def get_or_create(self, *, idempotency_key, defaults):
        existing = next((row for row in self.rows if row.idempotency_key == idempotency_key), None)
        if existing: return existing, False
        row = WebsiteConnectionOperation(idempotency_key=idempotency_key, **deepcopy(defaults))
        row.save = Mock(side_effect=lambda **kwargs: setattr(row, "updated_at", timezone.now()))
        row.refresh_from_db = Mock()
        row.updated_at = timezone.now()
        row.created_at = row.updated_at
        self.rows.append(row)
        return row, True


@override_settings(ARTICLES_CHANNEL_DISCOVERY_ENABLED=True, ARTICLES_PUBLISHING_CHANNELS_ENABLED=True,
    ARTICLES_CMS_CHANNEL_ENABLED=True, CONNECTOR_CREDENTIAL_KEYS=keys.json.dumps({"test": keys.TEST_KEY}),
    CONNECTOR_CREDENTIAL_ACTIVE_KEY_ID="test")
class CmsCaptureTests(SimpleTestCase):
    def setUp(self):
        authority_fixtures.CmsAuthorityTests.setUp(self)
        self.company.profile = self.profile
        for value in (self.user, self.org, self.profile, self.company, self.config):
            value.refresh_from_db = Mock()
        self.config.save = Mock()
        self.saved_rows = []
        self.manager = Rows(self.saved_rows)
        self.patches = [patch("django.db.transaction.atomic", side_effect=lambda: nullcontext()),
            patch.object(Organization.objects, "select_for_update"),
            patch.object(WebsiteConnectionOperation.objects, "filter", side_effect=self.manager.filter),
            patch.object(WebsiteConnectionOperation.objects, "select_for_update", return_value=self.manager),
            patch.object(WebsiteConnectionOperation.objects, "get_or_create", side_effect=self.manager.get_or_create),
            patch.object(OrganizationContentConfig.objects, "select_related"),
            patch.object(VibeRaisingCompany.objects, "select_related"),
            patch.object(get_user_model().objects, "filter"),
            patch.object(capture, "request_json", return_value=([wordpress_post()], {"pages": 1})),
            patch("content_factory.website_connections.require_unlocked_remote_call")]
        values = [item.start() for item in self.patches]
        values[1].return_value.get.return_value = self.org
        values[5].return_value.filter.return_value.first.return_value = self.config
        values[6].return_value.filter.return_value.first.return_value = self.company
        values[7].return_value.first.return_value = self.user
        self.http, self.unlocked = values[8:]
        for item in self.patches: self.addCleanup(item.stop)
        self.data = {**contract_for(self.connection), "client_request_id": str(uuid4())}
        self.now = timezone.now() + timedelta(seconds=1)

    def start(self, data=None):
        return capture.start_capture(self.context, self.config, user=self.user, data=data or self.data)

    def advance(self, operation, **kwargs):
        return capture.advance_capture(operation.pk, now=kwargs.get("now", self.now))

    def test_owner_reservation_is_idempotent_and_cannot_borrow_repo_or_stale_generation(self):
        operation = self.start()
        self.assertIs(self.start(), operation)
        self.assertEqual(len(self.saved_rows), 1)
        self.http.assert_not_called()
        self.assertNotIn("synthetic-application-password", keys.json.dumps(operation.payload))
        for changes in ({"connection_generation": 2}, {"repository_id": 123}, {"connection_target_id": "git"}):
            with self.subTest(changes=changes), self.assertRaises(WebsiteAuthorityError): self.start({**self.data, **changes})

    def test_one_page_completes_without_touching_provider_or_git_publication_facts(self):
        operation = self.start()
        result = self.advance(operation)
        self.assertEqual((result.state, result.receipt["coverage"], result.receipt["count"]), ("completed", "complete", 1))
        self.assertEqual(result.payload["inventory"]["records"][0]["html"], "<p>Original native post</p>")
        self.assertNotIn("Original native post", keys.json.dumps(result.receipt))
        self.assertIs(result.receipt["provider_modified"], False)
        self.assertIs(result.receipt["publishing_verified"], False)
        self.assertEqual(self.http.call_args.kwargs["method"], "GET")
        self.assertNotIn("lease_id", result.payload)
        self.assertEqual(self.connection.verified_sha, "")
        self.unlocked.assert_called_once()

    def test_next_page_resumes_the_same_saved_cursor_after_process_restart(self):
        operation = self.start()
        self.http.return_value = ([wordpress_post(1)], {"pages": 2})
        one = self.advance(operation)
        self.assertEqual((one.state, one.payload["inventory"]["next_page"]), ("pending", 2))
        self.http.return_value = ([wordpress_post(2)], {"pages": 2})
        two = self.advance(operation)
        self.assertEqual((two.state, two.receipt["count"], two.attempts), ("completed", 2, 2))
        self.assertEqual(self.http.call_args.kwargs["query"]["page"], 2)

    def test_active_lease_and_future_retry_do_not_start_another_provider_read(self):
        operation = self.start()
        operation.state, operation.next_attempt_at = "running", self.now + timedelta(seconds=30)
        operation.payload["lease_id"] = str(uuid4())
        self.assertIs(self.advance(operation), operation)
        self.http.assert_not_called()
        operation.state = "pending"
        self.assertIs(self.advance(operation), operation)
        self.http.assert_not_called()

    def test_abandoned_lease_is_replaced_and_only_its_current_result_can_commit(self):
        operation = self.start()
        previous = str(uuid4())
        operation.state, operation.next_attempt_at = "running", self.now - timedelta(seconds=1)
        operation.payload["lease_id"] = previous
        result = self.advance(operation)
        self.assertEqual(result.state, "completed")
        self.assertEqual(result.receipt["count"], 1)
        self.assertNotIn("lease_id", result.payload)

    def test_cancel_before_http_and_owner_transfer_during_read_discard_provider_results(self):
        for timing in ("before", "during"):
            operation = self.start({**self.data, "client_request_id": str(uuid4())})
            if timing == "before":
                operation.refresh_from_db.side_effect = lambda: setattr(self.profile, "user_id", 99)
            else:
                def transfer(*args, **kwargs):
                    self.profile.user_id = 99
                    return [wordpress_post()], {"pages": 1}
                self.http.side_effect = transfer
            result = self.advance(operation)
            self.assertEqual(result.state, "cancelled")
            self.assertEqual(result.payload["inventory"]["records"], [])
            self.assertEqual(self.http.call_count, 0 if timing == "before" else 1)
            self.profile.user_id = 4
            self.http.reset_mock(); self.http.side_effect = None

    def test_revocation_before_claim_is_terminal_without_a_polling_loop(self):
        operation = self.start()
        self.profile.user_id = 99
        result = self.advance(operation)
        self.assertEqual(result.state, "cancelled")
        self.http.assert_not_called()

    def test_newer_lease_or_cancellation_during_http_is_not_overwritten(self):
        for state in ("running", "cancelled"):
            operation = self.start({**self.data, "client_request_id": str(uuid4())})
            def change(*args, **kwargs):
                operation.payload["lease_id"] = "newer-worker"
                operation.state = state
                operation.receipt = {"status": "newer-outcome"}
                return [wordpress_post()], {"pages": 1}
            self.http.side_effect = change
            result = self.advance(operation)
            self.assertEqual(result.receipt, {"status": "newer-outcome"})
            self.assertEqual(result.payload["inventory"]["records"], [])

    def test_transient_failures_retry_same_page_with_backoff_then_stop_with_shared_failure(self):
        operation = self.start()
        self.http.side_effect = CmsApiError("transient")
        for attempt in range(1, 6):
            result = self.advance(operation, now=self.now + timedelta(hours=attempt))
            self.assertEqual(result.payload["inventory"]["next_page"], 1)
            self.assertEqual(result.payload["page_failures"], attempt)
            self.assertEqual(result.state, "pending" if attempt < 5 else "blocked")
            self.assertEqual(result.receipt["failure"]["failure_class"], "transient" if attempt < 5 else "unsupported")
        self.assertEqual(self.http.call_count, 5)
        self.assertIsNone(result.next_attempt_at)

    def test_changing_collection_restarts_are_bounded_and_retain_the_same_operation(self):
        operation = self.start()
        self.http.side_effect = CmsApiError("inventory_changed")
        for attempt in range(3):
            result = self.advance(operation, now=self.now + timedelta(minutes=attempt))
        self.assertEqual((result.state, result.payload["restarts"]), ("blocked", 2))
        self.assertEqual(result.pk, operation.pk)
        self.assertEqual(result.receipt["failure_class"], "unsupported")

    def test_bad_credentials_need_owner_and_unknown_provider_json_does_not_complete_capture(self):
        operation = self.start()
        self.http.side_effect = CmsApiError("credentials_required")
        result = self.advance(operation)
        self.assertEqual((result.state, result.receipt["failure_class"]), ("needs_user", "needs_user"))
        other = self.start({**self.data, "client_request_id": str(uuid4())})
        self.http.side_effect = None; self.http.return_value = ({"html": "private-provider-error-body"}, {})
        result = self.advance(other)
        self.assertEqual(result.state, "blocked")
        self.assertNotIn("private-provider-error-body", str(result.receipt))

    def test_corrupt_inventory_and_disabled_rollout_cannot_make_provider_requests(self):
        operation = self.start()
        operation.payload["inventory"]["binding"]["generation"] = 2
        result = self.advance(operation)
        self.assertEqual(result.state, "blocked")
        self.http.assert_not_called()
        with override_settings(ARTICLES_CMS_CHANNEL_ENABLED=False):
            self.assertIsNone(self.advance(operation))
        self.http.assert_not_called()

    def test_private_credential_rotation_after_read_fences_old_key_result(self):
        operation = self.start()
        def rotate(*args, **kwargs):
            self.connection.capabilities["_cms_credential_operation_id"] = str(uuid4())
            return [wordpress_post()], {"pages": 1}
        self.http.side_effect = rotate
        result = self.advance(operation)
        self.assertEqual(result.state, "cancelled")
        self.assertEqual(result.payload["inventory"]["records"], [])

    def test_late_contract_exception_cannot_block_a_newer_worker(self):
        operation = self.start()
        def newer(*args, **kwargs):
            operation.payload["lease_id"] = "newer-worker"
            operation.receipt = {"status": "newer-outcome"}
            raise ValueError("must-not-overwrite-a-new-lease")
        self.http.side_effect = newer
        result = self.advance(operation)
        self.assertEqual(result.state, "running")
        self.assertEqual(result.receipt, {"status": "newer-outcome"})

    def test_cms_capture_owner_route_uses_common_interface_and_only_returns_receipt(self):
        from types import SimpleNamespace
        from .website_views import WebsiteConnectionActionView
        operation = self.start()
        with patch("content_factory.website_views._context", return_value=(self.context, self.config, None)), \
                patch("content_factory.publishing_channels.execute", return_value=operation) as execute, \
                patch("content_factory.website_operations.operation_summary", return_value={"id": str(operation.pk)}):
            response = WebsiteConnectionActionView().post(SimpleNamespace(data=self.data, user=self.user), "capture-existing")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Cache-Control"], "private, no-store")
        self.assertEqual(execute.call_args.args[0], "capture_existing")
        self.assertEqual(response.data, {"operation": {"id": str(operation.pk)}})

    def test_common_capture_uses_actual_cms_binding_after_portable_preference(self):
        from .publishing_channels import execute
        from .publishing_channel_contract import ChannelRequest
        operation = self.start()
        with patch("content_factory.publishing_channels._selected", return_value="portable"), \
                patch.object(capture, "start_capture", return_value=operation), \
                patch.object(capture, "advance_capture", return_value=operation), \
                patch("content_factory.website_prepare._dispatch_git_prepare_step") as git:
            result = execute("capture_existing", ChannelRequest(context=self.context, config=self.config, user=self.user, data=self.data))
        self.assertIs(result, operation)
        git.assert_not_called()
