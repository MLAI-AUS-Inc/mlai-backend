"""Pure verification tests: unittest, no Django setup or database/migrations."""

from datetime import datetime, timezone
from unittest import TestCase
from unittest.mock import Mock, patch
from uuid import uuid4

from appstoreserverlibrary.models.Environment import Environment
from appstoreserverlibrary.models.InAppOwnershipType import InAppOwnershipType
from appstoreserverlibrary.models.JWSTransactionDecodedPayload import JWSTransactionDecodedPayload
from appstoreserverlibrary.models.Type import Type
from appstoreserverlibrary.signed_data_verifier import VerificationException, VerificationStatus

from roo.apple_verification import (
    AppleVerificationUnavailable, InvalidApplePurchase,
    PRODUCT_MICROROO, verify_purchase, verify_notification,
)


class AppleVerificationTests(TestCase):
    def payload(self, **overrides):
        now = int(datetime.now(timezone.utc).timestamp() * 1000)
        values = dict(
            transactionId="10001", originalTransactionId="10001",
            bundleId="au.mlai.chat", environment=Environment.PRODUCTION,
            productId=next(iter(PRODUCT_MICROROO)), quantity=1,
            type=Type.CONSUMABLE, inAppOwnershipType=InAppOwnershipType.PURCHASED,
            appAccountToken=str(uuid4()), purchaseDate=now, signedDate=now,
            currency="AUD", price=19990,
        )
        values.update(overrides)
        return JWSTransactionDecodedPayload(**values)

    def test_real_verifier_rejects_unsigned_or_malformed_transactions(self):
        for value in (None, "", "x" * 100001, "not-a-jws", "a.b.c",
                      "eyJhbGciOiJub25lIn0.eyJidW5kbGVJZCI6ImF1Lm1sYWkuY2hhdCJ9."):
            with self.subTest(value=str(value)[:30]):
                with self.assertRaises(InvalidApplePurchase):
                    verify_purchase(value)

    @patch("roo.apple_verification._verifier")
    def test_verified_product_amount_is_calculated_from_catalogue(self, factory):
        decoded = self.payload(quantity=2)
        factory.return_value.verify_and_decode_signed_transaction.return_value = decoded
        purchase = verify_purchase("header.payload.signature")
        self.assertEqual(purchase.microroo, 20_000_000)
        self.assertEqual(str(purchase.account_token), decoded.appAccountToken)
        self.assertEqual(purchase.price_milliunits, 19990)
        self.assertEqual(len(purchase.payload_digest), 64)
        factory.assert_called_once_with(Environment.PRODUCTION)

    @patch("roo.apple_verification._verifier")
    def test_wrong_app_product_environment_owner_or_quantity_is_rejected(self, factory):
        for overrides in (
            {"bundleId": "other.app"}, {"productId": "invented"},
            {"environment": Environment.SANDBOX}, {"quantity": 0},
            {"quantity": True}, {"quantity": 11}, {"appAccountToken": None},
            {"appAccountToken": "00000000-0000-0000-0000-000000000000"},
            {"type": Type.NON_CONSUMABLE},
            {"inAppOwnershipType": InAppOwnershipType.FAMILY_SHARED},
            {"purchaseDate": -1}, {"signedDate": 999999999999999},
            {"transactionId": "x"}, {"currency": "INVALID"}, {"price": -1},
        ):
            with self.subTest(overrides=overrides):
                factory.return_value.verify_and_decode_signed_transaction.return_value = self.payload(**overrides)
                with self.assertRaises(InvalidApplePurchase):
                    verify_purchase("header.payload.signature")

    @patch("roo.apple_verification._verifier")
    def test_revocation_network_failure_is_retryable_not_invalid_purchase(self, factory):
        factory.return_value.verify_and_decode_signed_transaction.side_effect = VerificationException(
            VerificationStatus.RETRYABLE_VERIFICATION_FAILURE)
        with self.assertRaises(AppleVerificationUnavailable):
            verify_purchase("header.payload.signature")

    @patch("roo.apple_verification._verifier")
    def test_bad_signature_never_falls_back_to_sandbox(self, factory):
        factory.return_value.verify_and_decode_signed_transaction.side_effect = VerificationException(
            VerificationStatus.INVALID_ENVIRONMENT)
        with self.assertRaises(InvalidApplePurchase):
            verify_purchase("header.payload.signature")
        factory.assert_called_once_with(Environment.PRODUCTION)

    @patch("roo.apple_verification._verifier")
    def test_refund_notification_requires_separate_nested_verification(self, factory):
        notification = Mock(
            notificationUUID=str(uuid4()), version="2.0", subtype=None,
            signedDate=int(datetime.now(timezone.utc).timestamp() * 1000),
        )
        notification.notificationType.value = "REFUND"
        notification.data.signedTransactionInfo = "nested.payload.signature"
        factory.return_value.verify_and_decode_notification.return_value = notification
        factory.return_value.verify_and_decode_signed_transaction.side_effect = VerificationException(
            VerificationStatus.INVALID_CERTIFICATE)
        with self.assertRaises(InvalidApplePurchase):
            verify_notification("header.payload.signature")
        factory.return_value.verify_and_decode_signed_transaction.assert_called_once_with(
            "nested.payload.signature")
