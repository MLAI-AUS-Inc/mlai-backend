"""Tests for field-level encryption and SECRET_KEY resolution (Crit-1 fix).

These are pure-function tests (no DB), so they run as SimpleTestCase.
"""
import base64
import hashlib
import os
from unittest import mock

from cryptography.fernet import Fernet
from django.core.exceptions import ImproperlyConfigured
from django.test import SimpleTestCase, override_settings

from integrations import fields as fields_mod
from mlai import settings as mlai_settings


def _legacy_token(secret: str, plaintext: str) -> str:
    key = base64.urlsafe_b64encode(hashlib.sha256(secret.encode()).digest())
    return Fernet(key).encrypt(plaintext.encode()).decode()


class FieldEncryptionTests(SimpleTestCase):
    @override_settings(
        FIELD_ENCRYPTION_KEY="",
        SECRET_KEY="unit-test-secret",
        LEGACY_FIELD_ENCRYPTION_SECRET="",
    )
    def test_round_trip_derived_from_secret_key(self):
        token = fields_mod.encrypt_value("hello-token")
        self.assertNotEqual(token, "hello-token")
        self.assertEqual(fields_mod.decrypt_value(token), "hello-token")

    def test_round_trip_with_dedicated_key(self):
        key = Fernet.generate_key().decode()
        with override_settings(
            FIELD_ENCRYPTION_KEY=key,
            SECRET_KEY="anything",
            LEGACY_FIELD_ENCRYPTION_SECRET="",
        ):
            token = fields_mod.encrypt_value("dedicated-value")
            self.assertEqual(fields_mod.decrypt_value(token), "dedicated-value")

    def test_legacy_ciphertext_still_decrypts_after_rotation(self):
        legacy_secret = "old-committed-secret"
        legacy_token = _legacy_token(legacy_secret, "legacy-value")
        new_key = Fernet.generate_key().decode()
        # SECRET_KEY rotated to a new value, dedicated key introduced, but the
        # legacy secret is retained for decryption only.
        with override_settings(
            FIELD_ENCRYPTION_KEY=new_key,
            SECRET_KEY="rotated-new-secret",
            LEGACY_FIELD_ENCRYPTION_SECRET=legacy_secret,
        ):
            self.assertEqual(fields_mod.decrypt_value(legacy_token), "legacy-value")
            # New writes use the new dedicated key and still round-trip.
            fresh = fields_mod.encrypt_value("fresh-value")
            self.assertEqual(fields_mod.decrypt_value(fresh), "fresh-value")

    @override_settings(FIELD_ENCRYPTION_KEY="not-a-valid-fernet-key")
    def test_invalid_dedicated_key_raises_clear_error(self):
        with self.assertRaises(ImproperlyConfigured):
            fields_mod.encrypt_value("x")


class SecretKeyResolutionTests(SimpleTestCase):
    def test_env_secret_used_when_present(self):
        with mock.patch.dict(os.environ, {"SECRET_KEY": "from-env"}, clear=False):
            self.assertEqual(
                mlai_settings._resolve_secret_key(is_production=True), "from-env"
            )

    def test_production_without_env_fails_closed(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(ImproperlyConfigured):
                mlai_settings._resolve_secret_key(is_production=True)

    def test_dev_without_env_uses_insecure_fallback(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertEqual(
                mlai_settings._resolve_secret_key(is_production=False),
                mlai_settings._DEV_INSECURE_SECRET_KEY,
            )
