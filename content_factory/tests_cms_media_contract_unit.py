"""An uncertain image upload never receives a second create allowance."""

from copy import deepcopy
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from unittest.mock import patch
from uuid import uuid4

from django.test import SimpleTestCase

from . import cms_media_contract as journal
from .cms_api_transport import CmsApiError
from .cms_media_evidence import observe_owned_image
from .cms_media_transport import UploadedImage, prepare_image
from .tests_cms_inventory_contract_unit import binding
from .tests_cms_media_evidence_unit import raster


def intent(platform="wordpress"):
    image = prepare_image(raster())
    asset = {"component_id": "hero-image", "src": "https://firebasestorage.googleapis.com/v0/b/fixture/o/image.png?alt=media",
        "alt": "A blue synthetic image", "sha256": image.sha256, "width": 32, "height": 24}
    return journal.make_intent(binding=binding(platform), review_operation_id=str(uuid4()), handoff_digest="a" * 64,
        source_run_id="original-run", source_generation=0, asset=asset, upload_id=str(uuid4()), title="Native article"), image


def row(value, image):
    return {"id": 42, "type": "attachment", "media_type": "image", "mime_type": image.media_type,
        "slug": journal.slug_for(value), "description": {"raw": "mlai-image:" + value["upload_id"] + ":" + image.sha256},
        "source_url": value["binding"]["public_origin"] + "wp-content/uploads/owned.png"}


class CmsMediaContractTests(SimpleTestCase):
    def test_preflight_and_only_upload_allowance_are_bound_to_original_source(self):
        value, image = intent()
        state = journal.new_state(value)
        self.assertEqual(journal.lookup_options(value, state), {"method": "GET", "resource": "media",
            "query": {"slug": journal.slug_for(value), "context": "edit", "per_page": 2}})
        ready = journal.observe_lookup(value, state, [], image=image)
        issued = journal.issue_upload(value, ready)
        self.assertEqual((issued["phase"], issued["create_count"]), ("upload_issued", 1))
        for current in (state, issued):
            with self.assertRaises(CmsApiError): journal.issue_upload(value, current)
        self.assertEqual(journal.validate_state(issued, value), issued)

    def test_lost_reply_uses_eight_bounded_gets_and_never_another_upload(self):
        value, image = intent()
        state = journal.issue_upload(value, journal.observe_lookup(value, journal.new_state(value), [], image=image))
        for count in range(1, 9):
            state = journal.observe_lookup(value, state, [], image=image)
            self.assertEqual((state["create_count"], state["probe_count"]), (1, count))
            with self.assertRaises(CmsApiError): journal.issue_upload(value, state)
        self.assertEqual(state["phase"], "reconcile_exhausted")
        with self.assertRaises(CmsApiError): journal.lookup_options(value, state)

    def test_marked_attachment_recovery_accepts_only_exact_original_uuid_and_raster(self):
        value, image = intent()
        state = journal.issue_upload(value, journal.observe_lookup(value, journal.new_state(value), [], image=image))
        found = journal.observe_lookup(value, state, [row(value, image)], image=image)
        self.assertEqual((found["phase"], found["create_count"], found["acknowledgement"]["provider_id"]), ("provider_ack", 1, 42))
        self.assertIsNone(found["evidence"])
        for changes in ({"slug": "foreign"}, {"description": {"raw": "foreign"}}, {"mime_type": "image/jpeg"}, {"id": True},
                {"source_url": "https://foreign.test/owned.png"}):
            with self.subTest(changes=changes), self.assertRaises(CmsApiError):
                journal.observe_lookup(value, state, [{**row(value, image), **changes}], image=image)
        with self.assertRaises(CmsApiError): journal.observe_lookup(value, state, [row(value, image)] * 2, image=image)

    def test_ghost_lost_reply_records_unsupported_recovery_without_a_fabricated_lookup(self):
        value, _image = intent("ghost")
        state = journal.issue_upload(value, journal.ghost_ready(value, journal.new_state(value)))
        unknown = journal.ghost_unknown(value, state)
        self.assertEqual((unknown["phase"], unknown["create_count"], unknown["probe_count"]), ("recovery_unsupported", 1, 0))
        for operation in (journal.issue_upload, journal.lookup_options):
            with self.assertRaises(CmsApiError): operation(value, unknown)

    def test_public_bytes_evidence_completes_a_private_upload_but_is_not_approval(self):
        value, image = intent()
        state = journal.issue_upload(value, journal.observe_lookup(value, journal.new_state(value), [], image=image))
        ack = UploadedImage("wordpress", 42, value["upload_id"], image.sha256, row(value, image)["source_url"])
        state = journal.record_ack(value, state, ack)
        old = datetime.now(timezone.utc) - timedelta(hours=1)
        with patch("content_factory.cms_media_evidence._read_image", return_value=image):
            evidence = observe_owned_image(value["binding"], handoff_digest=value["handoff_digest"],
                asset=value["asset"], acknowledgement=ack, checked_at=old)
        verified = journal.record_public(value, state, evidence)
        # Historical bytes stay in the journal. Final review explicitly asks
        # for current observations, and must reverify old public bytes.
        self.assertEqual(journal.validate_state(verified, value), verified)
        with self.assertRaises(CmsApiError): journal.validate_state(verified, value, now=datetime.now(timezone.utc))
        self.assertNotIn("approved", verified)
        self.assertEqual(verified["acknowledgement"], asdict(ack))

    def test_rechecksummed_intents_and_states_reject_rollback_and_boolean_counter_types(self):
        value, image = intent()
        for changes in ({"schema_version": True}, {"source_generation": True}, {"approved": True}, {"upload_id": "other"}):
            changed = {**deepcopy(value), **changes}
            changed["digest"] = journal.digest({key: item for key, item in changed.items() if key != "digest"})
            with self.assertRaises(CmsApiError): journal.validate_intent(changed)
        state = journal.issue_upload(value, journal.observe_lookup(value, journal.new_state(value), [], image=image))
        for changes in ({"create_count": True}, {"create_count": 0}, {"probe_count": True}, {"intent_digest": "f" * 64},
                {"acknowledgement": {"approved": True}}, {"phase": "reconcile_exhausted"}):
            with self.subTest(changes=changes), self.assertRaises(CmsApiError): journal.validate_state({**state, **changes}, value)
