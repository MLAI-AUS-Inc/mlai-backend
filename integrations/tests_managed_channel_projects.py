"""Provider contract and replay tests. All outbound HTTP is replaced with fixtures."""

import hashlib
import hmac
import json
import os
import time
from unittest import TestCase
from unittest.mock import Mock, patch
from uuid import UUID, uuid4

from integrations.services.managed_channels import projects


def uid():
    return str(uuid4())


def http_response(body, status=200):
    return Mock(status_code=status, json=Mock(return_value=body))


class ManagedProjectProvidersTests(TestCase):
    def setUp(self):
        self.scope, self.team, self.project, self.room = (uid() for _ in range(4))
        self.config = {
            "scope": self.scope, "community": "mlai", "team_ids": [self.team], "workspace_slug": "mlai",
            "public_project_ids": [self.project], "webhook_secret_env": "MANAGED_CHANNEL_TEST_SECRET",
            "api_key_env": "MANAGED_CHANNEL_TEST_KEY", "slack_workspace_id": "T0123",
        }
        self.keys = patch.dict(os.environ, {"MANAGED_CHANNEL_TEST_SECRET": "fixture", "MANAGED_CHANNEL_TEST_KEY": "fixture"})
        self.keys.start()
        self.addCleanup(self.keys.stop)

    def linear_project(self, **changes):
        return {
            "id": self.project, "name": "Example", "url": "https://linear.app/mlai/project/example",
            "updatedAt": "2026-11-23T01:00:00Z", "archivedAt": None, "trashed": False,
            "status": {"name": "Closed", "type": "completed"}, "slackChannelId": "C0123",
            "teams": {"nodes": [{"id": self.team, "private": False}], "pageInfo": {"hasNextPage": False}},
            **changes,
        }

    def plane_project(self, **changes):
        return {
            "id": self.project, "workspace": self.scope, "name": "Native", "network": 0,
            "updated_at": "2026-11-23T01:00:00Z", "archived_at": None, **changes,
        }

    def signed(self, provider, payload, delivery=None):
        body = json.dumps(payload).encode()
        prefix = "Linear" if provider == "linear" else "X-Plane"
        return {
            prefix + "-Signature": hmac.new(b"fixture", body, hashlib.sha256).hexdigest(),
            prefix + "-Event": payload.get("event") or payload.get("type"),
            prefix + "-Delivery": delivery or uid(),
        }, body

    def test_linear_identity_acl_and_lifecycle(self):
        native = self.linear_project()
        snap = projects._linear_snapshot(self.config, native)
        self.assertTrue(snap["public_authorized"])
        self.assertEqual(snap["status"], "completed")
        self.assertEqual(snap["slack_channel_id"], native["slackChannelId"])
        private = self.linear_project(archivedAt="2026-11-23", teams={
            "nodes": [{"id": self.team, "private": False}, {"id": uid(), "private": True}],
            "pageInfo": {"hasNextPage": False},
        })
        snap = projects._linear_snapshot(self.config, private)
        self.assertEqual((snap["status"], snap["visibility"], snap["public_authorized"]), ("archived", "private", False))
        with self.assertRaises(projects.ProjectExcluded):
            projects._linear_snapshot({**self.config, "team_ids": [uid()]}, native)

    def test_policy_can_provision_future_projects_only_for_confirmed_public_sources(self):
        config = {**self.config, "public_project_ids": [], "public_team_ids": [self.team],
                  "public_workspace_projects": True}
        row = self.linear_project()
        self.assertFalse(projects._linear_snapshot(config, row)["public_authorized"])
        config["full_team_visibility_verified"] = True
        self.assertTrue(projects._linear_snapshot(config, row)["public_authorized"])
        row["teams"]["nodes"][0]["private"] = True
        self.assertFalse(projects._linear_snapshot(config, row)["public_authorized"])
        self.assertTrue(projects._plane_snapshot(config, self.plane_project(network=2), self.project)["public_authorized"])
        self.assertFalse(projects._plane_snapshot(config, self.plane_project(network=0), self.project)["public_authorized"])

    @patch.object(projects.http_client, "post")
    def test_linear_inventory_rejects_repeated_cursor_and_wrong_workspace(self, post):
        first = {"nodes": [self.linear_project()], "pageInfo": {"hasNextPage": True, "endCursor": "one"}}
        post.return_value = http_response({"data": {"organization": {"id": self.scope}, "projects": first}})
        with self.assertRaises(projects.ProjectAccessUnconfirmed):
            projects.inventory("linear", self.config)
        self.assertEqual(post.call_args.kwargs["json"]["variables"], {"after": "one"})
        post.return_value = http_response({"data": {"organization": {"id": uid()}, "projects": first}})
        with self.assertRaises(PermissionError):
            projects.inventory("linear", self.config)

    @patch.object(projects.http_client, "post")
    def test_linear_additional_team_page_prevents_publishing_a_private_project(self, post):
        first = self.linear_project()
        first["teams"]["pageInfo"] = {"hasNextPage": True, "endCursor": "more"}
        post.return_value = http_response({"data": {"organization": {"id": self.scope}, "project": {
            "id": self.project, "teams": {"nodes": [{"id": uid(), "private": True}], "pageInfo": {"hasNextPage": False}},
        }}})
        self.assertFalse(projects._linear_snapshot(self.config, first)["public_authorized"])
        self.assertEqual(post.call_args.kwargs["json"]["variables"]["after"], "more")

    @patch.object(projects.http_client, "get")
    def test_plane_inventory_hydrates_archived_projects_and_reads_all_pages(self, get):
        second = uid()
        get.side_effect = [
            http_response({"results": [{"id": self.project}], "next_page_results": True, "next_cursor": "next"}),
            http_response(self.plane_project()),
            http_response({"results": [{"id": second}], "next_page_results": False}),
            http_response(self.plane_project(id=second, archived_at="2026-11-22", network=2)),
        ]
        rows = projects.inventory("plane", self.config)
        self.assertEqual([(r["external_id"], r["status"]) for r in rows], [(self.project, "active"), (second, "archived")])
        self.assertTrue(get.call_args_list[0].args[0].endswith("/projects-lite/"))
        self.assertEqual(get.call_args_list[0].kwargs["params"]["include_archived"], "true")
        self.assertEqual(get.call_args_list[2].kwargs["params"]["cursor"], "next")
        self.assertFalse(rows[1]["public_authorized"])

    def test_mirror_requires_verified_ids_and_excludes_team_backlogs(self):
        linear_id, linear_scope = uid(), uid()
        row = self.plane_project(external_source="linear-mlai-20260907", external_id=linear_id)
        with self.assertRaises(projects.MirrorIdentityUnconfirmed):
            projects._plane_snapshot(self.config, row, self.project)
        config = {**self.config, "linear_scope": linear_scope, "linear_project_map": {linear_id: {"id": self.project}}}
        self.assertEqual(projects._plane_snapshot(config, row, self.project)["alias"],
                         {"scope": linear_scope, "provider": "linear", "external_id": linear_id})
        with self.assertRaises(projects.ProjectExcluded):
            projects._plane_snapshot(config, dict(row, external_id="backlog:" + self.team), self.project)
        with self.assertRaises(PermissionError):
            projects._plane_snapshot(config, dict(row, workspace=uid()), self.project)

    @patch.object(projects.http_client, "get")
    def test_unmapped_mirror_queues_its_own_hold_alongside_native_project(self, get):
        native = uid()
        get.side_effect = [
            http_response({"results": [{"id": self.project}, {"id": native}], "next_page_results": False}),
            http_response(self.plane_project(external_source="linear-mlai-20260907", external_id=uid())),
            http_response(self.plane_project(id=native)),
        ]
        mirror, project = projects.inventory("plane", self.config)
        self.assertEqual(mirror, {"external_id": self.project, "hold_reason": "mirror_identity_unconfirmed"})
        self.assertEqual(project["external_id"], native)

    def test_webhooks_check_signed_scope_replay_window_and_entity_identity(self):
        payload = {"organizationId": self.scope, "type": "Project", "action": "remove",
                   "webhookTimestamp": int(time.time() * 1000), "data": {"id": self.project}}
        headers, body = self.signed("linear", payload)
        self.assertEqual(projects.decode_webhook("linear", self.config, headers, body)[1:], (self.project, "removed"))
        with self.assertRaises(PermissionError):
            projects.decode_webhook("linear", self.config, headers, body + b" ")
        headers, body = self.signed("linear", dict(payload, webhookTimestamp=1))
        with self.assertRaises(PermissionError):
            projects.decode_webhook("linear", self.config, headers, body)
        headers, body = self.signed("linear", dict(payload, organizationId=uid()))
        with self.assertRaises(PermissionError):
            projects.decode_webhook("linear", self.config, headers, body)
        payload.update(type="ProjectUpdate", data={"id": uid(), "projectId": self.project})
        headers, body = self.signed("linear", payload)
        self.assertEqual(projects.decode_webhook("linear", self.config, headers, body)[1:], (self.project, "refresh"))
        v1 = {"workspace_id": self.scope, "event": "project", "action": "deleted", "data": {"id": self.project}}
        headers, body = self.signed("plane", v1)
        self.assertEqual(projects.decode_webhook("plane", self.config, headers, body)[1:], (self.project, "removed"))
        event_id = uid()
        for _ in range(2):
            delivery_id = uid()
            v2 = {"workspace_id": self.scope, "version": "v2", "delivery_id": delivery_id, "event_id": event_id,
                  "entity_id": self.project, "event": "project.deleted", "data": {}}
            headers, body = self.signed("plane", v2, delivery_id)
            self.assertEqual(projects.decode_webhook("plane", self.config, headers, body), (event_id, self.project, "removed"))

    @patch.object(projects.http_client, "get")
    def test_unexplained_loss_is_not_a_removal(self, get):
        verified_scope = http_response({"results": [{"id": uid(), "workspace": self.scope}]})
        get.side_effect = [http_response({}, 404), verified_scope]
        with self.assertRaises(projects.ProjectNotFound):
            projects.get_snapshot("plane", self.config, self.project)
        get.side_effect = [http_response({}, 403), verified_scope]
        with self.assertRaises(projects.ProjectAccessRevoked):
            projects.get_snapshot("plane", self.config, self.project)
        get.side_effect = [http_response({}, 404), http_response({"results": [{"id": uid(), "workspace": uid()}]})]
        with self.assertRaises(projects.ProjectAccessUnconfirmed) as error:
            projects.get_snapshot("plane", self.config, self.project)
        self.assertNotIsInstance(error.exception, projects.ProjectNotFound)

    @patch.object(projects.http_client, "post")
    def test_backlink_after_lost_response_converges_without_rewriting_other_links(self, post):
        links = [{"id": uid(), "label": "Notes", "url": "https://docs.example.test/notes"}]
        writes = []

        def graphql(_url, **kwargs):
            payload = kwargs["json"]
            query, variables = payload["query"], payload["variables"]
            if "ManagedChannelProject(" in query:
                row = self.linear_project()
            elif "ManagedChannelLinks(" in query:
                row = {"id": self.project, "externalLinks": {"nodes": links.copy(), "pageInfo": {"hasNextPage": False}}}
            else:
                record = variables["input"]
                self.assertEqual(record["projectId"], self.project)
                self.assertEqual(UUID(record["id"]).version, 4)
                links.append(record.copy())
                writes.append(record["id"])
                raise TimeoutError("response lost")
            return http_response({"data": {"organization": {"id": self.scope}, "project": row}})

        post.side_effect = graphql
        with self.assertRaises(TimeoutError):
            projects.sync_backlink("linear", self.config, self.project, self.room)
        result = projects.sync_backlink("linear", self.config, self.project, self.room)
        self.assertEqual(writes, [result])
        self.assertEqual(links[0]["label"], "Notes")
        self.assertEqual(links[1]["url"], "https://chat.mlai.au/channels/" + self.room)
