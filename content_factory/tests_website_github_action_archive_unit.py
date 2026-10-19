"""The real archive reducer detects force pushes and failed/lost webhook ingress."""
from copy import deepcopy
from contextlib import nullcontext
from datetime import timedelta
import json
from unittest.mock import patch
from unittest.mock import Mock

from django.test import SimpleTestCase, override_settings
from django.utils import timezone

from . import website_github_action_archive as archive
from .tests_website_runtime_observation_unit import response


@override_settings(GITHUB_APP_ID="12", ARTICLES_WEBSITE_DOCTOR_ENABLED=True)
class GitHubActionArchiveTests(SimpleTestCase):
    def setUp(self):
        self.now = timezone.now()
        self.selected = {"goal_id": "00000000-0000-0000-0000-000000000021", "scope_digest": "a" * 64,
            "repository_id": 17, "installation_id": 9, "github_repo": "fixture/mlai-articles-fixture-html",
            "started": self.now - timedelta(minutes=20), "finished": self.now - timedelta(minutes=3)}
        self.app = {"id": 12, "events": ["push", "pull_request"], "permissions": {"contents": "write"}}
        self.config = {"content_type": "json", "insecure_ssl": "0", "url": "https://backend.example.test/github/webhook"}
        self.rows, self.details, self.reads = [], {}, []

    def delivery(self, *, number=1, event="push", action=None, actor_type="User", actor=99, redelivery=False, guid=None):
        guid = guid or f"00000000-0000-0000-0000-{number:012d}"
        row = {"id": number, "guid": guid, "event": event, "action": action, "installation_id": 9,
            "repository_id": 17 if event == "push" else None, "delivered_at": (self.now - timedelta(minutes=5)).isoformat(),
            "redelivery": redelivery, "status_code": 503}
        payload = {"installation": {"id": 9}, "sender": {"id": actor, "type": actor_type}, "action": action,
            "private_article_body": "private-body-canary", "credential": "github_pat_private-canary"}
        if event == "push":
            payload.update(repository={"id": 17, "full_name": self.selected["github_repo"]}, forced=True)
        if event == "installation_repositories":
            payload.update(repositories_added=[{"id": 17}], repositories_removed=[])
        self.rows.append(row)
        self.details[number] = {**row, "request": {"headers": {"X-GitHub-Delivery": guid, "X-GitHub-Event": event}, "payload": payload},
            "response": {"payload": "private-backend-response-canary"}}
        return row

    def get(self, path):
        self.reads.append(path)
        if path == "/app": return self.app, ""
        if path == "/app/hook/config": return self.config, ""
        if path == "/app/hook/deliveries?per_page=100": return self.rows, ""
        return self.details[int(path.rsplit("/", 1)[-1])], ""

    def collect(self):
        return archive.collect(self.selected, self.get, now=self.now)

    def test_failed_force_push_delivery_counts_even_when_ingress_and_ancestry_miss_it(self):
        self.delivery()
        value = self.collect()
        self.assertEqual(value["coverage"], "complete")
        self.assertEqual(value["coverage_scope"], "enumerated_delivery_attempts_only")
        self.assertEqual(value["sources"][0]["source"], "github_push")
        self.assertTrue(value["sources"][0]["forced"])
        self.assertEqual(value["sources"][0]["actor_id"], 99)
        for secret in ("private-body-canary", "github_pat_private-canary", "private-backend-response-canary"):
            self.assertNotIn(secret, json.dumps(value))

    def test_permission_and_repository_access_actions_are_not_inferred_from_latest_snapshot(self):
        self.delivery(event="installation", action="new_permissions_accepted")
        self.delivery(number=2, event="installation_repositories", action="added")
        value = self.collect()
        self.assertEqual([row["source"] for row in value["sources"]], ["github_access_approval"] * 2)

    def test_redelivery_is_deduplicated_but_changed_actor_is_refused(self):
        self.delivery()
        self.delivery(number=2, redelivery=True, guid=self.rows[0]["guid"])
        self.assertEqual(len(self.collect()["sources"]), 1)
        self.details[2]["request"]["payload"]["sender"]["id"] = 100
        with self.assertRaisesMessage(archive.ArchiveUnavailable, "redelivery_changed"):
            self.collect()

    def test_unrelated_installation_and_repository_payloads_are_never_read_or_exported(self):
        self.delivery()
        self.rows[0]["installation_id"] = 88
        self.assertEqual(self.collect()["sources"], [])
        self.assertNotIn("/app/hook/deliveries/1", self.reads)

    def test_empty_window_is_only_a_complete_delivery_source_not_an_exhaustive_owner_audit(self):
        value = self.collect()
        self.assertEqual(value["sources"], [])
        self.assertNotIn("human_actions", value)
        self.assertEqual(value["remaining_requirement"], "stable_repeated_window_and_frozen_provider_configuration")

    def test_configuration_change_and_truncated_or_looped_cursor_do_not_close_window(self):
        reads = 0
        def changed(path):
            nonlocal reads
            value, link = self.get(path)
            if path == "/app":
                reads += 1
                if reads > 1: value = {**value, "events": []}
            return value, link
        with self.assertRaisesMessage(archive.ArchiveUnavailable, "configuration_changed"):
            archive.collect(self.selected, changed, now=self.now)
        path = '<https://api.github.com/app/hook/deliveries?per_page=100&cursor=loop>; rel="next"'
        def looped(query):
            if query.startswith("/app/hook/deliveries?"): return [], path
            return self.get(query)
        with self.assertRaisesMessage(archive.ArchiveUnavailable, "pagination"):
            archive.collect(self.selected, looped, now=self.now)

    def test_outside_retention_and_unsettled_windows_refuse(self):
        for change in ({"started": self.now - timedelta(days=3)}, {"finished": self.now + timedelta(seconds=1)}, {"started": None}):
            with self.subTest(change=change), self.assertRaises(archive.ArchiveUnavailable):
                archive.collect({**self.selected, **change}, self.get, now=self.now)

    def test_missing_push_subscription_insecure_webhook_and_wrong_app_refuse(self):
        for app, config in (({**self.app, "events": []}, self.config), ({**self.app, "id": 2}, self.config),
                (self.app, {**self.config, "insecure_ssl": "1"}), (self.app, {**self.config, "url": "http://unsafe.test"})):
            with self.subTest(app=app, config=config), self.assertRaises(archive.ArchiveUnavailable):
                old = self.app, self.config
                self.app, self.config = app, config
                try: self.collect()
                finally: self.app, self.config = old

    def test_delivery_scope_header_actor_and_action_mismatches_refuse_without_echoing_payload(self):
        self.delivery()
        original = deepcopy(self.details[1])
        for location, key, value in (("sender", "type", "Unknown"), ("sender", "id", True),
                ("repository", "id", 999), ("payload", "forced", "true"), ("payload", "action", "private-body-canary"),
                ("headers", "X-GitHub-Event", "installation")):
            self.details[1] = deepcopy(original)
            parent = self.details[1]["request"]["headers"] if location == "headers" else self.details[1]["request"]["payload"]
            if location in {"sender", "repository"}: parent = parent[location]
            parent[key] = value
            with self.subTest(location=location, key=key), self.assertRaises(archive.ArchiveUnavailable) as raised:
                self.collect()
            self.assertNotIn("private-body-canary", str(raised.exception))

    def test_cursor_is_opaque_but_origin_path_duplicates_and_filter_changes_refuse(self):
        valid = '<https://api.github.com/app/hook/deliveries?per_page=100&cursor=opaque%2Bbase64%3D>; rel="next"'
        self.assertIn("cursor=opaque%2Bbase64%3D", archive._next(valid))
        for link in (valid.replace("api.github.com", "attacker.test"), valid.replace("/app/hook/", "/repos/private/"),
                valid.replace("per_page=100", "status=success"), valid + "," + valid, "garbled; rel=next"):
            with self.subTest(link=link), self.assertRaises(archive.ArchiveUnavailable): archive._next(link)

    def test_actual_app_transport_has_no_ambient_credentials_or_redirects(self):
        with patch.object(archive.requests.Session, "send", return_value=response({"id": 12})) as send:
            reader = archive.AppReads("synthetic-app-jwt")
            self.addCleanup(reader.close)
            value, _ = reader.get("/app")
        self.assertEqual(value, {"id": 12})
        prepared = send.call_args.args[0]
        self.assertEqual(prepared.url, "https://api.github.com/app")
        self.assertEqual(prepared.headers["Authorization"], "Bearer synthetic-app-jwt")
        self.assertFalse(send.call_args.kwargs["allow_redirects"])
        self.assertEqual(send.call_args.kwargs["proxies"], {})
        self.assertTrue(send.call_args.kwargs["verify"])

    def test_transport_refuses_arbitrary_reads_bounds_redirects_and_private_errors(self):
        for path in ("/repos/private/secret", "/app/hook/deliveries?status=success", "https://attacker.test", "/app/../secret"):
            with self.subTest(path=path), patch.object(archive.requests.Session, "send") as send:
                reader = archive.AppReads("synthetic-app-jwt")
                self.addCleanup(reader.close)
                with self.assertRaises(archive.ArchiveUnavailable): reader.get(path)
                send.assert_not_called()
        with patch.object(archive, "_select", side_effect=ValueError("github_pat_private-canary")):
            value = archive.read_bound_archive({})
        self.assertEqual(value["coverage"], "partial")
        self.assertNotIn("github_pat_private-canary", json.dumps(value))

    def test_preflight_returns_only_the_same_provider_configuration_fingerprint(self):
        expected = self.collect()["configuration_digest"]
        with patch("integrations.services.github_app._github_app_jwt", return_value="synthetic-app-jwt"), \
                patch.object(archive.AppReads, "get", side_effect=self.get):
            value = archive.read_profile()
        self.assertEqual(value["coverage"], "complete")
        self.assertEqual(value["configuration_digest"], expected)
        for secret in ("synthetic-app-jwt", self.config["url"], "permissions"):
            self.assertNotIn(secret, json.dumps(value))

    def test_real_scope_selection_uses_numeric_organization_and_saved_policy_stop(self):
        from founder_tools.models import VibeRaisingCompany
        from .website_models import WebsiteConnectionOperation
        from .tests_website_zoo_scope_unit import ZooScopeTests, rows
        from . import website_goal as goals
        fixture = ZooScopeTests(); fixture.setUp()
        connection, operation = fixture.connection, fixture.operation
        connection.github_repo = "fixture/mlai-articles-fixture-html"
        connection.installation_id = "9"
        operation.connection = connection
        operation.generation = connection.generation
        operation.state, operation.next_attempt_at = "needs_attention", None
        operation.updated_at = self.now - timedelta(minutes=5)
        original = {"state": "bound", "company_id": str(fixture.company.pk), "organization_id": "7",
            "original_goal_id": str(operation.pk), "scope_digest": goals.digest(fixture.goal["scope"]), "target": "ready"}
        database = Mock(vendor="postgresql", in_atomic_block=False)
        cursor = Mock()
        database.cursor.return_value = nullcontext(cursor)
        with patch.object(archive, "database", database), patch.object(archive.transaction, "atomic", return_value=nullcontext()), \
                patch.object(VibeRaisingCompany.objects, "filter", return_value=rows(fixture.company)), \
                patch.object(WebsiteConnectionOperation.objects, "select_related", return_value=rows(operation)):
            value = archive._select(original)
            self.assertEqual(value["repository_id"], connection.repository_id)
            self.assertEqual(value["finished"], operation.updated_at + archive.SETTLE)
            cursor.execute.assert_called_with("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")
            operation.generation += 1
            with self.assertRaises(archive.ArchiveUnavailable): archive._select(original)

    def test_historical_window_requires_the_exact_saved_disconnect_transition(self):
        from types import SimpleNamespace
        from founder_tools.models import VibeRaisingCompany
        from .models import OrganizationContentConfig
        from .website_models import WebsiteConnectionOperation
        from .tests_website_zoo_scope_unit import ZooScopeTests, rows
        from . import website_goal as goals
        fixture = ZooScopeTests(); fixture.setUp()
        connection, operation = fixture.connection, fixture.operation
        connection.github_repo, connection.installation_id = "fixture/mlai-articles-fixture-html", "9"
        operation.connection, operation.generation = connection, connection.generation
        operation.state, operation.next_attempt_at = "cancelled", None
        operation.updated_at = self.now - timedelta(minutes=5)
        connection.generation += 1
        connection.configuration_version += 1
        connection.state = "disconnected"
        config = SimpleNamespace(website_connection=connection)
        original = {"state": "bound", "company_id": str(fixture.company.pk), "organization_id": "7",
            "original_goal_id": str(operation.pk), "scope_digest": goals.digest(fixture.goal["scope"]),
            "target": "ready", "policy_historical_scope_v1": True}
        database = Mock(vendor="postgresql", in_atomic_block=False)
        database.cursor.return_value = nullcontext(Mock())
        with patch.object(archive, "database", database), patch.object(archive.transaction, "atomic", return_value=nullcontext()), \
                patch.object(VibeRaisingCompany.objects, "filter", return_value=rows(fixture.company)), \
                patch.object(WebsiteConnectionOperation.objects, "select_related", return_value=rows(operation)), \
                patch.object(OrganizationContentConfig.objects, "select_related", return_value=rows(config)):
            value = archive._select(original)
            self.assertEqual(value["scope_digest"], original["scope_digest"])
            self.assertEqual(value["finished"], operation.updated_at + archive.SETTLE)
            for parent, field, invalid in ((connection, "generation", connection.generation + 1),
                    (connection, "configuration_version", connection.configuration_version + 1),
                    (connection, "state", "connected"), (connection, "repository_id", 100),
                    (connection, "branch", "other"), (connection, "app_root", "other"),
                    (connection, "blockers", [{"deletion_epoch": 1}]),
                    (operation, "state", "needs_attention"), (config, "website_connection", None)):
                before = getattr(parent, field)
                setattr(parent, field, invalid)
                try:
                    with self.subTest(field=field), self.assertRaises(archive.ArchiveUnavailable):
                        archive._select(original)
                finally:
                    setattr(parent, field, before)
            for flag in (False, None, 1, "true"):
                with self.subTest(flag=flag), self.assertRaises(archive.ArchiveUnavailable):
                    archive._select({**original, "policy_historical_scope_v1": flag})
