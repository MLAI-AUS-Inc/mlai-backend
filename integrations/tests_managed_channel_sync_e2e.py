"""Signed provider webhook → persisted work → real loopback Buzz relay.

Run with MANAGED_CHANNEL_E2E_RELAY_URL, MANAGED_CHANNEL_E2E_RELAY_KEY and
MANAGED_CHANNEL_E2E_COMMUNITY on a disposable, seeded relay and Django database.
Provider HTTP is replaced at the transport boundary. Only the test relay can
receive network requests.
"""
from datetime import timedelta
import hashlib
import hmac
import json
import os
import time
from unittest import skipUnless
from unittest.mock import patch
from urllib.parse import urlsplit
from uuid import uuid4

import requests
from django.test import TestCase
from django.utils import timezone

from integrations.models import ManagedDelivery, ManagedReceipt, ManagedReference, ManagedRoom
from integrations.services.luma import LumaAPIError
from integrations.services.managed_channels import luma, worker
from integrations.services.managed_channels.relay_client import CREATE_KIND, METADATA_KIND, _read, _write
from roo.native_points_requests import RooRelay, tags


RELAY_URL = os.environ.get("MANAGED_CHANNEL_E2E_RELAY_URL", "")
SECRET = "managed-sync-fixture"


def response(body, status=200):
    result = requests.Response()
    result.status_code = status
    result.headers["Content-Type"] = "application/json"
    result._content = json.dumps(body).encode()
    return result


@skipUnless(RELAY_URL, "requires an isolated loopback Buzz relay")
class ManagedChannelEndToEnd(TestCase):
    def setUp(self):
        address = urlsplit(RELAY_URL)
        self.assertEqual((address.scheme, address.hostname), ("https", "127.0.0.1"))
        self.assertIsNotNone(address.port)
        self.community = os.environ["MANAGED_CHANNEL_E2E_COMMUNITY"]
        self.relay_key = os.environ["MANAGED_CHANNEL_E2E_RELAY_KEY"]
        self.ids = {name: str(uuid4()) for name in ("linear", "team", "project", "plane", "mirror", "native")}
        self.event_id = "evt-fixture"
        self.calendar_id = "cal-fixture"
        self.now = timezone.now()
        self.linear_rows = {}
        self.plane_rows = {}
        self.luma_rows = {}
        self.luma_incomplete = False
        self.links = []
        self.linear_writes = 0
        base = {
            "community": self.community,
            "enabled": True,
            "write_chat": True,
            "api_key_env": "MANAGED_CHANNEL_E2E_PROVIDER_KEY",
            "webhook_secret_env": "MANAGED_CHANNEL_E2E_PROVIDER_SECRET",
        }
        self.sources = {
            "linear": {**base, "provider": "linear", "scope": self.ids["linear"],
                       "team_ids": [self.ids["team"]], "public_project_ids": [self.ids["project"]],
                       "write_backlinks": True, "chat_origin": RELAY_URL},
            "plane": {**base, "provider": "plane", "scope": self.ids["plane"], "workspace_slug": "fixture",
                      "api_origin": "https://plane.example.test", "public_origin": "https://plane.example.test",
                      "public_workspace_projects": True, "linear_scope": self.ids["linear"],
                      "linear_project_map": {self.ids["project"]: {"id": self.ids["mirror"]}}},
            "luma": {**base, "provider": "luma", "scope": self.calendar_id, "public_event_rooms": True},
        }
        self.enterContext(self.settings(
            MANAGED_CHANNEL_SOURCES=self.sources,
            COMMUNITY_CHAT_RELAY_URL=RELAY_URL,
            LUMA_BASE_URL="https://luma.example.test",
        ))
        self.enterContext(patch.dict(os.environ, {
            "MANAGED_CHANNEL_E2E_PROVIDER_KEY": SECRET,
            "MANAGED_CHANNEL_E2E_PROVIDER_SECRET": SECRET,
            "MANAGED_CHANNEL_RELAY_COMMUNITY_ID": self.community,
            "MANAGED_CHANNEL_RELAY_SIGNING_KEY": self.relay_key,
        }))
        original_request = requests.Session.request

        def transport(session, method, url, **kwargs):
            host = urlsplit(url).netloc
            if host == address.netloc:
                return original_request(session, method, url, **kwargs)
            if host == "api.linear.app":
                self.assertEqual(method, "POST")
                self.assertEqual(kwargs["headers"]["Authorization"], SECRET)
                return self.linear(**kwargs["json"])
            if host == "plane.example.test":
                self.assertEqual(method, "GET")
                self.assertEqual(kwargs["headers"]["X-API-Key"], SECRET)
                return self.plane(urlsplit(url).path)
            if host == "luma.example.test":
                self.assertEqual(method, "GET")
                self.assertEqual(kwargs["headers"]["x-luma-api-key"], SECRET)
                return self.calendar(urlsplit(url).path, kwargs["params"])
            raise AssertionError(f"Unexpected outbound request to {host}")

        self.enterContext(patch.object(requests.Session, "request", transport))
        self.relay = RooRelay(self.relay_key)

    def linear(self, query, variables):
        organization = {"id": self.ids["linear"]}
        page = {"hasNextPage": False}
        data = {"organization": organization}
        if "ManagedChannelProjects(" in query:
            data["projects"] = {"nodes": list(self.linear_rows.values()), "pageInfo": page}
        elif "ManagedChannelProject(" in query:
            row = self.linear_rows.get(variables["id"])
            if row is None:
                return response({"errors": [{"path": ["project"], "extensions": {"code": "NOT_FOUND"}}]})
            data["project"] = row
        elif "ManagedChannelLinks(" in query:
            data["project"] = {"id": variables["id"], "externalLinks": {"nodes": self.links, "pageInfo": page}}
        elif "ManagedChannelLinkCreate(" in query:
            link = variables["input"]
            self.assertEqual(link["projectId"], self.ids["project"])
            self.linear_writes += 1
            self.links.append(link)
            data["entityExternalLinkCreate"] = {"success": True, "entityExternalLink": link}
        else:
            self.assertIn("ManagedChannelWorkspace", query)
        return response({"data": data})

    def plane(self, path):
        prefix = "/api/v1/workspaces/fixture/"
        self.assertTrue(path.startswith(prefix))
        relative = path.removeprefix(prefix)
        if relative in {"projects-lite/", "projects/"}:
            return response({"results": [{"id": key, "workspace": self.ids["plane"]} for key in self.plane_rows],
                             "next_page_results": False})
        project_id = relative.removeprefix("projects/").rstrip("/")
        self.assertTrue(relative.startswith("projects/"))
        return response(self.plane_rows[project_id]) if project_id in self.plane_rows else response({}, 404)

    def calendar(self, path, params):
        if path == "/v1/calendars/get":
            return response({"id": self.calendar_id})
        if path == "/v1/calendars/events/list":
            self.assertEqual(params["status"], "approved")
            if self.luma_incomplete:
                return response({"entries": [], "has_more": True})
            return response({"entries": list(self.luma_rows.values()), "has_more": False})
        self.assertEqual(path, "/v1/calendars/events/lookup")
        return response({"event": self.luma_rows.get(params["event_id"])})

    def webhook(self, provider, action, resource, *, duplicate=False):
        identity = self.sources[provider]["scope"]
        epoch = int(time.time())
        delivery = str(uuid4())
        if provider == "linear":
            payload = {"type": "Project", "action": action, "organizationId": identity,
                       "webhookTimestamp": epoch * 1000, "data": {"id": resource}}
        elif provider == "plane":
            payload = {"event": "project", "action": action, "workspace_id": identity, "data": {"id": resource}}
        else:
            payload = {"type": action, "data": {"id": resource, "calendar_id": identity}}
        body = json.dumps(payload).encode()
        signed = f"{epoch}.".encode() + body if provider == "luma" else body
        digest = hmac.new(SECRET.encode(), signed, hashlib.sha256).hexdigest()
        if provider == "linear":
            headers = {"Linear-Signature": digest, "Linear-Delivery": delivery, "Linear-Event": "Project"}
        elif provider == "plane":
            headers = {"X-Plane-Signature": digest, "X-Plane-Delivery": delivery, "X-Plane-Event": "project"}
        else:
            headers = {"Webhook-Signature": f"t={epoch},v1={digest}", "Webhook-Id": delivery}
        path = f"/api/v1/integrations/managed-channels/webhooks/{provider}"
        for _ in range(2 if duplicate else 1):
            result = self.client.post(path, body, content_type="application/json", headers=headers, secure=True)
            self.assertEqual(result.status_code, 200, result.content[:250])
        self.assertEqual(ManagedReceipt.objects.filter(delivery_id=delivery).count(), 1)

    def drain(self):
        for _ in range(2):
            self.assertEqual(worker.tick(), {"status": "ok"})
        self.assertFalse(ManagedReceipt.objects.filter(completed_at=None).exists(),
                         list(ManagedReceipt.objects.filter(completed_at=None).values_list("action", "error")))
        self.assertFalse(ManagedDelivery.objects.exclude(error="").exists(),
                         list(ManagedDelivery.objects.exclude(error="").values_list("target", "error")))

    def room(self, provider, resource):
        ref = ManagedReference.objects.get(scope__provider=provider, external_id=resource)
        return str(ManagedRoom.objects.get(entity=ref.entity).channel_id)

    def projection(self, room, section, title, status):
        found = _read(self.relay, room, METADATA_KIND)
        self.assertIsNotNone(found)
        for name, value in (("channel_section", section), ("managed_title", title), ("source_status", status)):
            self.assertEqual(tags(found, name), [[name, value]])
        self.assertEqual(len(self.relay.query(room, [CREATE_KIND], authors=[self.relay.pubkey])), 1)
        return found

    def test_all_sources_publish_lifecycle_transitions_to_the_same_chat_rooms(self):
        ids = self.ids
        self.drain()  # Baseline inventories are empty. Creates below start with signed webhooks.
        self.linear_rows[ids["project"]] = {
            "id": ids["project"], "name": "Linear initial title", "url": "https://linear.app/fixture/project/test",
            "updatedAt": self.now.isoformat(), "archivedAt": None, "trashed": False,
            "status": {"name": "Started", "type": "started"},
            "teams": {"nodes": [{"id": ids["team"], "private": False}], "pageInfo": {"hasNextPage": False}},
        }
        self.plane_rows[ids["native"]] = {
            "id": ids["native"], "workspace": ids["plane"], "name": "Native Plane project",
            "network": 2, "updated_at": self.now.isoformat(), "archived_at": None,
        }
        self.luma_rows[self.event_id] = {
            "id": self.event_id, "name": "Fixture event", "platform": "luma", "visibility": "public",
            "url": "https://luma.com/fixture", "timezone": "Australia/Melbourne",
            "start_at": (self.now + timedelta(hours=1)).isoformat(),
            "end_at": (self.now + timedelta(hours=2)).isoformat(),
        }
        self.webhook("linear", "create", ids["project"], duplicate=True)
        self.webhook("plane", "created", ids["native"])
        self.webhook("luma", "event.created", self.event_id, duplicate=True)
        self.drain()
        project_room = self.room("linear", ids["project"])
        plane_room = self.room("plane", ids["native"])
        event_room = self.room("luma", self.event_id)
        self.projection(project_room, "project", "Linear initial title", "started")
        self.projection(plane_room, "project", "Native Plane project", "active")
        self.projection(event_room, "event", "Fixture event", "scheduled")
        self.assertEqual([link["url"] for link in self.links], [f"{RELAY_URL}/channels/{project_room}"])

        self.linear_rows[ids["project"]].update(name="Linear renamed", status={"name": "Completed", "type": "completed"})
        self.webhook("linear", "update", ids["project"])
        self.drain()
        self.projection(project_room, "project", "Linear renamed", "completed")
        self.assertEqual(self.linear_writes, 1)

        removed_project = self.linear_rows.pop(ids["project"])
        self.webhook("linear", "remove", ids["project"])
        self.drain()
        metadata = self.projection(project_room, "project", "Linear renamed", "removed")
        self.assertFalse(tags(metadata, "source_url"))
        self.linear_rows[ids["project"]] = removed_project
        self.webhook("linear", "update", ids["project"])
        self.drain()
        self.projection(project_room, "project", "Linear renamed", "completed")
        self.assertEqual(self.linear_writes, 1)

        self.plane_rows[ids["mirror"]] = {
            **self.plane_rows[ids["native"]], "id": ids["mirror"], "name": "Plane after migration",
            "external_source": "linear-mlai-20260907", "external_id": ids["project"],
        }
        self.sources["plane"]["authoritative_project_ids"] = [ids["mirror"]]
        self.webhook("plane", "created", ids["mirror"])
        self.drain()
        self.assertEqual(self.room("plane", ids["mirror"]), project_room)
        metadata = self.projection(project_room, "project", "Plane after migration", "active")
        self.assertEqual({tag[1] for tag in tags(metadata, "source_link")}, {"linear", "plane"})
        self.linear_rows[ids["project"]]["name"] = "Obsolete Linear title"
        self.webhook("linear", "update", ids["project"])
        self.drain()
        self.projection(project_room, "project", "Plane after migration", "active")
        self.assertEqual(ManagedRoom.objects.count(), 3)

        self.plane_rows[ids["native"]]["archived_at"] = self.now.isoformat()
        worker.scan(self.sources["plane"], force=True)  # Archive can arrive through polling alone.
        self.drain()
        self.projection(plane_room, "project", "Native Plane project", "archived")
        self.plane_rows[ids["native"]]["archived_at"] = None
        worker.scan(self.sources["plane"], force=True)
        self.drain()
        self.projection(plane_room, "project", "Native Plane project", "active")
        del self.plane_rows[ids["native"]]
        self.webhook("plane", "deleted", ids["native"])
        self.drain()
        metadata = self.projection(plane_room, "project", "Native Plane project", "removed")
        self.assertFalse(tags(metadata, "source_url"))

        self.luma_rows[self.event_id]["name"] = "Fixture event renamed"
        self.luma_rows[self.event_id]["start_at"] = (self.now + timedelta(hours=3)).isoformat()
        self.luma_rows[self.event_id]["end_at"] = (self.now + timedelta(hours=4)).isoformat()
        self.webhook("luma", "event.updated", self.event_id)
        self.drain()
        event = self.projection(event_room, "event", "Fixture event renamed", "scheduled")
        self.assertEqual(tags(event, "source_start")[0][1], self.luma_rows[self.event_id]["start_at"])
        for hours, phase in ((3, "live"), (4, "ended")):
            with patch.object(luma, "timezone", wraps=timezone) as clock:
                clock.now.return_value = self.now + timedelta(hours=hours, seconds=1)
                self.drain()
            self.projection(event_room, "event", "Fixture event renamed", phase)

        _write(self.relay, 9, [["h", event_room]], "A follow-up after the event")
        deadline = time.monotonic() + 25
        while not tags(_read(self.relay, event_room, METADATA_KIND), "last_conversation_at"):
            self.assertLess(time.monotonic(), deadline, "relay did not reproject conversation activity")
            time.sleep(0.3)

        del self.luma_rows[self.event_id]
        self.luma_incomplete = True
        with self.assertRaises(LumaAPIError):
            worker.scan(self.sources["luma"], force=True)
        self.projection(event_room, "event", "Fixture event renamed", "ended")
        self.luma_incomplete = False
        self.webhook("luma", "event.canceled", self.event_id)
        self.drain()
        event = self.projection(event_room, "event", "Fixture event renamed", "canceled")
        self.assertFalse(tags(event, "source_url"))
        self.assertTrue(tags(event, "last_conversation_at"))

        second_id = "evt-fixture-second"
        self.luma_rows[second_id] = {
            "id": second_id, "name": "Fixture event renamed", "platform": "luma", "visibility": "public",
            "url": "https://luma.com/fixture-repeat", "timezone": "Australia/Melbourne",
            "start_at": (self.now + timedelta(days=1)).isoformat(),
            "end_at": (self.now + timedelta(days=1, hours=1)).isoformat(),
        }
        self.webhook("luma", "event.created", second_id)
        self.drain()
        second_room = self.room("luma", second_id)
        self.assertNotEqual(second_room, event_room)
        self.projection(second_room, "event", "Fixture event renamed", "scheduled")
        del self.luma_rows[second_id]
        worker.scan(self.sources["luma"], force=True)
        self.drain()
        removed_event = self.projection(second_room, "event", "Fixture event renamed", "removed")
        self.assertFalse(tags(removed_event, "source_url"))
        self.projection(event_room, "event", "Fixture event renamed", "canceled")
