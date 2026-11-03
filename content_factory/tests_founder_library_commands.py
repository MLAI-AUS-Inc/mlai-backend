"""Authenticated SQL/library command custody with synthetic external effects."""
from contextlib import contextmanager
from copy import deepcopy
from threading import Barrier, Event, Thread
from unittest.mock import patch

from django.core.cache import cache
from django.db import connections
from django.test import TransactionTestCase, override_settings
from rest_framework.test import APIClient

from core.models import User
from roo.models import Ledger
from workflow_runs.models import ContentFactoryRun
from . import founder_library_commands as commands
from .founder_journey_testing import ALL_P1_FLAGS_ON
from .models import OrganizationContentConfig, TopicFeedback, WrittenArticle, ResearchedKeyword, KeywordStatus
from .run_ownership import bind_admitted_run, reserve_admitted_run, user_principal
from .self_heal_testing import make_p2_org
from .tests_founder_run_commands import authenticated_client
from .tests_run_current_review import response
from .tests_run_event_authority import FLAGS


@override_settings(**FLAGS, **ALL_P1_FLAGS_ON, CONTENT_FACTORY_URL="https://cf.fixture.invalid",
    CONTENT_FACTORY_API_KEY="synthetic-library-key")
class FounderLibraryCommandTests(TransactionTestCase):
    def setUp(self):
        built = make_p2_org(domain="library-command.example")
        self.org, self.user, self.company = built.organization, built.owner, built.company
        cache.clear()
        self.client = authenticated_client(self.user)
        sitemap = patch("content_factory.article_publish_status.http_requests.get", return_value=response({}, status=404))
        sitemap.start()
        self.addCleanup(sitemap.stop)
        self.topic = {"keyword": "A retained topic", "reasonCode": "not_appropriate", "reasonText": "Original note"}
        self.author = {"companyId": str(self.company.pk), "authors": [{"id": "original-author", "name": "Original Author", "avatarUrl": ""}],
            "defaultAuthorId": "original-author"}

    def send(self, suffix, body=None, *, key="library-command-key", method="post", client=None):
        return getattr(client or self.client, method)(
            f"/api/v1/my-startup/vibe-marketing/{suffix}/?company_id={self.company.pk}",
            body or {}, format="json", **({"HTTP_IDEMPOTENCY_KEY": key} if key is not None else {}))

    def receipts(self):
        return OrganizationContentConfig.objects.get(organization=self.org).pillar_strategy.get(commands.KEY, {})

    def test_topic_replay_keeps_one_original_row_and_changed_body_conflicts(self):
        first = self.send("topic-feedback", self.topic)
        saved = list(TopicFeedback.objects.values())
        replay = self.send("topic-feedback", self.topic)
        conflict = self.send("topic-feedback", {**self.topic, "reasonText": "Changed"})
        self.assertEqual((first.status_code, replay.status_code, conflict.status_code), (201, 201, 409), first.data)
        self.assertEqual(first.data, replay.data)
        self.assertEqual(list(TopicFeedback.objects.values()), saved)
        self.assertEqual(TopicFeedback.objects.count(), 1)

    def test_replay_of_original_ignore_does_not_undo_a_later_restore(self):
        first = self.send("topic-feedback", self.topic)
        restored = self.send(f"topic-feedback/{first.data['id']}/restore", key="restore-original")
        self.assertEqual(restored.status_code, 200, restored.data)
        saved = list(TopicFeedback.objects.values())
        replay = self.send("topic-feedback", self.topic)
        self.assertFalse(replay.data["active"])
        self.assertEqual(list(TopicFeedback.objects.values()), saved)

    def test_restore_replay_does_not_restore_a_later_feedback_record(self):
        first = self.send("topic-feedback", self.topic)
        url = f"topic-feedback/{first.data['id']}/restore"
        self.assertEqual(self.send(url, key="restore").status_code, 200)
        new = self.send("topic-feedback", self.topic, key="new-ignore")
        saved = list(TopicFeedback.objects.values())
        replay = self.send(url, key="restore")
        conflict = self.send(f"topic-feedback/{new.data['id']}/restore", key="restore")
        self.assertEqual((replay.status_code, conflict.status_code), (200, 409))
        self.assertEqual(list(TopicFeedback.objects.values()), saved)

    def test_header_cannot_change_action_and_invalid_requests_do_not_commit_receipts(self):
        invalid = self.send("topic-feedback", {})
        self.assertEqual(invalid.status_code, 400)
        self.assertEqual(self.receipts(), {})
        first = self.send("topic-feedback", self.topic)
        changed = self.send(f"topic-feedback/{first.data['id']}/restore")
        self.assertEqual(changed.status_code, 409)

    def test_foreign_and_revoked_principal_cannot_read_original_receipt(self):
        self.assertEqual(self.send("topic-feedback", self.topic).status_code, 201)
        foreign = authenticated_client(make_p2_org(domain="foreign-library.example").owner)
        denied = self.send("topic-feedback", self.topic, client=foreign)
        User.objects.filter(pk=self.user.pk).update(is_active=False)
        revoked = self.send("topic-feedback", self.topic)
        self.assertIn(denied.status_code, (400, 403, 404))
        self.assertNotIn("keyword", denied.data)
        self.assertEqual(revoked.status_code, 401)
        self.assertEqual(TopicFeedback.objects.count(), 1)

    def test_simultaneous_topic_commands_commit_one_row_and_one_receipt(self):
        barrier, results, errors = Barrier(2), [], []
        def worker():
            try:
                client = APIClient(); client.credentials(**self.client._credentials)
                barrier.wait(timeout=15)
                result = self.send("topic-feedback", self.topic, client=client)
                results.append((result.status_code, result.data))
            except Exception as exc:
                errors.append(repr(exc))
            finally:
                connections.close_all()
        threads = [Thread(target=worker) for _ in range(2)]
        for thread in threads: thread.start()
        for thread in threads: thread.join(timeout=30)
        self.assertFalse(any(t.is_alive() for t in threads))
        self.assertEqual(errors, [])
        self.assertEqual([x[0] for x in results], [201, 201], results)
        self.assertEqual(results[0][1], results[1][1])
        self.assertEqual(TopicFeedback.objects.count(), 1)
        self.assertEqual(len(self.receipts()), 1)

    def test_author_save_replay_reads_current_settings_without_resaving_prior_values(self):
        first = self.send("settings", self.author, method="put")
        self.assertEqual(first.status_code, 200, first.data)
        changed_body = {**self.author, "authors": [{**self.author["authors"][0], "name": "New Author"}]}
        conflict = self.send("settings", changed_body, method="put")
        self.assertEqual(conflict.status_code, 409)
        changed = self.send("settings", changed_body, key="new-author-intent", method="put")
        self.assertEqual(changed.status_code, 200, changed.data)
        saved = OrganizationContentConfig.objects.values().get(organization=self.org)
        replay = self.send("settings", self.author, method="put")
        self.assertEqual(replay.status_code, 200, replay.data)
        self.assertEqual(replay.data["settings"]["authors"][0]["name"], "New Author")
        self.assertEqual(OrganizationContentConfig.objects.values().get(organization=self.org), saved)
        for receipt in self.receipts().values():
            self.assertNotIn("response", receipt)
            self.assertNotIn("payload", receipt)

    def test_author_commit_survives_lost_bootstrap_response_without_resaving(self):
        self.client.raise_request_exception = False
        with patch("content_factory.vibe_marketing_views._serialize_bootstrap", side_effect=RuntimeError("Synthetic lost response")):
            first = self.send("settings", self.author, method="put")
        self.assertEqual(first.status_code, 500)
        saved = OrganizationContentConfig.objects.values().get(organization=self.org)
        replay = self.send("settings", self.author, method="put")
        self.assertEqual(replay.status_code, 200, replay.data)
        self.assertEqual(OrganizationContentConfig.objects.values().get(organization=self.org), saved)

    def test_unknown_photo_storage_is_held_without_another_copy_or_profile_write(self):
        from .author_photos import AuthorPhotoError
        body = {**self.author, "authors": [{**self.author["authors"][0], "avatarUrl": "https://photo.example/avatar.jpg"}]}
        before = OrganizationContentConfig.objects.get(organization=self.org).authors
        with patch("content_factory.author_photos.copy_photo", side_effect=AuthorPhotoError("storage_unknown", "Synthetic response loss", status=502)) as copy:
            first = self.send("settings", body, method="put")
            replay = self.send("settings", body, method="put")
        self.assertEqual((first.status_code, replay.status_code), (502, 503), first.data)
        self.assertEqual(copy.call_count, 1)
        self.assertEqual(OrganizationContentConfig.objects.get(organization=self.org).authors, before)

    def article(self):
        run = bind_admitted_run(reserve_admitted_run(organization=self.org,
            principal=user_principal(self.user.pk, f"mlai_user:{self.user.pk}"), workflow="article_generation",
            payload={"domain": self.org.domain, "client_request_id": "library-original", "topic": "Original draft",
                "delivery_mode": "content_only", "delivery_mode_confirmed": True}), "library-worker")
        article = WrittenArticle.objects.create(organization=self.org, title="Original draft", slug="original-draft",
            primary_keyword="original draft", source_run_id=run.run_id)
        ResearchedKeyword.objects.create(organization=self.org, keyword="original draft", keyword_normalized="original draft",
            status=KeywordStatus.WRITTEN, written_article=article)
        return article, run

    @contextmanager
    def cancellation(self, run, **options):
        with patch("content_factory.vibe_marketing_views.http_client.post",
                **(options or {"return_value": response({"run_id": run.run_id, "status": "cancelled"})})) as outbound:
            yield outbound

    def test_discard_deletion_and_receipt_are_atomic_and_replay_after_row_is_gone(self):
        article, run = self.article()
        before = ContentFactoryRun.objects.values().get(pk=run.pk)
        with self.cancellation(run) as outbound:
            first = self.send(f"written-articles/{article.pk}/discard")
            keyword = list(ResearchedKeyword.objects.values())
            replay = self.send(f"written-articles/{article.pk}/discard")
            conflict = self.send(f"written-articles/{article.pk}/discard", {"changed": True})
        self.assertEqual((first.status_code, replay.status_code, conflict.status_code), (200, 200, 409), first.data)
        self.assertEqual(first.data, replay.data)
        self.assertEqual(outbound.call_count, 1)
        self.assertFalse(WrittenArticle.objects.filter(pk=article.pk).exists())
        self.assertEqual(list(ResearchedKeyword.objects.values()), keyword)
        after = ContentFactoryRun.objects.values().get(pk=run.pk)
        for field in before.keys() - {"result"}:
            self.assertEqual(after[field], before[field], field)
        self.assertEqual(Ledger.objects.count(), 0)

    def test_unknown_discard_cancellation_preserves_article_and_never_reposts(self):
        import requests
        article, run = self.article()
        before = list(ResearchedKeyword.objects.values())
        with self.cancellation(run, side_effect=requests.Timeout("Synthetic cancellation response loss")) as outbound:
            first = self.send(f"written-articles/{article.pk}/discard")
            replay = self.send(f"written-articles/{article.pk}/discard")
        self.assertEqual((first.status_code, replay.status_code), (503, 503), first.data)
        self.assertEqual(outbound.call_count, 1)
        self.assertTrue(WrittenArticle.objects.filter(pk=article.pk).exists())
        self.assertEqual(list(ResearchedKeyword.objects.values()), before)

    def test_failed_receipt_commit_rolls_back_article_deletion_and_keyword_changes(self):
        article, run = self.article()
        before = list(ResearchedKeyword.objects.values())
        self.client.raise_request_exception = False
        with self.cancellation(run) as outbound:
            with patch.object(commands, "accept_local", side_effect=RuntimeError("Synthetic receipt transaction failure")):
                first = self.send(f"written-articles/{article.pk}/discard")
            replay = self.send(f"written-articles/{article.pk}/discard")
        self.assertEqual((first.status_code, replay.status_code), (500, 503), replay.data)
        self.assertEqual(outbound.call_count, 1)
        self.assertTrue(WrittenArticle.objects.filter(pk=article.pk).exists())
        self.assertEqual(list(ResearchedKeyword.objects.values()), before)

    def test_discard_checks_current_publication_status_again_at_commit(self):
        article, run = self.article()
        actual = commands.local_commit
        @contextmanager
        def published(action):
            WrittenArticle.objects.filter(pk=article.pk).update(publish_status="live")
            with actual(action): yield
        with self.cancellation(run) as outbound, patch.object(commands, "local_commit", side_effect=published):
            result = self.send(f"written-articles/{article.pk}/discard")
        self.assertEqual(result.status_code, 409, result.data)
        self.assertEqual(outbound.call_count, 1)
        self.assertTrue(WrittenArticle.objects.filter(pk=article.pk, publish_status="live").exists())

    def test_discard_known_terminal_refusal_does_not_invent_a_stop_or_retry_it(self):
        article, run = self.article()
        before = ContentFactoryRun.objects.values().get(pk=run.pk)
        with self.cancellation(run, return_value=response({"detail": "Run is terminal."}, status=409)) as outbound:
            first = self.send(f"written-articles/{article.pk}/discard")
            replay = self.send(f"written-articles/{article.pk}/discard")
        self.assertEqual((first.status_code, replay.status_code), (200, 200), first.data)
        self.assertEqual(outbound.call_count, 1)
        after = ContentFactoryRun.objects.values().get(pk=run.pk)
        for field in before.keys() - {"result"}:
            self.assertEqual(after[field], before[field], field)

    def test_unkeyed_topic_behavior_remains_legacy(self):
        first = self.send("topic-feedback", self.topic, key=None)
        updated = self.send("topic-feedback", {**self.topic, "reasonText": "Legacy update"}, key=None)
        self.assertEqual((first.status_code, updated.status_code), (201, 200))
        self.assertEqual(updated.data["reason_text"], "Legacy update")
        self.assertEqual(self.receipts(), {})

    def test_disabled_cohort_cannot_bypass_an_existing_header_binding(self):
        first = self.send("topic-feedback", self.topic)
        self.assertEqual(first.status_code, 201, first.data)
        saved = list(TopicFeedback.objects.values())
        with override_settings(MY_STARTUP_FOUNDER_JOURNEY_V2_ENABLED=False):
            replay = self.send("topic-feedback", self.topic)
            changed = self.send("topic-feedback", {**self.topic, "reasonText": "Unexpected overwrite"})
        self.assertEqual((replay.status_code, changed.status_code), (201, 409))
        self.assertEqual(list(TopicFeedback.objects.values()), saved)

    def test_changed_author_body_cannot_escape_into_general_settings(self):
        first = self.send("settings", self.author, method="put")
        self.assertEqual(first.status_code, 200, first.data)
        before = OrganizationContentConfig.objects.values().get(organization=self.org)
        changed = self.send("settings", {"dailyDiscoveryEnabled": True}, method="put")
        self.assertEqual(changed.status_code, 409, changed.data)
        self.assertEqual(OrganizationContentConfig.objects.values().get(organization=self.org), before)

    def test_general_keyed_settings_can_still_save_the_first_company_domain(self):
        from founder_tools.models import VibeRaisingCompany
        VibeRaisingCompany.objects.filter(pk=self.company.pk).update(domain="", organization=None)
        saved = self.send("settings", {"companyId": str(self.company.pk), "domain": "first-domain.example"}, method="put")
        self.assertEqual(saved.status_code, 200, saved.data)
        self.company.refresh_from_db()
        self.assertEqual(self.company.domain, "first-domain.example")
        config = OrganizationContentConfig.objects.get(organization_id=self.company.organization_id)
        self.assertNotIn(commands.KEY, config.pillar_strategy)

    def test_feedback_receipt_failure_rolls_back_the_local_effect(self):
        self.client.raise_request_exception = False
        with patch.object(commands, "_save", side_effect=RuntimeError("Synthetic local receipt failure")):
            failed = self.send("topic-feedback", self.topic)
        self.assertEqual(failed.status_code, 500)
        self.assertEqual(TopicFeedback.objects.count(), 0)
        self.assertEqual(self.receipts(), {})
        self.assertEqual(self.send("topic-feedback", self.topic).status_code, 201)

    def _concurrent_external(self, suffix, body, method, target, result):
        entered, release, outcomes, errors = Event(), Event(), [], []
        def external(*args, **kwargs):
            entered.set()
            if not release.wait(timeout=20): raise RuntimeError("Concurrent test did not release external effect")
            return result
        def worker():
            try:
                client = APIClient(); client.credentials(**self.client._credentials)
                value = self.send(suffix, body, method=method, client=client)
                outcomes.append((value.status_code, value.data))
            except Exception as exc:
                errors.append(repr(exc))
            finally:
                connections.close_all()
        with patch(target, side_effect=external) as effect:
            thread = Thread(target=worker); thread.start()
            try:
                self.assertTrue(entered.wait(timeout=15))
                pending = self.send(suffix, body, method=method)
                self.assertEqual(pending.status_code, 503, pending.data)
            finally:
                release.set(); thread.join(timeout=30)
            self.assertFalse(thread.is_alive())
            replay = self.send(suffix, body, method=method)
        self.assertEqual(errors, [])
        self.assertEqual([v[0] for v in outcomes], [200], outcomes)
        self.assertEqual(replay.status_code, 200, replay.data)
        self.assertEqual(effect.call_count, 1)

    def test_simultaneous_author_save_copies_one_photo_and_replays_after_commit(self):
        body = {**self.author, "authors": [{**self.author["authors"][0], "avatarUrl": "https://photo.example/avatar.jpg"}]}
        self._concurrent_external("settings", body, "put", "content_factory.author_photos.copy_photo", "/stored/avatar.jpg")

    def test_simultaneous_discard_submits_one_cancel_and_deletes_once(self):
        article, run = self.article()
        self._concurrent_external(f"written-articles/{article.pk}/discard", {}, "post",
            "content_factory.vibe_marketing_views.http_client.post", response({"run_id": run.run_id, "status": "cancelled"}))
        self.assertFalse(WrittenArticle.objects.filter(pk=article.pk).exists())

    def test_discard_lost_response_after_atomic_delete_replays_original_acknowledgement(self):
        from rest_framework.response import Response
        article, run = self.article()
        self.client.raise_request_exception = False
        def lost_after_commit(data, *args, **kwargs):
            if isinstance(data, dict) and data.get("discarded"):
                raise RuntimeError("Synthetic response loss after atomic discard")
            return Response(data, *args, **kwargs)
        with self.cancellation(run) as outbound:
            with patch("content_factory.vibe_marketing_views.Response", side_effect=lost_after_commit):
                lost = self.send(f"written-articles/{article.pk}/discard")
            replay = self.send(f"written-articles/{article.pk}/discard")
        self.assertEqual((lost.status_code, replay.status_code), (500, 200), replay.data)
        self.assertTrue(replay.data["discarded"])
        self.assertEqual(outbound.call_count, 1)

    def test_discard_permission_revoked_before_delete_preserves_article(self):
        article, run = self.article()
        original = commands.local_commit
        @contextmanager
        def revoked(action):
            User.objects.filter(pk=self.user.pk).update(is_active=False)
            with original(action): yield
        with self.cancellation(run) as outbound, patch.object(commands, "local_commit", side_effect=revoked):
            result = self.send(f"written-articles/{article.pk}/discard")
        self.assertEqual(result.status_code, 403, result.data)
        self.assertTrue(WrittenArticle.objects.filter(pk=article.pk).exists())
        self.assertEqual(outbound.call_count, 1)

    def test_discard_ticket_change_before_transport_cannot_cancel_the_new_execution(self):
        from .tests_run_event_authority import advance_ticket
        article, run = self.article()
        before = commands.before_transport
        def changed(**kwargs):
            advance_ticket(run)
            return before(**kwargs)
        with self.cancellation(run) as outbound, patch.object(commands, "before_transport", side_effect=changed):
            result = self.send(f"written-articles/{article.pk}/discard")
        self.assertEqual(result.status_code, 409, result.data)
        self.assertTrue(WrittenArticle.objects.filter(pk=article.pk).exists())
        outbound.assert_not_called()

    def test_refused_discard_cannot_retarget_an_edited_article_on_retry(self):
        article, run = self.article()
        WrittenArticle.objects.filter(pk=article.pk).update(publish_status="pr_open")
        with self.cancellation(run) as outbound:
            refused = self.send(f"written-articles/{article.pk}/discard")
            self.assertEqual(refused.status_code, 409, refused.data)
            WrittenArticle.objects.filter(pk=article.pk).update(publish_status="written", title="A different edited article")
            replay = self.send(f"written-articles/{article.pk}/discard")
        self.assertEqual(replay.status_code, 409, replay.data)
        self.assertTrue(WrittenArticle.objects.filter(pk=article.pk).exists())
        outbound.assert_not_called()

    def test_discard_new_execution_after_cancel_ack_prevents_local_deletion(self):
        from .tests_run_event_authority import advance_ticket
        article, run = self.article()
        original = commands.local_commit
        @contextmanager
        def changed(action):
            advance_ticket(run)
            with original(action): yield
        with self.cancellation(run) as outbound, patch.object(commands, "local_commit", side_effect=changed):
            result = self.send(f"written-articles/{article.pk}/discard")
        self.assertEqual(result.status_code, 409, result.data)
        self.assertTrue(WrittenArticle.objects.filter(pk=article.pk).exists())
        self.assertEqual(outbound.call_count, 1)

    def test_enrolled_flag_off_refuses_unseen_topic_but_keeps_original_replay(self):
        first = self.send("topic-feedback", self.topic)
        self.assertEqual(first.status_code, 201, first.data)
        config = OrganizationContentConfig.objects.values().get(organization=self.org)
        rows = list(TopicFeedback.objects.values())
        with override_settings(MY_STARTUP_FOUNDER_JOURNEY_V2_ENABLED=False):
            unseen = self.send("topic-feedback", {**self.topic, "keyword": "New topic"}, key="new-off-topic")
            replay = self.send("topic-feedback", self.topic)
        self.assertEqual((unseen.status_code, replay.status_code), (409, 201), unseen.data)
        self.assertEqual(list(TopicFeedback.objects.values()), rows)
        self.assertEqual(OrganizationContentConfig.objects.values().get(organization=self.org), config)
        self.assertEqual(Ledger.objects.count(), 0)

    def test_enrolled_flag_off_refuses_unseen_restore_and_replays_original_restore(self):
        first = self.send("topic-feedback", self.topic)
        url = f"topic-feedback/{first.data['id']}/restore"
        before = list(TopicFeedback.objects.values())
        with override_settings(MY_STARTUP_FOUNDER_JOURNEY_V2_ENABLED=False):
            unseen = self.send(url, key="original-restore")
        self.assertEqual(unseen.status_code, 409, unseen.data)
        self.assertEqual(list(TopicFeedback.objects.values()), before)
        self.assertEqual(self.send(url, key="original-restore").status_code, 200)
        saved = list(TopicFeedback.objects.values())
        receipts = self.receipts()
        with override_settings(MY_STARTUP_FOUNDER_JOURNEY_V2_ENABLED=False):
            replay = self.send(url, key="original-restore")
        self.assertEqual(replay.status_code, 200, replay.data)
        self.assertEqual(list(TopicFeedback.objects.values()), saved)
        self.assertEqual(self.receipts(), receipts)
        self.assertEqual(Ledger.objects.count(), 0)

    def test_enrolled_flag_off_refuses_unseen_author_without_photo_or_profile_effect(self):
        self.assertEqual(self.send("settings", self.author, method="put").status_code, 200)
        body = {**self.author, "authors": [{**self.author["authors"][0], "avatarUrl": "https://photo.example/new.jpg"}]}
        before = OrganizationContentConfig.objects.values().get(organization=self.org)
        with patch("content_factory.author_photos.copy_photo", return_value="/stored/new.jpg") as copy:
            with override_settings(MY_STARTUP_FOUNDER_JOURNEY_V2_ENABLED=False):
                unseen = self.send("settings", body, key="new-off-author", method="put")
                replay = self.send("settings", self.author, method="put")
            self.assertEqual((unseen.status_code, replay.status_code), (409, 200), unseen.data)
            copy.assert_not_called()
            self.assertEqual(OrganizationContentConfig.objects.values().get(organization=self.org), before)
            self.assertEqual(self.send("settings", body, key="new-off-author", method="put").status_code, 200)
            self.assertEqual(copy.call_count, 1)
        self.assertEqual(Ledger.objects.count(), 0)

    def test_enrolled_flag_off_refuses_unseen_discard_and_retains_later_acceptance(self):
        self.assertEqual(self.send("topic-feedback", self.topic).status_code, 201)
        article, run = self.article()
        url = f"written-articles/{article.pk}/discard"
        before = {"article": list(WrittenArticle.objects.values()), "keywords": list(ResearchedKeyword.objects.values()),
            "run": ContentFactoryRun.objects.values().get(pk=run.pk), "receipts": self.receipts()}
        with self.cancellation(run) as outbound:
            with override_settings(MY_STARTUP_FOUNDER_JOURNEY_V2_ENABLED=False):
                unseen = self.send(url, key="new-off-discard")
            self.assertEqual(unseen.status_code, 409, unseen.data)
            outbound.assert_not_called()
            self.assertEqual(list(WrittenArticle.objects.values()), before["article"])
            self.assertEqual(list(ResearchedKeyword.objects.values()), before["keywords"])
            self.assertEqual(ContentFactoryRun.objects.values().get(pk=run.pk), before["run"])
            self.assertEqual(self.receipts(), before["receipts"])
            accepted = self.send(url, key="new-off-discard")
            self.assertEqual(accepted.status_code, 200, accepted.data)
            saved = self.receipts()
            with override_settings(MY_STARTUP_FOUNDER_JOURNEY_V2_ENABLED=False):
                replay = self.send(url, key="new-off-discard")
            self.assertEqual(replay.status_code, 200, replay.data)
            self.assertEqual(replay.data, accepted.data)
            self.assertEqual(outbound.call_count, 1)
            self.assertEqual(self.receipts(), saved)
        self.assertEqual(Ledger.objects.count(), 0)

    def test_unenrolled_flag_off_keyed_topic_keeps_legacy_compatibility(self):
        with override_settings(MY_STARTUP_FOUNDER_JOURNEY_V2_ENABLED=False):
            response = self.send("topic-feedback", self.topic)
        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual(self.receipts(), {})

    def test_enrolled_flag_off_keeps_unrelated_general_settings_compatible(self):
        self.assertEqual(self.send("settings", self.author, method="put").status_code, 200)
        receipts = self.receipts()
        with override_settings(MY_STARTUP_FOUNDER_JOURNEY_V2_ENABLED=False):
            response = self.send("settings", {"companyId": str(self.company.pk), "brandName": "Updated brand"},
                key="unrelated-general-settings", method="put")
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(self.receipts(), receipts)
