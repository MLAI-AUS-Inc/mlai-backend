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
