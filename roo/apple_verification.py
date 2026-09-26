"""Verify Apple-signed purchases before passing data to the points ledger.

This module is database-independent. Neither unsigned StoreKit JSON nor a
client-supplied product amount, account ID or environment authorizes a grant.
"""

import hashlib
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path
from uuid import UUID

from appstoreserverlibrary.models.Environment import Environment
from appstoreserverlibrary.models.InAppOwnershipType import InAppOwnershipType
from appstoreserverlibrary.models.Type import Type
from appstoreserverlibrary.signed_data_verifier import (
    SignedDataVerifier, VerificationException, VerificationStatus,
)

BUNDLE_ID = "au.mlai.chat"
APP_APPLE_ID = 6808091752
PRODUCT_MICROROO = {
    "au.mlai.chat.roo.digital.10": 10_000_000,
    "au.mlai.chat.roo.digital.20": 20_000_000,
    "au.mlai.chat.roo.digital.50": 50_000_000,
}
MAX_SIGNED_PAYLOAD_BYTES = 100_000


class InvalidApplePurchase(ValueError):
    """An untrusted or unsupported purchase; never log its signed payload."""


class AppleVerificationUnavailable(RuntimeError):
    """Apple's revocation check must be retried without finishing the purchase."""


@dataclass(frozen=True)
class VerifiedApplePurchase:
    """The minimal verified data needed for durable delivery and reversals."""

    environment: str
    transaction_id: str
    original_transaction_id: str
    account_token: UUID
    product_id: str
    quantity: int
    microroo: int
    price_milliunits: int | None
    currency: str
    purchased_at: datetime
    signed_at: datetime
    revoked_at: datetime | None
    payload_digest: str


@dataclass(frozen=True)
class VerifiedAppleNotification:
    """A verified notification and its separately verified nested transaction."""

    notification_id: UUID
    environment: str
    notification_type: str
    subtype: str
    signed_at: datetime
    purchase: VerifiedApplePurchase | None
    payload_digest: str


@lru_cache(maxsize=2)
def _verifier(environment: Environment) -> SignedDataVerifier:
    root = Path(__file__).with_name("apple_pki") / "AppleRootCA-G3.cer"
    return SignedDataVerifier(
        [root.read_bytes()], True, environment, BUNDLE_ID, APP_APPLE_ID,
    )


def _validate_payload(payload: str) -> None:
    if (not isinstance(payload, str) or not payload
            or len(payload) > MAX_SIGNED_PAYLOAD_BYTES or payload.count(".") != 2
            or not payload.isascii()):
        raise InvalidApplePurchase("invalid_signed_payload")


def _decode(payload: str, *, notification: bool, environment: Environment):
    _validate_payload(payload)
    try:
        verifier = _verifier(environment)
        if notification:
            return verifier.verify_and_decode_notification(payload)
        return verifier.verify_and_decode_signed_transaction(payload)
    except VerificationException as error:
        if error.status == VerificationStatus.RETRYABLE_VERIFICATION_FAILURE:
            raise AppleVerificationUnavailable("apple_verification_unavailable") from None
        raise InvalidApplePurchase("invalid_apple_signature_or_app") from None
    except (ValueError, TypeError, OverflowError):
        raise InvalidApplePurchase("invalid_apple_payload") from None


def _date(value) -> datetime:
    if type(value) is not int or value <= 0:
        raise InvalidApplePurchase("invalid_apple_timestamp")
    try:
        result = datetime.fromtimestamp(value / 1000, tz=timezone.utc)
    except (ValueError, OverflowError, OSError):
        raise InvalidApplePurchase("invalid_apple_timestamp") from None
    if result.timestamp() > datetime.now(timezone.utc).timestamp() + 300:
        raise InvalidApplePurchase("future_apple_timestamp")
    return result


def _uuid(value) -> UUID:
    try:
        result = UUID(str(value))
    except (ValueError, AttributeError, TypeError):
        raise InvalidApplePurchase("invalid_apple_account_or_notification") from None
    if result.int == 0:
        raise InvalidApplePurchase("invalid_apple_account_or_notification")
    return result


def _transaction(payload: str, decoded, environment: Environment) -> VerifiedApplePurchase:
    if (decoded.bundleId != BUNDLE_ID or decoded.environment != environment
            or decoded.type != Type.CONSUMABLE
            or decoded.inAppOwnershipType != InAppOwnershipType.PURCHASED
            or decoded.productId not in PRODUCT_MICROROO):
        raise InvalidApplePurchase("unsupported_apple_purchase")
    if type(decoded.quantity) is not int or not 1 <= decoded.quantity <= 10:
        raise InvalidApplePurchase("invalid_apple_quantity")
    for value in (decoded.transactionId, decoded.originalTransactionId):
        if not isinstance(value, str) or re.fullmatch(r"[0-9]{1,64}", value) is None:
            raise InvalidApplePurchase("invalid_apple_transaction_id")
    if decoded.price is not None and (type(decoded.price) is not int or decoded.price < 0):
        raise InvalidApplePurchase("invalid_apple_price")
    currency = decoded.currency or ""
    if currency and re.fullmatch(r"[A-Z]{3}", currency) is None:
        raise InvalidApplePurchase("invalid_apple_currency")
    return VerifiedApplePurchase(
        environment=environment.value,
        transaction_id=decoded.transactionId,
        original_transaction_id=decoded.originalTransactionId,
        account_token=_uuid(decoded.appAccountToken),
        product_id=decoded.productId,
        quantity=decoded.quantity,
        microroo=PRODUCT_MICROROO[decoded.productId] * decoded.quantity,
        price_milliunits=decoded.price,
        currency=currency,
        purchased_at=_date(decoded.purchaseDate),
        signed_at=_date(decoded.signedDate),
        revoked_at=_date(decoded.revocationDate) if decoded.revocationDate is not None else None,
        payload_digest=hashlib.sha256(payload.encode("ascii")).hexdigest(),
    )


def verify_purchase(payload: str, *, sandbox: bool = False) -> VerifiedApplePurchase:
    """Verify in an explicit server-selected environment; never auto-fallback."""
    environment = Environment.SANDBOX if sandbox else Environment.PRODUCTION
    decoded = _decode(payload, notification=False, environment=environment)
    return _transaction(payload, decoded, environment)


def verify_notification(payload: str, *, sandbox: bool = False) -> VerifiedAppleNotification:
    """Verify the outer app identity and nested transaction independently."""
    environment = Environment.SANDBOX if sandbox else Environment.PRODUCTION
    decoded = _decode(payload, notification=True, environment=environment)
    kind = decoded.notificationType.value if decoded.notificationType else ""
    if not kind or decoded.version != "2.0" or decoded.data is None:
        raise InvalidApplePurchase("unsupported_apple_notification")
    purchase = None
    if decoded.data.signedTransactionInfo:
        purchase = verify_purchase(decoded.data.signedTransactionInfo, sandbox=sandbox)
    elif kind != "TEST":
        raise InvalidApplePurchase("missing_apple_transaction")
    return VerifiedAppleNotification(
        notification_id=_uuid(decoded.notificationUUID),
        environment=environment.value,
        notification_type=kind,
        subtype=decoded.subtype.value if decoded.subtype else "",
        signed_at=_date(decoded.signedDate),
        purchase=purchase,
        payload_digest=hashlib.sha256(payload.encode("ascii")).hexdigest(),
    )
