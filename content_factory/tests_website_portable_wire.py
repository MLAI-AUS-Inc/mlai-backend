"""Compatibility with actual Content Factory portable callback/mirror output.

The synthetic fixture was captured from Content Factory's existing
test_runner_completes_content_only_without_repo on 2026-10-04, using the real
PipelineRunner finalizer and MlaiRunMirror._status_payload. Only the temporary
artifact-root prefix was normalized; all emitted keys and values are retained.
Models, media and external services were fixtures; no live credentials or data.
"""

from copy import deepcopy
import json
from pathlib import Path
from types import SimpleNamespace

from django.test import SimpleTestCase

from .website_connections import portable_run_update_allowed
from .portable_drafts import PORTABLE_DISPATCH_RESERVATION, portable_dispatch_snapshot_allowed


def admitted_portable_snapshot():
    """Synthetic, validated catalog observations as emitted by the worker."""
    from .tests_editorial_snapshot_unit import admitted_snapshot
    payload = admitted_snapshot()
    payload["run_request"]["editorial_admission"]["github_repo"] = None
    payload["run_request"].update(delivery_mode="content_only", delivery_mode_confirmed=True)
    return payload


class PortableWorkerWireTests(SimpleTestCase):
    def setUp(self):
        self.wire = json.loads((Path(__file__).parent / "testdata" / "portable_worker_wire.json").read_text())
        self.run = SimpleNamespace(run_id="run-content-only-1", workflow="article_generation",
            domain="example.com", github_repo="",
            run_request={"delivery_mode": "content_only", "delivery_mode_confirmed": True})

    def test_actual_progress_and_completion_callbacks_are_portable(self):
        self.assertEqual([payload["event"] for payload in self.wire["callbacks"]],
            ["article_progress", "article_progress", "article_progress", "content_ready"])
        for payload in self.wire["callbacks"]:
            with self.subTest(event=payload["event"], milestone=payload.get("milestone_key")):
                self.assertTrue(portable_run_update_allowed(self.run, payload, event_type=payload["event"]))

    def test_actual_completed_mirror_with_nested_packages_and_steps_is_portable(self):
        payload = self.wire["snapshot"]
        self.assertEqual(payload["status"], "completed")
        self.assertEqual(payload["workflow"], "direct_generate")
        self.assertEqual(payload["publish_resolution"], "content_only")
        self.assertTrue(payload["result"]["content_package"]["article_markdown"])
        self.assertTrue(payload["step_states"])
        self.assertTrue(portable_run_update_allowed(self.run, payload))

    def test_real_payload_cannot_change_authority_or_native_delivery_in_nested_result(self):
        for change in (
            {"resolvedDeliveryMode": "publish_code"},
            {"publish_resolution": "direct_preview_pr"},
            {"live_preview_url": "https://preview.invalid"},
            {"connectionId": "fbc09c73-e449-4c43-88ea-385b249a7a20", "connectionGeneration": 1},
        ):
            payload = deepcopy(self.wire["snapshot"])
            payload["result"].update(change)
            with self.subTest(change=change):
                self.assertFalse(portable_run_update_allowed(self.run, payload))

    def test_real_payload_does_not_override_original_native_intent(self):
        self.run.run_request = {"delivery_mode": "publish_code"}
        self.assertFalse(portable_run_update_allowed(self.run, self.wire["snapshot"]))

    def test_real_payload_does_not_confirm_a_saved_default(self):
        self.run.run_request = {"delivery_mode": "content_only"}
        self.assertFalse(portable_run_update_allowed(self.run, self.wire["snapshot"]))

    def test_approved_editorial_selections_are_not_publication_approval(self):
        payload = admitted_portable_snapshot()
        self.run.domain = payload["domain"]
        for status in ("running", "failed", "completed"):
            with self.subTest(status=status):
                self.assertTrue(portable_run_update_allowed(self.run, {**payload, "status": status}))
        self.run.run_request.update(deepcopy(payload["run_request"]))
        self.assertTrue(portable_run_update_allowed(self.run, payload))

    def test_editorial_observation_cannot_hide_publishing_or_connection_authority(self):
        base = admitted_portable_snapshot()
        self.run.domain = base["domain"]
        for field, value in (
            ("approval_state", "approved"), ("status", "published"),
            ("result", {"status": "approved"}),
            ("result", {"editorial_admission": {"audience": {"status": "approved"}}}),
            ("result", {"connectionId": "synthetic-connection"}),
        ):
            with self.subTest(field=field, value=value):
                self.assertFalse(portable_run_update_allowed(self.run, {**base, field: value}))
        payload = deepcopy(base)
        payload["run_request"]["editorial_admission"]["github_repo"] = "other/site"
        self.assertFalse(portable_run_update_allowed(self.run, payload))

    def test_invalid_or_changed_editorial_observation_still_fails_closed(self):
        base = admitted_portable_snapshot()
        self.run.domain = base["domain"]
        for change in ({"selection_sha256": "0" * 64}, {"domain": "other.example"},
                       {"audience": {"status": "approved"}}, {"connection_id": "synthetic"}):
            payload = deepcopy(base)
            payload["run_request"]["editorial_admission"].update(change)
            with self.subTest(change=change):
                self.assertFalse(portable_run_update_allowed(self.run, payload))
        self.run.run_request.update(deepcopy(base["run_request"]))
        payload = deepcopy(base)
        payload["run_request"]["editorial_admission"]["checked_at"] = "2026-09-11T01:00:00+00:00"
        self.assertFalse(portable_run_update_allowed(self.run, payload))

class PortableDispatchIntentTests(SimpleTestCase):
    def setUp(self):
        self.payload = admitted_portable_snapshot()
        self.payload.update(run_id="remote-draft", workflow="confirmed_topic", status="queued")
        self.payload["run_request"].update(client_request_id="draft-dispatch", topic="A reviewed topic")
        saved = deepcopy(self.payload["run_request"])
        saved.pop("editorial_admission")
        saved.update({PORTABLE_DISPATCH_RESERVATION: True, "dispatch_pending_resolution": True})
        self.run = SimpleNamespace(run_id="draft-dispatch", domain=self.payload["domain"],
            workflow="confirmed_topic", github_repo="", status="queued", run_request=saved)

    def test_first_mirror_binds_only_backend_reserved_original_intent(self):
        self.assertTrue(portable_dispatch_snapshot_allowed(self.run, "remote-draft", self.payload))
        self.run.status = "blocked"  # A lost queue response remains pending.
        self.assertTrue(portable_dispatch_snapshot_allowed(self.run, "remote-draft", self.payload))
        for field in (PORTABLE_DISPATCH_RESERVATION, "dispatch_pending_resolution", "delivery_mode_confirmed"):
            saved = deepcopy(self.run.run_request)
            self.run.run_request.pop(field)
            self.assertFalse(portable_dispatch_snapshot_allowed(self.run, "remote-draft", self.payload))
            self.run.run_request = saved
        self.run.status = "failed"
        self.assertFalse(portable_dispatch_snapshot_allowed(self.run, "remote-draft", self.payload))

    def test_first_mirror_cannot_change_review_or_tenant_or_gain_website_authority(self):
        changes = [
            {"domain": "other.example.test"}, {"workflow": "site_scan"}, {"run_id": "other-run"},
            {"client_request_id": "other-key"}, {"event_type": "preview_ready"},
            {"result": {"preview_url": "https://preview.invalid"}},
            {"run_request": {**self.payload["run_request"], "client_request_id": "other-key"}},
            {"run_request": {**self.payload["run_request"], "topic": "Unreviewed topic"}},
            {"run_request": {**self.payload["run_request"], "delivery_mode": "publish_code"}},
        ]
        changed_brief = deepcopy(self.payload["run_request"])
        changed_brief.pop("editorial_admission")
        changed_brief["editorial_brief"]["reader_task"] = "An unreviewed task"
        changes.append({"run_request": changed_brief})
        for change in changes:
            with self.subTest(change=change):
                self.assertFalse(portable_dispatch_snapshot_allowed(self.run, "remote-draft", {**self.payload, **change}))
