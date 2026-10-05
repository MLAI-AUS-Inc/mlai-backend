"""Approved migration tests using synthetic credentials in disposable databases."""

import importlib
import threading
from unittest.mock import patch

from django.apps import apps
from django.db import close_old_connections, connection, connections, transaction
from django.test import TransactionTestCase, override_settings, skipUnlessDBFeature, tag

from content_factory.models import OrganizationContentConfig
from integrations.fields import (
    CredentialEncryptionError, decrypt_credential_value,
    encrypt_credential_value, encrypt_value,
)
from organizations.models import Organization

backfill = importlib.import_module(
    "content_factory.migrations.0040_backfill_github_credential_envelopes"
)


@override_settings(
    IS_LOCAL_ENV=True,
    CONNECTOR_CREDENTIAL_KEYS='{"test":"MDEyMzQ1Njc4OWFiY2RlZjAxMjM0NTY3ODlhYmNkZWY="}',
    CONNECTOR_CREDENTIAL_ACTIVE_KEY_ID="test",
    FIELD_ENCRYPTION_KEY="", SECRET_KEY="synthetic-github-migration-secret",
)
class GitHubCredentialMigrationTests(TransactionTestCase):
    def seed(self, access, refresh):
        org = Organization.objects.create(domain=f"credentials-{Organization.objects.count()}.example.test")
        config = OrganizationContentConfig.objects.create(organization=org)
        # Simulate historical raw storage, bypassing the new field on purpose.
        with connection.cursor() as cursor:
            cursor.execute(
                "UPDATE content_factory_org_config SET github_token_encrypted=%s, "
                "github_refresh_token_encrypted=%s WHERE id=%s", [access, refresh, config.pk],
            )
        return config

    def raw(self, config):
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT github_token_encrypted, github_refresh_token_encrypted "
                "FROM content_factory_org_config WHERE id=%s", [config.pk],
            )
            return cursor.fetchone()

    def migrate(self):
        with connection.schema_editor(atomic=False) as editor:
            backfill.backfill_github_credentials(apps, editor)

    def test_plaintext_is_converted_and_orm_reads_remain_plaintext(self):
        config = self.seed("synthetic-access", "synthetic-refresh")
        config.refresh_from_db()
        self.assertEqual(config.github_token_encrypted, "synthetic-access")
        timestamp = config.updated_at
        self.migrate()
        values = self.raw(config)
        self.assertTrue(all(value.startswith("mlai-enc:v1:test:") for value in values))
        self.assertEqual(tuple(map(decrypt_credential_value, values)), ("synthetic-access", "synthetic-refresh"))
        config.refresh_from_db()
        self.assertEqual(config.github_refresh_token_encrypted, "synthetic-refresh")
        self.assertEqual(config.updated_at, timestamp)

    def test_valid_ciphertext_empty_and_null_are_preserved_on_repeated_runs(self):
        pairs = [
            (encrypt_credential_value("synthetic-existing"), encrypt_value("synthetic-legacy")),
            (None, ""), ("", None),
        ]
        configs = [self.seed(*pair) for pair in pairs]
        self.migrate()
        self.migrate()
        self.assertEqual([self.raw(config) for config in configs], pairs)

    def test_new_model_writes_and_queryset_updates_are_encrypted(self):
        config = self.seed(None, None)
        config.github_token_encrypted = "synthetic-new"
        config.save(update_fields=["github_token_encrypted"])
        OrganizationContentConfig.objects.filter(pk=config.pk).update(
            github_refresh_token_encrypted="synthetic-refresh-new",
        )
        self.assertEqual(tuple(map(decrypt_credential_value, self.raw(config))),
                         ("synthetic-new", "synthetic-refresh-new"))
        self.assertTrue(all(value.startswith("mlai-enc:v1:") for value in self.raw(config)))

    def test_invalid_ciphertext_fails_without_exposing_it_or_partially_updating_pair(self):
        for invalid in ("mlai-enc:v1:private-fixture-id:broken", "mlai-enc:v2:broken", "gAAAA-broken"):
            config = self.seed("synthetic-access", invalid)
            with self.assertRaises(CredentialEncryptionError) as caught:
                self.migrate()
            self.assertNotIn(invalid, str(caught.exception))
            self.assertNotIn("private-fixture-id", str(caught.exception))
            self.assertEqual(self.raw(config), ("synthetic-access", invalid))
            config.delete()

    def test_restart_preserves_committed_batches_and_finishes_after_repair(self):
        first = self.seed("synthetic-first", None)
        second = self.seed("synthetic-second", "mlai-enc:v1:unknown:broken")
        with patch.object(backfill, "BATCH_SIZE", 1):
            with self.assertRaises(CredentialEncryptionError):
                self.migrate()
            committed = self.raw(first)
            self.assertTrue(committed[0].startswith("mlai-enc:v1:"))
            self.assertEqual(self.raw(second)[0], "synthetic-second")
            # Repair only the synthetic bad fixture, as an explicit owner would.
            OrganizationContentConfig.objects.filter(pk=second.pk).update(github_refresh_token_encrypted=None)
            self.migrate()
        self.assertEqual(self.raw(first), committed)
        self.assertEqual(decrypt_credential_value(self.raw(second)[0]), "synthetic-second")

    def test_final_verification_rejects_plaintext_and_backfill_has_no_reverse(self):
        self.seed("synthetic-leftover", None)
        with self.assertRaises(CredentialEncryptionError):
            backfill.verify_encrypted(apps, connection)
        self.assertFalse(backfill.Migration.operations[0].reversible)

    @tag("postgres-only")
    @skipUnlessDBFeature("has_select_for_update")
    def test_refresh_waiting_on_backfill_lock_is_not_lost(self):
        config = self.seed("synthetic-old", None)
        writer_started = threading.Event()
        writer_finished = threading.Event()
        errors = []
        worker = None
        original_convert = backfill.converted_value

        def refresh():
            close_old_connections()
            try:
                with transaction.atomic():
                    with connections["default"].cursor() as cursor:
                        cursor.execute("SET LOCAL lock_timeout = '5s'")
                    writer_started.set()
                    OrganizationContentConfig.objects.filter(pk=config.pk).update(
                        github_token_encrypted="synthetic-refreshed",
                    )
            except Exception as exc:
                errors.append(exc)
            finally:
                connections.close_all()
                writer_finished.set()

        def convert_while_locked(value):
            nonlocal worker
            if value == "synthetic-old" and worker is None:
                worker = threading.Thread(target=refresh)
                worker.start()
                self.assertTrue(writer_started.wait(3))
                self.assertFalse(writer_finished.wait(0.15), "Concurrent refresh bypassed the row lock")
            return original_convert(value)

        try:
            with patch.object(backfill, "converted_value", side_effect=convert_while_locked):
                self.migrate()
        finally:
            if worker:
                worker.join(timeout=8)
        self.assertTrue(writer_finished.is_set())
        self.assertEqual(errors, [])
        config.refresh_from_db()
        self.assertEqual(config.github_token_encrypted, "synthetic-refreshed")
