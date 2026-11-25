import hashlib
import hmac
import json
from datetime import datetime, timezone
from unittest import TestCase as UnitTestCase
from unittest.mock import Mock, patch

from django.test import TestCase, override_settings

from integrations.models import ManagedDelivery, ManagedEntity, ManagedReceipt, ManagedReference, ManagedRoom
from integrations.services.managed_channels import luma, slack, worker
from integrations.services.managed_channels.registry import observe, scope_for

CFG = {"community": "486d74ab-0b26-4352-8395-beeb5775cb96", "scope": "cal-mlai", "provider": "luma",
       "enabled": True, "write_chat": True, "public_event_rooms": True}


def sample(event_id="evt-one", title="Ada Conference"):
    return {"id": event_id, "name": title, "platform": "luma", "visibility": "public",
            "start_at": "2035-11-10T08:00:00Z", "end_at": "2035-11-10T09:00:00Z",
            "url": "https://luma.com/example", "timezone": "Australia/Melbourne"}


class EventContracts(UnitTestCase):
    def test_time_bounds_and_unknown_end(self):
        start = datetime(2026, 11, 23, 3, tzinfo=timezone.utc)
        end = datetime(2026, 11, 23, 4, tzinfo=timezone.utc)
        self.assertEqual(luma.event_status(start, end, start), "live")
        self.assertEqual(luma.event_status(start, end, end), "ended")
        self.assertEqual(luma.event_status(start, None, end), "unknown")

    def test_unconfirmed_pagination_cannot_count_as_a_complete_inventory(self):
        from integrations.services.luma import LumaAPIError, LumaAttendeeReportService
        service = LumaAttendeeReportService(api_key="fixture")
        with patch.object(service, "_get", return_value={"entries": []}):
            with self.assertRaises(LumaAPIError):
                service.list_crm_events(strict_pagination=True)

    def test_guest_host_fields_and_private_visibility(self):
        row = {**sample(), "visibility": "private", "meeting_url": "https://example.org/private",
               "registration_questions": [{"foo": "bar"}]}
        normalized = luma.snapshot(row, CFG)
        self.assertFalse(normalized["public_authorized"])
        self.assertNotIn("meeting_url", normalized)
        external = luma.snapshot({**sample(), "platform": "external", "visibility": None}, CFG)
        self.assertFalse(external["public_authorized"])

    def test_luma_signing_and_scope(self):
        body = json.dumps({"type":"event.created","data":{"calendar_id":"cal-mlai","id":"evt-one"}}).encode()
        sig = hmac.new(b"fixture", b"12345." + body, hashlib.sha256).hexdigest()
        headers = {"Webhook-Signature": f"t=12345,v1={sig}", "Webhook-Id":"delivery-one"}
        with patch.dict("os.environ", MANAGED_CHANNEL_LUMA_WEBHOOK_SECRET="fixture"), patch("time.time", return_value=12346):
            self.assertEqual(luma.decode_webhook("luma", CFG, headers, body), ("delivery-one", "evt-one", "refresh"))
            with self.assertRaises(PermissionError):
                luma.decode_webhook("luma", {**CFG, "scope":"another"}, headers, body)
            with self.assertRaises(PermissionError):
                luma.decode_webhook("luma", CFG, headers, body + b" ")

    def test_missing_listing_for_current_calendar(self):
        service = Mock()
        service._get.return_value = {"event": None}
        self.assertIsNone(luma.confirm_missing(service, "evt-one", {}))
        service._get.assert_called_once_with("/v1/calendars/events/lookup", params={"platform":"luma", "event_id":"evt-one"})
        service._get.return_value = {}
        with self.assertRaises(ValueError):
            luma.confirm_missing(service, "evt-one", {})

    @override_settings(MANAGED_CHANNEL_SOURCES={})
    def test_legacy_bridge_skips_missing_optional_registry(self):
        with patch.object(slack.connection.introspection, "table_names", return_value=[]):
            self.assertFalse(slack.metadata_only("T123", "C123", "general"))

    def test_slack_capability_closed_without_workspace_grant(self):
        config = {**CFG, "slack_workspace_id":"T123"}
        with patch.dict("os.environ", MANAGED_CHANNEL_SLACK_BOT_TOKEN="xoxb-fixture"), patch.object(slack.http_client, "post") as call:
            call.return_value.headers = {"x-oauth-scopes":"chat:write"}
            call.return_value.json.return_value = {"ok":True, "team_id":"T123","bot_id":"B123"}
            with self.assertRaises(slack.SlackHold):
                slack.capabilities(config)
            self.assertEqual(call.call_count, 1)


@override_settings(MANAGED_CHANNEL_SOURCES={"mlai": CFG})
class EventPersistence(TestCase):
    def test_rename_and_retry_preserve_room_occurrences_and_revision(self):
        scope = scope_for(CFG)
        first = observe(scope, CFG, luma.snapshot(sample(), CFG))
        room = ManagedRoom.objects.get(entity=first)
        renamed = luma.snapshot(sample(title="New Ada"), CFG)
        second = observe(scope, CFG, renamed)
        third = observe(scope, CFG, renamed)
        self.assertEqual(second.revision, third.revision)
        self.assertEqual(ManagedRoom.objects.get(entity=second).channel_id, room.channel_id)
        fourth = observe(scope, CFG, luma.snapshot(sample("evt-two", "New Ada"), CFG))
        self.assertNotEqual(first.pk, fourth.pk)
        self.assertEqual(ManagedDelivery.objects.filter(target="chat").count(), 2)

    def test_duplicate_cancellation_and_privacy_reduction(self):
        scope = scope_for(CFG)
        snap = luma.snapshot(sample(), CFG)
        observed = observe(scope, CFG, snap)
        self.assertTrue(worker.receive(CFG, "wh-1", "evt-one", "canceled"))
        self.assertFalse(worker.receive(CFG, "wh-1", "evt-one", "canceled"))
        with patch.object(luma, "get_snapshot", return_value=snap):
            worker.refresh(ManagedReceipt.objects.get().pk, CFG)
        self.assertEqual(ManagedEntity.objects.get(pk=observed.pk).snapshot["status"], "canceled")
        restricted = observe(scope, CFG, {**snap, "title":"Secret", "visibility":"private", "public_authorized":False})
        self.assertEqual(restricted.snapshot["status"], "restricted")
        self.assertNotIn("Secret", json.dumps(worker._payload(restricted)))

    def test_failed_inventory_preserves_bindings_and_skips_missing_checks(self):
        scope = scope_for(CFG)
        observe(scope, CFG, luma.snapshot(sample(), CFG))
        with patch.object(luma, "inventory", side_effect=RuntimeError("partial page")):
            with self.assertRaises(RuntimeError):
                worker.scan(CFG, force=True)
        self.assertFalse(ManagedReceipt.objects.exists())
        self.assertEqual(ManagedReference.objects.get().snapshot["status"], "scheduled")

    def test_metadata_only_slack_creation_visible_before_discovery_and_indeterminate_retry(self):
        scope = scope_for(CFG)
        entity = observe(scope, CFG, luma.snapshot(sample(), CFG))
        room = ManagedRoom.objects.get(entity=entity)
        conf = {**CFG, "slack_events":True, "slack_workspace_id":"T123"}
        def timeout_after_create(method, key, payload):
            self.assertTrue(slack.metadata_only("T123", "CNEW", payload["name"]))
            raise TimeoutError()
        with patch.object(slack, "capabilities", return_value="test"), patch.object(slack, "_call", side_effect=timeout_after_create) as call:
            with self.assertRaises(TimeoutError):
                slack.sync(conf, room, entity.snapshot)
            with self.assertRaises(slack.SlackHold):
                slack.sync(conf, room, entity.snapshot)
            self.assertEqual(call.call_count, 1)

    def test_alias_arrives_first_and_authority_cutover_reuses_the_room(self):
        linear_cfg = {**CFG, "scope":"linear-workspace", "provider":"linear"}
        plane_cfg = {**CFG, "scope":"plane-workspace", "provider":"plane"}
        external_project = {
            "external_id":"p-1", "title":"Project", "status":"active", "visibility":"public",
            "public_authorized":True, "url":"https://plane.example/project",
            "alias":{"provider":"linear","scope":"linear-workspace","external_id":"l-1"},
        }
        linear_project = {k:v for k,v in external_project.items() if k != "alias"}
        linear_project.update(external_id="l-1", title="Linear project", url="https://linear.app/acme/project/test")
        with override_settings(MANAGED_CHANNEL_SOURCES={"l":linear_cfg, "p":plane_cfg}):
            linear_scope, plane_scope = scope_for(linear_cfg), scope_for(plane_cfg)
            first = observe(plane_scope, plane_cfg, external_project)
            self.assertFalse(ManagedRoom.objects.exists())
            after_linear = observe(linear_scope, linear_cfg, linear_project)
            self.assertEqual(first.pk, after_linear.pk)
            room_id = ManagedRoom.objects.get().channel_id
            self.assertEqual(after_linear.snapshot["title"], "Linear project")
            self.assertEqual(set(after_linear.snapshot["links"]), {"linear","plane"})
            cutover = observe(plane_scope, {**plane_cfg,"authoritative_project_ids":["p-1"]}, external_project)
            self.assertEqual(cutover.snapshot["title"], "Project")
            late = observe(linear_scope, linear_cfg, {**linear_project, "title":"Late old title"})
            self.assertEqual(late.snapshot["title"], "Project")
            self.assertEqual(ManagedRoom.objects.get().channel_id, room_id)
            self.assertEqual(ManagedReference.objects.count(), 2)

    def test_curated_command_preserves_uuid_and_requires_explicit_provider_adoption(self):
        from io import StringIO
        from uuid import uuid4
        from django.core.management import call_command

        channel_id = uuid4()
        args = ["--community", CFG["community"], "--room", str(channel_id), "--section", "committee",
                "--visibility", "public"]
        with patch("integrations.management.commands.classify_chat_channel.apply_room") as send:
            call_command("classify_chat_channel", *args, stdout=StringIO())
            self.assertFalse(ManagedRoom.objects.exists())
            call_command("classify_chat_channel", *args, "--apply", stdout=StringIO())
            call_command("classify_chat_channel", *args, "--apply", stdout=StringIO())
            self.assertEqual(send.call_args.args[-1], {"revision":1,"section":"committee","status":"active"})
        scope = scope_for(CFG)
        snap = luma.snapshot(sample(), CFG)
        config = {**CFG, "bindings":{"evt-one":{"channel_id":str(channel_id)}}}
        with self.assertRaises(ValueError):
            observe(scope, config, snap)
        config["bindings"]["evt-one"]["adopt_curated"] = True
        entity = observe(scope, config, snap)
        room = ManagedRoom.objects.get(entity=entity)
        self.assertEqual(room.channel_id, channel_id)
        self.assertEqual(room.section_override, "committee")
        self.assertGreater(entity.revision, 1)
        self.assertEqual(ManagedRoom.objects.count(), 1)
