"""Actual disposable CMS APIs; selected only by the isolated remote CI runner.

This complements the original-goal ORM fixture. Its direct post contracts do
not grant a real paid-source authorization or certify public DNS/TLS/hosting.
"""

from contextlib import ExitStack
from io import BytesIO
import json
import os
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from uuid import uuid4

from django.contrib.auth import get_user_model
from django.db import connection
from django.test import TransactionTestCase, override_settings
from PIL import Image

from scripts.cms_provider_fixture import HOSTS, FixtureConnection, addresses
from . import cms_api_transport as api, cms_capture, cms_live_evidence as live
from . import cms_media_transport as media, cms_owner_connection as owner
from . import cms_post_contract as posts, cms_unpublish_contract as unpublish
from . import cms_unpublish_public as absence
from . import website_channel_observation as homepage
from .cms_connection_authority import current_credential
from .models import OrganizationContentConfig
from .website_connections import contract_for
from .website_contract import WebsiteAuthorityError
from .website_disconnect import start_disconnect
from .website_publishing_channels import scope_for, update_choice


@unittest.skipUnless(os.environ.get("MLAI_CMS_FIXTURE"), "Dedicated remote provider fixture required")
@override_settings(ARTICLES_CHANNEL_DISCOVERY_ENABLED=True, ARTICLES_PUBLISHING_CHANNELS_ENABLED=True,
    ARTICLES_CMS_CHANNEL_ENABLED=True, ARTICLES_CMS_ONBOARDING_ENABLED=True)
class CmsProviderAcceptanceTests(TransactionTestCase):
    """Real connect/capture persistence, provider contracts, public rendering and revoke."""

    def test_native_provider_round_trip(self):
        from organizations.models import Organization
        from founder_tools.models import VibeRaisingCompany, VibeRaisingProfile
        fixture = json.loads(os.environ["MLAI_CMS_FIXTURE"])
        platform = fixture["provider"]
        host = HOSTS[platform]
        origin = "https://" + host + "/"
        user = get_user_model().objects.create_user(email="cms-fixture@example.test")
        org = Organization.objects.create(domain=host, name="Disposable native CMS")
        profile = VibeRaisingProfile.objects.create(user=user, role="founder")
        company = VibeRaisingCompany.objects.create(profile=profile, organization=org, name="CMS fixture", domain=host)
        context = SimpleNamespace(profile=profile, company=company, organization=org)
        config = OrganizationContentConfig.objects.create(organization=org, github_repo="")
        with ExitStack() as stack:
            for module in (api, live, absence, homepage):
                stack.enter_context(patch.object(module, "public_addresses", new=addresses))
                stack.enter_context(patch.object(module, "PinnedHTTPSConnection", new=FixtureConnection))
            # Read the actual fixture homepage. The synthetic owner explicitly
            # selects its CMS, as the real initial form permits when hints are
            # unavailable. Neither a hint nor this choice authenticates the key.
            inspected = update_choice(context, config, expected_scope=scope_for(context, config), expected_revision=0)
            selected = update_choice(context, config, expected_scope=inspected["scope"], expected_revision=1,
                selected="cms", platform=platform)
            data = {"client_request_id": str(uuid4()), "channel_scope": selected["scope"], "channel_revision": 2,
                "platform": platform, "public_origin": origin, "api_origin": origin, "credential_origin_confirmed": True,
                "username": fixture["username"], "secret": fixture["secret"]}
            self.assertFalse(connection.in_atomic_block)
            website = owner.connect(context, config, user=user, data=data)
            self.assertEqual(str(owner.connect(context, config, user=user, data=data).pk), str(website.pk))
            binding, credential = current_credential(context, config, website, user=user)
            private = website.operations.get(action="channel-credential")
            self.assertTrue(fixture["secret"] not in json.dumps(private.payload))
            self.assertTrue(private.payload["sealed_credential"].startswith("mlai-enc:v1:"))

            def request(**effect):
                self.assertFalse(connection.in_atomic_block)
                return api.request_json(binding, credential, **effect)

            if platform == "ghost":
                # More than one production-sized page, without model calls or
                # seeded response JSON. These are real provider-created posts.
                for index in range(101):
                    request(method="POST", resource="posts", query={"source": "html", "formats": "html"},
                        payload={"posts": [{"title": "Existing fixture " + str(index), "html": "<p>Existing native content.</p>",
                            "status": "published", "visibility": "public"}]})
            capture = cms_capture.start_capture(context, config, user=user,
                data={**contract_for(website), "client_request_id": str(uuid4())})
            for _ in range(4):
                capture = cms_capture.advance_capture(capture.pk)
                if capture.state != "pending":
                    break
            self.assertEqual(capture.state, "completed")
            inventory = capture.payload["inventory"]
            self.assertEqual(inventory["coverage"], "complete")
            self.assertGreaterEqual(inventory["observed_pages"], 2)
            self.assertGreaterEqual(len(inventory["records"]), 101)

            first = inventory["records"][0]
            # Read a real original row before and after the entire operation.
            original_id = first["provider_id"]
            query = {"context": "edit"} if platform == "wordpress" else {"formats": "html"}
            original, _ = request(method="GET", resource="posts", identifier=original_id, query=query)
            raster = BytesIO()
            Image.new("RGB", (16, 16), (26, 72, 110)).save(raster, format="PNG")
            image = media.prepare_image(raster.getvalue())
            uploaded = media.upload_image(binding, credential, upload_id=str(uuid4()), image=image,
                title="Disposable fixture image", alt_text="A blue square")
            if platform == "wordpress":
                raw = original
                native = {"author": raw["author"], "categories": raw["categories"], "tags": raw["tags"], "featured_media": uploaded.provider_id}
            else:
                raw = original["posts"][0]
                native = {"authors": [item["id"] for item in raw["authors"]], "tags": [item["id"] for item in raw["tags"]], "feature_image": uploaded.url}
            intent = posts.make_intent(binding=binding, operation_id=str(uuid4()), source_run_id="recorded-provider-fixture",
                run_generation=1, artifact_sha256="a" * 64, editorial_sha256="b" * 64, approval_sha256="c" * 64,
                title="Disposable native article", slug_seed="disposable-native-article", status="published", native_fields=native,
                html='<article data-cf-article-body><h1>Disposable native article</h1><p>Recorded provider acceptance body.</p></article>')

            def effect(plan):
                return request(method=plan.method, resource=plan.resource, identifier=plan.identifier,
                    query=plan.query, payload=plan.payload)[0]

            state = posts.new_post_state(intent)
            state = posts.observe_posts(intent, state, effect(posts.lookup_effect(intent, state)))
            state, create = posts.issue_create(intent, state)
            effect(create)  # Deliberately discard the successful POST reply.
            state = posts.observe_posts(intent, state, effect(posts.lookup_effect(intent, state)))
            self.assertEqual((state["phase"], state["create_count"]), ("provider_confirmed", 1))
            posts.observe_confirmation(intent, state, effect(posts.confirmation_effect(intent, state)))
            proof = live.observe_public_article(intent, state, live.fetch_public_article(intent, state))
            live.validate_public_observation(proof, intent=intent, state=state)

            withdrawal = unpublish.make_intent(operation_id=str(uuid4()), request_id=str(uuid4()), publication=intent, state=state)
            options = {"publication": intent, "published_state": state}
            progress = unpublish.new_state(withdrawal, **options)
            progress = unpublish.observe(withdrawal, progress, effect(unpublish.read_effect(withdrawal, progress, **options)), **options)
            progress, update = unpublish.issue(withdrawal, progress, **options)
            self.assertEqual(update.identifier, state["provider_id"])
            effect(update)  # Discard again; the next observation is GET only.
            progress = unpublish.observe(withdrawal, progress, effect(unpublish.read_effect(withdrawal, progress, **options)), **options)
            self.assertEqual((progress["phase"], progress["issue_count"]), ("provider_draft", 1))
            observed = absence.fetch_absence(withdrawal, progress, **options)
            withdrawn = absence.observation(withdrawal, progress, observed, **options)
            absence.validate_observation(withdrawn, withdrawal, progress, **options)
            after, _ = request(method="GET", resource="posts", identifier=original_id, query=query)
            self.assertEqual(after, original)
            start_disconnect(config, user=user, data={**contract_for(website), "remove_setup": False})
            config.refresh_from_db(); website.refresh_from_db(); private.refresh_from_db()
            self.assertEqual(website.state, "disconnected")
            self.assertEqual(private.payload, {})
            with self.assertRaises(WebsiteAuthorityError): current_credential(context, config, website, user=user)
            # Disconnection revokes local authority; it cannot revoke an owner's
            # provider key. Container teardown destroys this synthetic key.
            print("CMS provider acceptance: encrypted connect, two-page capture, media, one-create recovery, rendered body, one-update withdrawal, public absence, disconnect.")
