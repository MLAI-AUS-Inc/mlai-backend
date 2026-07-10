import os

from django.test import TestCase
from rest_framework import status
from rest_framework.test import APIClient

from content_factory.models import ContentFactoryHealingRecord, ContentFactoryLearningEntry
from organizations.models import Organization


class ContentFactoryLearningEntryTests(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.api_key = "test_roo_key"
        os.environ["ROO_API_KEY"] = self.api_key
        os.environ["INTERNAL_API_KEY"] = self.api_key

        from django.conf import settings

        settings.ROO_API_KEY = self.api_key
        settings.INTERNAL_API_KEY = self.api_key
        self.client.credentials(HTTP_X_API_KEY=self.api_key)

    def _pair_payload(self, **overrides):
        payload = {
            "error_category": "typescript_error",
            "error_excerpt": "Type error: cannot find name 'Article'",
            "fix_description": "Import the Article type from the registry module.",
            "framework": "react-router",
            "occurrences": 1,
            "confidence": "low",
            "first_seen": "2026-07-10T00:00:00",
            "last_seen": "2026-07-10T00:00:00",
        }
        payload.update(overrides)
        return payload

    def test_post_single_entry_creates_then_updates(self):
        entry = {
            "store": "error_fix_pairs",
            "scope": "repo",
            "repo_name": "MLAI-AUS-Inc/mlai-au",
            "framework": "react-router",
            "entry_key": "abc123def4567890",
            "payload": self._pair_payload(),
        }

        response = self.client.post("/api/content-factory/learning-entries/", entry, format="json")
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(response.data["created"], 1)
        self.assertEqual(response.data["results"][0]["sync_status"], "created")
        self.assertEqual(ContentFactoryLearningEntry.objects.count(), 1)

        updated = {
            **entry,
            "payload": self._pair_payload(occurrences=3, confidence="medium"),
        }
        update_response = self.client.post("/api/content-factory/learning-entries/", updated, format="json")
        self.assertEqual(update_response.status_code, status.HTTP_200_OK)
        self.assertEqual(update_response.data["updated"], 1)
        self.assertEqual(ContentFactoryLearningEntry.objects.count(), 1)

        record = ContentFactoryLearningEntry.objects.get()
        self.assertEqual(record.payload["confidence"], "medium")
        self.assertEqual(record.occurrences, 3)

    def test_post_batch_upserts_entries(self):
        entries = [
            {
                "store": "build_failures",
                "repo_name": "drsamdonegan/TalaThrive-Web",
                "framework": "react-router",
                "entry_key": f"sig-{index}",
                "payload": {"signature": f"sig-{index}", "occurrences": index + 1},
            }
            for index in range(3)
        ]
        response = self.client.post(
            "/api/content-factory/learning-entries/",
            {"entries": entries},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(response.data["created"], 3)
        self.assertEqual(ContentFactoryLearningEntry.objects.count(), 3)
        # Defaulted scope is repo.
        self.assertEqual(
            set(ContentFactoryLearningEntry.objects.values_list("scope", flat=True)),
            {"repo"},
        )

        # Re-posting one entry with a changed payload updates in place.
        entries[0]["payload"] = {"signature": "sig-0", "occurrences": 9}
        response = self.client.post(
            "/api/content-factory/learning-entries/",
            {"entries": [entries[0]]},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(ContentFactoryLearningEntry.objects.count(), 3)
        self.assertEqual(
            ContentFactoryLearningEntry.objects.get(entry_key="sig-0").occurrences,
            9,
        )

    def test_get_requires_store_and_filters(self):
        missing_store = self.client.get("/api/content-factory/learning-entries/")
        self.assertEqual(missing_store.status_code, status.HTTP_400_BAD_REQUEST)

        ContentFactoryLearningEntry.objects.create(
            store="error_fix_pairs",
            scope="repo",
            repo_name="MLAI-AUS-Inc/mlai-au",
            framework="react-router",
            entry_key="pair-1",
            payload=self._pair_payload(),
        )
        ContentFactoryLearningEntry.objects.create(
            store="error_fix_pairs",
            scope="framework",
            repo_name="",
            framework="react-router",
            entry_key="pair-1",
            payload=self._pair_payload(fix_description="Framework-scoped rule."),
        )
        ContentFactoryLearningEntry.objects.create(
            store="build_failures",
            scope="repo",
            repo_name="MLAI-AUS-Inc/mlai-au",
            framework="react-router",
            entry_key="sig-1",
            payload={"signature": "sig-1"},
        )

        repo_entries = self.client.get(
            "/api/content-factory/learning-entries/",
            {"store": "error_fix_pairs", "repo_name": "MLAI-AUS-Inc/mlai-au"},
        )
        self.assertEqual(repo_entries.status_code, status.HTTP_200_OK)
        self.assertEqual(len(repo_entries.data), 1)
        self.assertEqual(repo_entries.data[0]["scope"], "repo")

        framework_entries = self.client.get(
            "/api/content-factory/learning-entries/",
            {"store": "error_fix_pairs", "scope": "framework", "framework": "react-router"},
        )
        self.assertEqual(framework_entries.status_code, status.HTTP_200_OK)
        self.assertEqual(len(framework_entries.data), 1)
        self.assertEqual(framework_entries.data[0]["payload"]["fix_description"], "Framework-scoped rule.")

        keyed = self.client.get(
            "/api/content-factory/learning-entries/",
            {"store": "build_failures", "repo_name": "MLAI-AUS-Inc/mlai-au", "entry_key": "sig-1"},
        )
        self.assertEqual(len(keyed.data), 1)
        self.assertEqual(keyed.data[0]["entry_key"], "sig-1")

    def test_same_entry_key_across_scopes_does_not_collide(self):
        for scope, repo_name in (("repo", "acme/site"), ("framework", "")):
            response = self.client.post(
                "/api/content-factory/learning-entries/",
                {
                    "store": "error_fix_pairs",
                    "scope": scope,
                    "repo_name": repo_name,
                    "framework": "nextjs",
                    "entry_key": "shared-key",
                    "payload": self._pair_payload(),
                },
                format="json",
            )
            self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(ContentFactoryLearningEntry.objects.count(), 2)

    def test_post_rejects_bad_bodies(self):
        empty_batch = self.client.post(
            "/api/content-factory/learning-entries/",
            {"entries": []},
            format="json",
        )
        self.assertEqual(empty_batch.status_code, status.HTTP_400_BAD_REQUEST)

        too_large = self.client.post(
            "/api/content-factory/learning-entries/",
            {
                "entries": [
                    {"store": "s", "entry_key": f"k{i}", "payload": {}}
                    for i in range(201)
                ]
            },
            format="json",
        )
        self.assertEqual(too_large.status_code, status.HTTP_400_BAD_REQUEST)

        missing_fields = self.client.post(
            "/api/content-factory/learning-entries/",
            {"entries": [{"payload": {}}]},
            format="json",
        )
        self.assertEqual(missing_fields.status_code, status.HTTP_400_BAD_REQUEST)

    def test_requires_api_key(self):
        anonymous = APIClient()
        response = anonymous.get(
            "/api/content-factory/learning-entries/",
            {"store": "error_fix_pairs"},
        )
        self.assertIn(response.status_code, (status.HTTP_401_UNAUTHORIZED, status.HTTP_403_FORBIDDEN))


class ContentFactoryHealingRecordFrameworkTests(TestCase):
    """3.2 groundwork: healing records carry an optional framework key."""

    def setUp(self):
        self.client = APIClient()
        self.api_key = "test_roo_key"
        os.environ["ROO_API_KEY"] = self.api_key
        os.environ["INTERNAL_API_KEY"] = self.api_key

        from django.conf import settings

        settings.ROO_API_KEY = self.api_key
        settings.INTERNAL_API_KEY = self.api_key
        self.client.credentials(HTTP_X_API_KEY=self.api_key)
        self.organization = Organization.objects.create(name="MLAI", domain="mlai.au")

    def _payload(self, **overrides):
        payload = {
            "domain": "mlai.au",
            "github_repo": "MLAI-AUS-Inc/mlai-au",
            "failure_kind": "repo_code_build",
            "failure_family_key": "vite_transform_tsx_module",
            "summary": "Vite transform failures for TSX article modules.",
            "promotion_state": "candidate",
        }
        payload.update(overrides)
        return payload

    def test_legacy_payload_without_framework_still_upserts(self):
        created = self.client.post("/api/content-factory/healing-records/", self._payload(), format="json")
        self.assertEqual(created.status_code, status.HTTP_201_CREATED)
        updated = self.client.post(
            "/api/content-factory/healing-records/",
            self._payload(promotion_state="promoted"),
            format="json",
        )
        self.assertEqual(updated.status_code, status.HTTP_200_OK)
        self.assertEqual(ContentFactoryHealingRecord.objects.count(), 1)
        record = ContentFactoryHealingRecord.objects.get()
        self.assertEqual(record.framework, "")
        self.assertEqual(record.promotion_state, "promoted")

    def test_framework_participates_in_upsert_key_and_get_filter(self):
        without_framework = self.client.post(
            "/api/content-factory/healing-records/", self._payload(), format="json"
        )
        self.assertEqual(without_framework.status_code, status.HTTP_201_CREATED)

        with_framework = self.client.post(
            "/api/content-factory/healing-records/",
            self._payload(framework="react-router"),
            format="json",
        )
        self.assertEqual(with_framework.status_code, status.HTTP_201_CREATED)
        self.assertEqual(with_framework.data["framework"], "react-router")
        self.assertEqual(ContentFactoryHealingRecord.objects.count(), 2)

        filtered = self.client.get(
            "/api/content-factory/healing-records/",
            {"domain": "mlai.au", "framework": "react-router"},
        )
        self.assertEqual(filtered.status_code, status.HTTP_200_OK)
        self.assertEqual(len(filtered.data), 1)
        self.assertEqual(filtered.data[0]["framework"], "react-router")

    def test_framework_scoped_record_without_domain_is_allowed_shape(self):
        """Future cross-site promotion writes framework-scoped rows with an
        empty repo; the unique key must keep two frameworks apart."""
        for framework in ("react-router", "nextjs"):
            response = self.client.post(
                "/api/content-factory/healing-records/",
                self._payload(
                    domain="framework-scope.invalid",
                    github_repo="",
                    framework=framework,
                    promotion_state="promoted",
                ),
                format="json",
            )
            self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(
            ContentFactoryHealingRecord.objects.filter(domain="framework-scope.invalid").count(),
            2,
        )
