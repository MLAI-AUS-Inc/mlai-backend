import hashlib
import inspect
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from decimal import Decimal
from unittest import skipUnless

import jwt
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import (
    Encoding,
    NoEncryption,
    PrivateFormat,
    PublicFormat,
)
from django.db import close_old_connections, connection
from django.test import TestCase, TransactionTestCase, override_settings
from django.urls import reverse
from django.utils import timezone
from rest_framework import status
from rest_framework.test import APITestCase

from community_chat.account_sessions import ACCESS_TOKEN_PREFIX
from community_chat.models import CommunityChatAccountSession
from core.models import User
from roo.coding import (
    CodingError,
    admit_call,
    calculate_charge_microroo,
    conservative_call_reservation,
    finalize_turn,
    reconcile_coding_reservations,
    release_stale_ambiguous_calls,
)
from roo.models import (
    CodingModelCall,
    CodingPricingVersion,
    CodingTurn,
    Ledger,
    PointsAccount,
    PointsPurchase,
)
from roo.permissions import IdempotencyConflictError
from roo.services import PointsService


_SIGNING_KEY = Ed25519PrivateKey.generate()
_PRIVATE_PEM = _SIGNING_KEY.private_bytes(
    Encoding.PEM,
    PrivateFormat.PKCS8,
    NoEncryption(),
).decode("utf-8")
_PUBLIC_PEM = _SIGNING_KEY.public_key().public_bytes(
    Encoding.PEM,
    PublicFormat.SubjectPublicKeyInfo,
).decode("utf-8")


CODING_SETTINGS = {
    "MLAI_CODING_PILOT_EMAILS": ["pilot@mlai.au"],
    "MLAI_CODING_TICKET_PRIVATE_KEY": _PRIVATE_PEM,
    "MLAI_CODING_TICKET_PUBLIC_KEY": _PUBLIC_PEM,
    "MLAI_CODING_TICKET_KEY_ID": "test-key",
    "MLAI_CODING_TICKET_ISSUER": "api.mlai.au",
    "MLAI_CODING_TICKET_AUDIENCE": "mlai-kimi-inference",
    "MLAI_CODING_INFERENCE_BASE_URL": "https://inference.mlai.au/v1",
    "MLAI_CODING_DISPATCH_LEASE_SECONDS": 120,
    "ROO_API_KEY": "coding-test-roo-api-key-that-is-long-enough",
}


def ensure_pricing():
    return CodingPricingVersion.objects.update_or_create(
        version="do-kimi-k3-2026-08",
        defaults={
            "model": "kimi-k3",
            "input_usd_per_million": Decimal("3.000000"),
            "cached_input_usd_per_million": Decimal("0.600000"),
            "output_usd_per_million": Decimal("15.000000"),
            "usd_aud_rate": Decimal("1.500000"),
            "margin_multiplier": Decimal("1.300000"),
            "aud_per_roo": Decimal("1.000000"),
            "is_active": True,
        },
    )[0]


def create_account_session(user, *, installation_id=None):
    raw = f"{ACCESS_TOKEN_PREFIX}{uuid.uuid4().hex}{uuid.uuid4().hex}"
    now = timezone.now()
    session = CommunityChatAccountSession.objects.create(
        user=user,
        public_key="a" * 64,
        installation_id=installation_id or uuid.uuid4(),
        client_id="mlai-desktop-test",
        origin="tauri://localhost",
        platform="macos",
        name="Test Mac",
        access_token_hash=hashlib.sha256(raw.encode("utf-8")).hexdigest(),
        refresh_token_hash=hashlib.sha256(f"refresh-{raw}".encode("utf-8")).hexdigest(),
        auth_version=user.auth_version,
        access_expires_at=now + timedelta(minutes=15),
        expires_at=now + timedelta(days=30),
    )
    return session, raw


class MicrorooCompatibilityTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(email="precision@mlai.au")

    def test_legacy_account_is_initialized_once_and_services_dual_write(self):
        account = PointsAccount.objects.create(
            user=self.user,
            balance=3,
            earned_balance=3,
            lifetime_earned=3,
        )
        balance = PointsService.get_balance(self.user)
        self.assertEqual(balance["balance"], 3)
        self.assertEqual(balance["balance_microroo"], 3_000_000)

        PointsService.spend(
            user=self.user,
            delta=1,
            source="TOOLS",
            description="compatibility",
            created_by_slack_id="TEST",
            idempotency_key="compat-spend",
        )
        account.refresh_from_db()
        self.assertTrue(account.microroo_initialized)
        self.assertEqual(account.balance, 2)
        self.assertEqual(account.balance_microroo, 2_000_000)
        self.assertEqual(Ledger.objects.get().delta_microroo, -1_000_000)

    def test_initialized_zero_is_not_rehydrated_from_stale_legacy_value(self):
        account = PointsAccount.objects.create(
            user=self.user,
            balance=9,
            balance_microroo=0,
            microroo_initialized=True,
        )
        balance = PointsService.get_balance(self.user)
        account.refresh_from_db()
        self.assertEqual(balance["balance_microroo"], 0)
        self.assertEqual(account.balance_microroo, 0)

    def test_fractional_spend_projects_only_whole_spendable_roo(self):
        account = PointsAccount.objects.create(
            user=self.user,
            balance=2,
            earned_balance=2,
            balance_microroo=2_000_000,
            earned_balance_microroo=2_000_000,
            microroo_initialized=True,
        )
        ledger, created = PointsService.spend_microroo(
            user=self.user,
            delta_microroo=25_001,
            source="TOOLS",
            description="fractional coding call",
            created_by_slack_id="TEST",
            idempotency_key="micro-spend",
        )
        account.refresh_from_db()
        self.assertTrue(created)
        self.assertEqual(account.balance_microroo, 1_974_999)
        self.assertEqual(account.balance, 1)
        self.assertEqual(ledger.delta_microroo, -25_001)

    def test_microroo_idempotency_key_mismatch_is_rejected(self):
        PointsAccount.objects.create(
            user=self.user,
            balance=2,
            earned_balance=2,
        )
        PointsService.spend_microroo(
            user=self.user,
            delta_microroo=10_000,
            source="TOOLS",
            description="first",
            created_by_slack_id="TEST",
            idempotency_key="shared-key",
            reference_type="KIMI_MODEL_CALL",
            reference_id="first",
        )
        with self.assertRaises(IdempotencyConflictError):
            PointsService.spend_microroo(
                user=self.user,
                delta_microroo=20_000,
                source="TOOLS",
                description="second",
                created_by_slack_id="TEST",
                idempotency_key="shared-key",
                reference_type="KIMI_MODEL_CALL",
                reference_id="second",
            )


class CodingPricingTests(TestCase):
    def test_cost_formula_includes_cache_fx_and_margin_with_ceiling(self):
        pricing = ensure_pricing()
        charge = calculate_charge_microroo(
            pricing,
            input_tokens=1_000,
            cached_input_tokens=250,
            output_tokens=200,
        )
        # ((750*3 + 250*.60 + 200*15) / 1m) * 1.5 * 1.3 Roo
        self.assertEqual(charge, 10_530)

    def test_invalid_pricing_fails_closed(self):
        pricing = ensure_pricing()
        pricing.aud_per_roo = Decimal("0")
        with self.assertRaises(CodingError) as raised:
            calculate_charge_microroo(
                pricing,
                input_tokens=1,
                cached_input_tokens=0,
                output_tokens=1,
            )
        self.assertEqual(raised.exception.code, "pricing_unavailable")

        pricing.aud_per_roo = Decimal("1.000000")
        pricing.output_usd_per_million = Decimal("0")
        with self.assertRaises(CodingError) as raised:
            conservative_call_reservation(
                pricing,
                estimated_input_tokens=1,
                requested_output_tokens=1,
                available_microroo=1_000_000,
            )
        self.assertEqual(raised.exception.code, "pricing_unavailable")


@override_settings(**CODING_SETTINGS)
class CodingPublicApiTests(APITestCase):
    def setUp(self):
        ensure_pricing()
        self.user = User.objects.create_user(email="pilot@mlai.au")
        self.account = PointsAccount.objects.create(
            user=self.user,
            balance=2,
            earned_balance=2,
        )
        self.session, raw = create_account_session(self.user)
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {raw}")

    def test_entitlement_uses_authenticated_identity_and_exact_balance(self):
        response = self.client.get(reverse("community_chat_coding_entitlement"))
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertTrue(response.data["pilot_access"])
        self.assertTrue(response.data["can_start_turn"])
        self.assertEqual(response.data["balance_microroo"], "2000000")
        self.assertEqual(response.data["balance_roo"], "2.000000")
        self.assertEqual(response.data["pricing"]["margin_multiplier"], "1.300000")

    def test_entitlement_poll_does_not_reconcile_another_users_turn(self):
        other = User.objects.create_user(email="unrelated@mlai.au")
        other_session, _ = create_account_session(other)
        other_turn = CodingTurn.objects.create(
            user=other,
            account_session=other_session,
            device_id=other_session.installation_id,
            local_session_id=uuid.uuid4(),
            idempotency_key=uuid.uuid4(),
            pricing_version=ensure_pricing(),
            reserved_microroo=1_000_000,
            expires_at=timezone.now() - timedelta(seconds=1),
        )
        unrelated_call = CodingModelCall.objects.create(
            turn=other_turn,
            call_id=uuid.uuid4(),
            estimated_input_tokens=10,
            requested_output_tokens=10,
            max_output_tokens=10,
            reserved_microroo=1_000,
            dispatch_owner_hash="b" * 64,
            dispatch_lease_expires_at=timezone.now() - timedelta(seconds=1),
        )

        response = self.client.get(reverse("community_chat_coding_entitlement"))

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        other_turn.refresh_from_db()
        unrelated_call.refresh_from_db()
        self.assertEqual(other_turn.status, CodingTurn.Status.ACTIVE)
        self.assertEqual(unrelated_call.status, CodingModelCall.Status.RESERVED)

        created = self.client.post(
            reverse("community_chat_coding_turn_create"),
            {
                "idempotency_key": str(uuid.uuid4()),
                "local_session_id": str(uuid.uuid4()),
                "model": "kimi-k3",
            },
            format="json",
        )
        refreshed = self.client.post(
            reverse(
                "community_chat_coding_turn_ticket_refresh",
                args=[created.data["turn_id"]],
            ),
            format="json",
        )
        self.assertEqual(created.status_code, status.HTTP_201_CREATED)
        self.assertEqual(refreshed.status_code, status.HTTP_200_OK)
        other_turn.refresh_from_db()
        unrelated_call.refresh_from_db()
        self.assertEqual(other_turn.status, CodingTurn.Status.ACTIVE)
        self.assertEqual(unrelated_call.status, CodingModelCall.Status.RESERVED)

    def test_legacy_balance_endpoint_reports_spendable_balance_during_reservation(self):
        created = self.client.post(
            reverse("community_chat_coding_turn_create"),
            {
                "idempotency_key": str(uuid.uuid4()),
                "local_session_id": str(uuid.uuid4()),
                "model": "kimi-k3",
            },
            format="json",
        )
        self.assertEqual(created.status_code, status.HTTP_201_CREATED)

        # The legacy JWT endpoint must not advertise the reserved account total
        # as spendable while the Coding turn owns it.
        self.client.force_authenticate(user=self.user)
        response = self.client.get(reverse("current-user-balance"))

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["balance"], 0)
        self.assertEqual(response.data["balance_microroo"], "0")
        self.assertEqual(response.data["balance_roo"], "0.000000")
        self.assertEqual(response.data["reserved_microroo"], "2000000")
        self.assertEqual(response.data["total_balance_microroo"], "2000000")

    def test_non_allowlisted_user_gets_readable_denial_without_identity_override(self):
        other = User.objects.create_user(email="other@mlai.au")
        _, raw = create_account_session(other)
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {raw}")
        response = self.client.get(reverse("community_chat_coding_entitlement"))
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertFalse(response.data["pilot_access"])
        self.assertFalse(response.data["can_start_turn"])

    def test_turn_returns_device_scoped_five_minute_eddsa_ticket(self):
        local_id = uuid.uuid4()
        response = self.client.post(
            reverse("community_chat_coding_turn_create"),
            {
                "idempotency_key": str(uuid.uuid4()),
                "local_session_id": str(local_id),
                "model": "kimi-k3",
            },
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(response.data["reserved_microroo"], "2000000")
        claims = jwt.decode(
            response.data["inference_ticket"],
            _SIGNING_KEY.public_key(),
            algorithms=["EdDSA"],
            audience="mlai-kimi-inference",
            issuer="api.mlai.au",
        )
        self.assertEqual(claims["sub"], str(self.user.community_chat_profile_id))
        self.assertEqual(claims["turn_id"], response.data["turn_id"])
        self.assertEqual(claims["device_id"], str(self.session.installation_id))
        self.assertEqual(claims["model"], "kimi-k3")
        self.assertLessEqual(claims["exp"] - claims["iat"], 300)
        turn = CodingTurn.objects.get()
        self.assertFalse(hasattr(turn, "inference_ticket"))

    def test_turn_idempotency_and_single_active_turn(self):
        idem = uuid.uuid4()
        local_id = uuid.uuid4()
        payload = {
            "idempotency_key": str(idem),
            "local_session_id": str(local_id),
            "model": "kimi-k3",
        }
        first = self.client.post(reverse("community_chat_coding_turn_create"), payload, format="json")
        replay = self.client.post(reverse("community_chat_coding_turn_create"), payload, format="json")
        other = self.client.post(
            reverse("community_chat_coding_turn_create"),
            {**payload, "idempotency_key": str(uuid.uuid4())},
            format="json",
        )
        self.assertEqual(first.status_code, status.HTTP_201_CREATED)
        self.assertEqual(replay.status_code, status.HTTP_200_OK)
        self.assertEqual(replay.data["turn_id"], first.data["turn_id"])
        self.assertEqual(other.status_code, status.HTTP_409_CONFLICT)
        self.assertEqual(other.data["code"], "active_turn_exists")

    def test_zero_balance_cannot_start_but_entitlement_remains_readable(self):
        self.account.balance = 0
        self.account.earned_balance = 0
        self.account.save()
        entitlement = self.client.get(reverse("community_chat_coding_entitlement"))
        turn = self.client.post(
            reverse("community_chat_coding_turn_create"),
            {
                "idempotency_key": str(uuid.uuid4()),
                "local_session_id": str(uuid.uuid4()),
                "model": "kimi-k3",
            },
            format="json",
        )
        self.assertEqual(entitlement.status_code, status.HTTP_200_OK)
        self.assertFalse(entitlement.data["can_start_turn"])
        self.assertEqual(turn.status_code, status.HTTP_402_PAYMENT_REQUIRED)
        self.assertFalse(CodingTurn.objects.exists())

    def test_jwks_is_public_and_contains_only_public_key(self):
        self.client.credentials()
        response = self.client.get(reverse("community_chat_coding_jwks"))
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["keys"][0]["kid"], "test-key")
        self.assertEqual(response.data["keys"][0]["kty"], "OKP")
        self.assertIn("x", response.data["keys"][0])
        self.assertNotIn("d", response.data["keys"][0])

    def test_community_chat_session_can_create_top_up_for_request_user(self):
        response = self.client.post(
            reverse("current-user-purchase"),
            {"pack_id": "topup_5", "purchase_from": {"source": "mlai-coding"}},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(response.data["pack_id"], "topup_5")
        self.assertEqual(PointsPurchase.objects.get().user_id, self.user.id)

    def test_finalize_treats_an_admitted_unsettled_call_as_ambiguous(self):
        turn_response = self.client.post(
            reverse("community_chat_coding_turn_create"),
            {
                "idempotency_key": str(uuid.uuid4()),
                "local_session_id": str(uuid.uuid4()),
                "model": "kimi-k3",
            },
            format="json",
        )
        turn = CodingTurn.objects.get(id=turn_response.data["turn_id"])
        call = CodingModelCall.objects.create(
            turn=turn,
            call_id=uuid.uuid4(),
            estimated_input_tokens=10,
            requested_output_tokens=10,
            max_output_tokens=10,
            reserved_microroo=1_000,
            dispatch_owner_hash="a" * 64,
            dispatch_lease_expires_at=timezone.now() + timedelta(minutes=2),
            dispatch_started_at=timezone.now(),
        )
        response = self.client.post(
            reverse("community_chat_coding_turn_finalize", args=[turn.id]),
            {"outcome": "cancelled"},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_202_ACCEPTED)
        self.assertEqual(response.data["status"], CodingTurn.Status.RECONCILING)
        call.refresh_from_db()
        self.assertEqual(call.status, CodingModelCall.Status.AMBIGUOUS)
        self.assertEqual(call.failure_reason, "settlement_unconfirmed")
        self.assertIsNotNone(call.reconcile_after)

        conflict = self.client.post(
            reverse("community_chat_coding_turn_finalize", args=[turn.id]),
            {"outcome": "completed"},
            format="json",
        )
        self.assertEqual(conflict.status_code, status.HTTP_409_CONFLICT)
        self.assertEqual(conflict.data["code"], "idempotency_conflict")

    def test_finalize_releases_a_call_that_never_started_dispatch(self):
        turn_response = self.client.post(
            reverse("community_chat_coding_turn_create"),
            {
                "idempotency_key": str(uuid.uuid4()),
                "local_session_id": str(uuid.uuid4()),
                "model": "kimi-k3",
            },
            format="json",
        )
        turn = CodingTurn.objects.get(id=turn_response.data["turn_id"])
        call = CodingModelCall.objects.create(
            turn=turn,
            call_id=uuid.uuid4(),
            estimated_input_tokens=10,
            requested_output_tokens=10,
            max_output_tokens=10,
            reserved_microroo=1_000,
            dispatch_owner_hash="c" * 64,
            dispatch_lease_expires_at=timezone.now() + timedelta(minutes=2),
        )

        response = self.client.post(
            reverse("community_chat_coding_turn_finalize", args=[turn.id]),
            {"outcome": "cancelled"},
            format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["status"], CodingTurn.Status.CANCELLED)
        call.refresh_from_db()
        self.assertEqual(call.status, CodingModelCall.Status.RELEASED)
        self.assertEqual(call.failure_reason, "dispatch_not_started")
        self.assertIsNone(call.reconcile_after)


@override_settings(**CODING_SETTINGS)
class CodingInternalCallApiTests(APITestCase):
    def setUp(self):
        self.pricing = ensure_pricing()
        self.user = User.objects.create_user(email="pilot@mlai.au")
        PointsAccount.objects.create(
            user=self.user,
            balance=2,
            earned_balance=2,
        )
        session, _ = create_account_session(self.user)
        self.turn = CodingTurn.objects.create(
            user=self.user,
            account_session=session,
            device_id=session.installation_id,
            local_session_id=uuid.uuid4(),
            idempotency_key=uuid.uuid4(),
            pricing_version=self.pricing,
            reserved_microroo=2_000_000,
        )
        self.call_id = uuid.uuid4()
        self.dispatch_owner = uuid.uuid4().hex
        self.client.credentials(
            HTTP_X_API_KEY="coding-test-roo-api-key-that-is-long-enough"
        )

    def admit(self, *, start_dispatch=True, **overrides):
        dispatch_owner = overrides.pop("dispatch_owner", self.dispatch_owner)
        payload = {
            "turn_id": str(self.turn.id),
            "call_id": str(self.call_id),
            "subject": str(self.user.community_chat_profile_id),
            "device_id": str(self.turn.device_id),
            "estimated_input_tokens": 1_000,
            "requested_output_tokens": 1_000,
            "dispatch_owner": dispatch_owner,
            **overrides,
        }
        response = self.client.post(reverse("kimi-call-admit"), payload, format="json")
        if start_dispatch and response.status_code in (
            status.HTTP_200_OK,
            status.HTTP_201_CREATED,
        ):
            dispatched = self.client.post(
                reverse("kimi-call-dispatch"),
                {
                    "reservation_id": response.data["reservation_id"],
                    "turn_id": str(self.turn.id),
                    "call_id": str(self.call_id),
                    "dispatch_owner": dispatch_owner,
                },
                format="json",
            )
            self.assertEqual(dispatched.status_code, status.HTTP_201_CREATED)
            self.assertTrue(dispatched.data["dispatch_allowed"])
        return response

    def dispatch(self, admitted, *, dispatch_owner=None):
        return self.client.post(
            reverse("kimi-call-dispatch"),
            {
                "reservation_id": admitted.data["reservation_id"],
                "turn_id": str(self.turn.id),
                "call_id": str(self.call_id),
                "dispatch_owner": dispatch_owner or self.dispatch_owner,
            },
            format="json",
        )

    def test_admit_is_service_authenticated_and_replay_cannot_dispatch_twice(self):
        self.client.credentials()
        denied = self.admit(start_dispatch=False)
        self.assertEqual(denied.status_code, status.HTTP_401_UNAUTHORIZED)

        self.client.credentials(HTTP_X_API_KEY="coding-test-roo-api-key-that-is-long-enough")
        admitted = self.admit(start_dispatch=False)
        replay = self.admit(start_dispatch=False)
        conflict = self.admit(start_dispatch=False, requested_output_tokens=900)
        self.assertEqual(admitted.status_code, status.HTTP_201_CREATED)
        self.assertFalse(admitted.data["dispatch_allowed"])
        self.assertTrue(admitted.data["dispatch_start_required"])
        self.assertEqual(replay.status_code, status.HTTP_200_OK)
        self.assertEqual(replay.data["reservation_id"], admitted.data["reservation_id"])
        self.assertEqual(conflict.status_code, status.HTTP_409_CONFLICT)
        self.assertEqual(conflict.data["code"], "idempotency_conflict")

        started = self.dispatch(admitted)
        dispatch_replay = self.dispatch(admitted)
        admitted_after_start = self.admit(start_dispatch=False)
        self.assertEqual(started.status_code, status.HTTP_201_CREATED)
        self.assertTrue(started.data["dispatch_allowed"])
        self.assertEqual(dispatch_replay.status_code, status.HTTP_200_OK)
        self.assertFalse(dispatch_replay.data["dispatch_allowed"])
        self.assertEqual(admitted_after_start.status_code, status.HTTP_409_CONFLICT)
        self.assertEqual(admitted_after_start.data["code"], "call_already_dispatched")

    def test_dispatch_lease_blocks_other_owner_then_allows_safe_takeover(self):
        admitted = self.admit(start_dispatch=False)
        replacement_owner = uuid.uuid4().hex
        blocked = self.admit(
            start_dispatch=False,
            dispatch_owner=replacement_owner,
        )
        self.assertEqual(blocked.status_code, status.HTTP_409_CONFLICT)
        self.assertEqual(blocked.data["code"], "dispatch_lease_owned")

        call = CodingModelCall.objects.get(id=admitted.data["reservation_id"])
        call.dispatch_lease_expires_at = timezone.now() - timedelta(seconds=1)
        call.save(update_fields=("dispatch_lease_expires_at", "updated_at"))
        recovered = self.admit(
            start_dispatch=False,
            dispatch_owner=replacement_owner,
        )
        self.assertEqual(recovered.status_code, status.HTTP_200_OK)
        self.assertEqual(recovered.data["reservation_id"], admitted.data["reservation_id"])

        stale_failure = self.client.post(
            reverse("kimi-call-fail"),
            {
                "reservation_id": admitted.data["reservation_id"],
                "turn_id": str(self.turn.id),
                "call_id": str(self.call_id),
                "dispatch_owner": self.dispatch_owner,
                "reason": "dispatch_failed",
                "ambiguous": False,
            },
            format="json",
        )
        stale = self.dispatch(admitted)
        replacement = self.dispatch(admitted, dispatch_owner=replacement_owner)
        self.assertEqual(stale_failure.status_code, status.HTTP_409_CONFLICT)
        self.assertEqual(stale_failure.data["code"], "dispatch_owner_mismatch")
        self.assertEqual(stale.status_code, status.HTTP_409_CONFLICT)
        self.assertEqual(stale.data["code"], "dispatch_owner_mismatch")
        self.assertEqual(replacement.status_code, status.HTTP_201_CREATED)
        self.assertTrue(replacement.data["dispatch_allowed"])

    def test_admit_rejects_ticket_subject_or_device_outside_turn_scope(self):
        wrong_subject = self.admit(subject=str(uuid.uuid4()))
        wrong_device = self.admit(device_id=str(uuid.uuid4()))

        self.assertEqual(wrong_subject.status_code, status.HTTP_403_FORBIDDEN)
        self.assertEqual(wrong_subject.data["code"], "ticket_scope_mismatch")
        self.assertEqual(wrong_device.status_code, status.HTTP_403_FORBIDDEN)
        self.assertEqual(wrong_device.data["code"], "ticket_scope_mismatch")
        self.assertFalse(CodingModelCall.objects.exists())

    def test_every_call_lifecycle_endpoint_requires_a_valid_dispatch_owner(self):
        invalid_admit = self.admit(
            start_dispatch=False,
            dispatch_owner="too-short",
        )
        self.assertEqual(invalid_admit.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(invalid_admit.data["code"], "invalid_dispatch_owner")
        admitted = self.admit(start_dispatch=False)

        common = {
            "reservation_id": admitted.data["reservation_id"],
            "turn_id": str(self.turn.id),
            "call_id": str(self.call_id),
        }
        missing_dispatch = self.client.post(
            reverse("kimi-call-dispatch"),
            common,
            format="json",
        )
        missing_settlement = self.client.post(
            reverse("kimi-call-settle"),
            {
                **common,
                "input_tokens": 1,
                "cached_input_tokens": 0,
                "output_tokens": 1,
            },
            format="json",
        )
        missing_failure = self.client.post(
            reverse("kimi-call-fail"),
            {**common, "reason": "dispatch_failed", "ambiguous": False},
            format="json",
        )
        for response in (missing_dispatch, missing_settlement, missing_failure):
            self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
            self.assertEqual(response.data["code"], "invalid_dispatch_owner")

    def test_settlement_charges_exact_usage_and_is_idempotent(self):
        admitted = self.admit()
        payload = {
            "reservation_id": admitted.data["reservation_id"],
            "turn_id": str(self.turn.id),
            "call_id": str(self.call_id),
            "dispatch_owner": self.dispatch_owner,
            "provider_request_id": "provider-1",
            "provider_trace_id": "trace-1",
            "input_tokens": 1_000,
            "cached_input_tokens": 250,
            "output_tokens": 200,
        }
        stale = self.client.post(
            reverse("kimi-call-settle"),
            {**payload, "dispatch_owner": uuid.uuid4().hex},
            format="json",
        )
        settled = self.client.post(reverse("kimi-call-settle"), payload, format="json")
        replay = self.client.post(reverse("kimi-call-settle"), payload, format="json")
        self.assertEqual(stale.status_code, status.HTTP_409_CONFLICT)
        self.assertEqual(stale.data["code"], "dispatch_owner_mismatch")
        self.assertEqual(settled.status_code, status.HTTP_201_CREATED)
        self.assertEqual(settled.data["charged_microroo"], "10530")
        self.assertEqual(settled.data["balance_microroo"], "1989470")
        self.assertEqual(replay.status_code, status.HTTP_200_OK)
        self.assertEqual(replay.data["status"], "already_settled")
        self.assertEqual(Ledger.objects.count(), 1)
        ledger = Ledger.objects.get()
        self.assertEqual(ledger.delta_microroo, -10_530)
        self.assertNotIn("provider-1", ledger.description)

    def test_settlement_rejects_invalid_cache_and_changed_replay_payload(self):
        admitted = self.admit()
        payload = {
            "reservation_id": admitted.data["reservation_id"],
            "turn_id": str(self.turn.id),
            "call_id": str(self.call_id),
            "dispatch_owner": self.dispatch_owner,
            "provider_request_id": "provider-1",
            "provider_trace_id": "trace-1",
            "input_tokens": 100,
            "cached_input_tokens": 101,
            "output_tokens": 10,
        }
        invalid = self.client.post(reverse("kimi-call-settle"), payload, format="json")
        self.assertEqual(invalid.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(invalid.data["code"], "invalid_cached_input_tokens")
        payload["cached_input_tokens"] = 0
        first = self.client.post(reverse("kimi-call-settle"), payload, format="json")
        payload["provider_trace_id"] = "trace-changed"
        conflict = self.client.post(reverse("kimi-call-settle"), payload, format="json")
        self.assertEqual(first.status_code, status.HTTP_201_CREATED)
        self.assertEqual(conflict.status_code, status.HTTP_409_CONFLICT)
        self.assertEqual(conflict.data["code"], "idempotency_conflict")

    def test_settlement_holds_usage_outside_admitted_envelope_without_charging(self):
        cases = (
            ("input", {"input_tokens": 1_001, "output_tokens": 10}),
            ("output", {"input_tokens": 1_000, "output_tokens": 1_001}),
        )
        for label, usage in cases:
            with self.subTest(label=label):
                self.call_id = uuid.uuid4()
                admitted = self.admit()
                payload = {
                    "reservation_id": admitted.data["reservation_id"],
                    "turn_id": str(self.turn.id),
                    "call_id": str(self.call_id),
                    "dispatch_owner": self.dispatch_owner,
                    "provider_request_id": f"provider-{label}",
                    "provider_trace_id": f"trace-{label}",
                    "cached_input_tokens": 0,
                    **usage,
                }
                account = PointsAccount.objects.get(user=self.user)
                balance_before = account.balance_microroo
                ledger_count_before = Ledger.objects.count()

                rejected = self.client.post(
                    reverse("kimi-call-settle"), payload, format="json"
                )
                replay = self.client.post(
                    reverse("kimi-call-settle"), payload, format="json"
                )

                self.assertEqual(
                    rejected.status_code, status.HTTP_422_UNPROCESSABLE_ENTITY
                )
                self.assertEqual(
                    rejected.data["code"], "usage_outside_admitted_envelope"
                )
                self.assertEqual(rejected.data["status"], "usage_rejected")
                self.assertEqual(rejected.data["charged_microroo"], "0")
                self.assertEqual(replay.status_code, status.HTTP_200_OK)
                self.assertEqual(replay.data["status"], "already_rejected")

                call = CodingModelCall.objects.get(id=admitted.data["reservation_id"])
                self.assertEqual(call.status, CodingModelCall.Status.AMBIGUOUS)
                self.assertEqual(
                    call.failure_reason, "usage_outside_admitted_envelope"
                )
                self.assertEqual(call.input_tokens, usage["input_tokens"])
                self.assertEqual(call.output_tokens, usage["output_tokens"])
                self.assertEqual(call.provider_request_id, f"provider-{label}")
                self.assertEqual(call.trace_id, f"trace-{label}")
                self.assertIsNotNone(call.reconcile_after)
                self.assertIsNone(call.ledger_entry)
                self.assertEqual(call.charged_microroo, 0)
                self.assertEqual(call.calculated_microroo, 0)
                self.assertEqual(Ledger.objects.count(), ledger_count_before)
                account.refresh_from_db()
                self.assertEqual(account.balance_microroo, balance_before)
                self.turn.refresh_from_db()
                self.assertEqual(self.turn.settled_microroo, 0)

                call.status = CodingModelCall.Status.RELEASED
                call.reconcile_after = None
                call.settled_at = timezone.now()
                call.save(
                    update_fields=(
                        "status",
                        "reconcile_after",
                        "settled_at",
                        "updated_at",
                    )
                )
                released_replay = self.client.post(
                    reverse("kimi-call-settle"), payload, format="json"
                )
                self.assertEqual(released_replay.status_code, status.HTTP_200_OK)
                self.assertEqual(
                    released_replay.data["status"], "released_unbilled"
                )
                self.assertEqual(Ledger.objects.count(), ledger_count_before)
                account.refresh_from_db()
                self.assertEqual(account.balance_microroo, balance_before)

    def test_delayed_settlement_after_reconciliation_is_released_unbilled(self):
        admitted = self.admit()
        failure_payload = {
            "reservation_id": admitted.data["reservation_id"],
            "turn_id": str(self.turn.id),
            "call_id": str(self.call_id),
            "dispatch_owner": self.dispatch_owner,
            "reason": "settlement_unconfirmed",
            "ambiguous": True,
            "provider_request_id": "provider-late-settle",
            "provider_trace_id": "trace-late-settle",
        }
        ambiguous = self.client.post(
            reverse("kimi-call-fail"), failure_payload, format="json"
        )
        self.assertEqual(ambiguous.status_code, status.HTTP_200_OK)
        finalize_turn(turn=self.turn, outcome="failed")
        call = CodingModelCall.objects.get(id=admitted.data["reservation_id"])
        call.reconcile_after = timezone.now() - timedelta(seconds=1)
        call.save(update_fields=("reconcile_after", "updated_at"))
        self.assertEqual(reconcile_coding_reservations()["released_calls"], 1)
        call.refresh_from_db()
        self.assertEqual(call.status, CodingModelCall.Status.RELEASED)
        self.assertEqual(
            call.failure_reason,
            "settlement_unconfirmed; reconciliation_timeout",
        )

        settlement_payload = {
            "reservation_id": admitted.data["reservation_id"],
            "turn_id": str(self.turn.id),
            "call_id": str(self.call_id),
            "dispatch_owner": self.dispatch_owner,
            "provider_request_id": "provider-late-settle",
            "provider_trace_id": "trace-late-settle",
            "input_tokens": 100,
            "cached_input_tokens": 25,
            "output_tokens": 20,
        }
        account = PointsAccount.objects.get(user=self.user)
        balance_before = account.balance_microroo
        first = self.client.post(
            reverse("kimi-call-settle"), settlement_payload, format="json"
        )
        replay = self.client.post(
            reverse("kimi-call-settle"), settlement_payload, format="json"
        )

        self.assertEqual(first.status_code, status.HTTP_200_OK)
        self.assertEqual(first.data["status"], "released_unbilled")
        self.assertEqual(first.data["charged_microroo"], "0")
        self.assertEqual(replay.status_code, status.HTTP_200_OK)
        self.assertEqual(replay.data["status"], "released_unbilled")
        call.refresh_from_db()
        self.turn.refresh_from_db()
        account.refresh_from_db()
        self.assertEqual(call.status, CodingModelCall.Status.RELEASED)
        self.assertEqual(call.input_tokens, 100)
        self.assertEqual(call.cached_input_tokens, 25)
        self.assertEqual(call.output_tokens, 20)
        self.assertIn("reconciliation_timeout", call.failure_reason)
        self.assertIn("late_settlement_report", call.failure_reason)
        self.assertEqual(call.charged_microroo, 0)
        self.assertEqual(call.calculated_microroo, 0)
        self.assertIsNone(call.ledger_entry)
        self.assertEqual(self.turn.settled_microroo, 0)
        self.assertEqual(account.balance_microroo, balance_before)
        self.assertEqual(Ledger.objects.count(), 0)

        changed = self.client.post(
            reverse("kimi-call-settle"),
            {**settlement_payload, "output_tokens": 21},
            format="json",
        )
        wrong_identity = self.client.post(
            reverse("kimi-call-settle"),
            {**settlement_payload, "call_id": str(uuid.uuid4())},
            format="json",
        )
        self.assertEqual(changed.status_code, status.HTTP_409_CONFLICT)
        self.assertEqual(changed.data["code"], "idempotency_conflict")
        self.assertEqual(wrong_identity.status_code, status.HTTP_404_NOT_FOUND)
        self.assertEqual(Ledger.objects.count(), 0)

    def test_delayed_failure_after_reconciliation_is_already_released(self):
        admitted = self.admit()
        failure_payload = {
            "reservation_id": admitted.data["reservation_id"],
            "turn_id": str(self.turn.id),
            "call_id": str(self.call_id),
            "dispatch_owner": self.dispatch_owner,
            "reason": "provider_timeout",
            "ambiguous": True,
            "provider_request_id": "provider-late-fail",
            "provider_trace_id": "trace-late-fail",
        }
        ambiguous = self.client.post(
            reverse("kimi-call-fail"), failure_payload, format="json"
        )
        self.assertEqual(ambiguous.status_code, status.HTTP_200_OK)
        finalize_turn(turn=self.turn, outcome="failed")
        call = CodingModelCall.objects.get(id=admitted.data["reservation_id"])
        call.reconcile_after = timezone.now() - timedelta(seconds=1)
        call.save(update_fields=("reconcile_after", "updated_at"))
        self.assertEqual(reconcile_coding_reservations()["released_calls"], 1)
        call.refresh_from_db()
        self.assertEqual(
            call.failure_reason,
            "provider_timeout; reconciliation_timeout",
        )
        account = PointsAccount.objects.get(user=self.user)
        balance_before = account.balance_microroo

        first = self.client.post(
            reverse("kimi-call-fail"), failure_payload, format="json"
        )
        replay = self.client.post(
            reverse("kimi-call-fail"), failure_payload, format="json"
        )
        self.assertEqual(first.status_code, status.HTTP_200_OK)
        self.assertEqual(first.data["status"], "already_released")
        self.assertFalse(first.data["changed"])
        self.assertEqual(replay.status_code, status.HTTP_200_OK)
        self.assertEqual(replay.data["status"], "already_released")
        call.refresh_from_db()
        self.turn.refresh_from_db()
        account.refresh_from_db()
        self.assertEqual(call.status, CodingModelCall.Status.RELEASED)
        self.assertIn("provider_timeout; reconciliation_timeout", call.failure_reason)
        self.assertIn(
            "late_failure_report:provider_timeout:ambiguous",
            call.failure_reason,
        )
        self.assertEqual(call.charged_microroo, 0)
        self.assertIsNone(call.ledger_entry)
        self.assertEqual(self.turn.settled_microroo, 0)
        self.assertEqual(account.balance_microroo, balance_before)
        self.assertEqual(Ledger.objects.count(), 0)

        changed = self.client.post(
            reverse("kimi-call-fail"),
            {**failure_payload, "reason": "provider_unavailable"},
            format="json",
        )
        wrong_identity = self.client.post(
            reverse("kimi-call-fail"),
            {**failure_payload, "reservation_id": str(uuid.uuid4())},
            format="json",
        )
        self.assertEqual(changed.status_code, status.HTTP_409_CONFLICT)
        self.assertEqual(changed.data["code"], "idempotency_conflict")
        self.assertEqual(wrong_identity.status_code, status.HTTP_404_NOT_FOUND)
        self.assertEqual(Ledger.objects.count(), 0)

    def test_definite_failure_releases_and_ambiguous_failure_expires_at_24_hours(self):
        admitted = self.admit()
        definite = self.client.post(
            reverse("kimi-call-fail"),
            {
                "reservation_id": admitted.data["reservation_id"],
                "turn_id": str(self.turn.id),
                "call_id": str(self.call_id),
                "dispatch_owner": self.dispatch_owner,
                "reason": "provider_rejected",
                "ambiguous": False,
            },
            format="json",
        )
        self.assertEqual(definite.status_code, status.HTTP_200_OK)
        self.assertEqual(definite.data["status"], "released")
        self.assertEqual(definite.data["remaining_microroo"], "2000000")

        self.call_id = uuid.uuid4()
        admitted = self.admit()
        ambiguous = self.client.post(
            reverse("kimi-call-fail"),
            {
                "reservation_id": admitted.data["reservation_id"],
                "turn_id": str(self.turn.id),
                "call_id": str(self.call_id),
                "dispatch_owner": self.dispatch_owner,
                "reason": "provider_timeout",
                "ambiguous": True,
                "provider_request_id": "provider-ambiguous",
            },
            format="json",
        )
        self.assertEqual(ambiguous.status_code, status.HTTP_200_OK)
        call = CodingModelCall.objects.get(id=admitted.data["reservation_id"])
        self.assertEqual(call.status, CodingModelCall.Status.AMBIGUOUS)
        self.assertAlmostEqual(
            call.reconcile_after,
            timezone.now() + timedelta(hours=24),
            delta=timedelta(seconds=2),
        )
        call.reconcile_after = timezone.now() - timedelta(seconds=1)
        call.save(update_fields=("reconcile_after", "updated_at"))
        self.assertEqual(release_stale_ambiguous_calls(), 1)
        call.refresh_from_db()
        self.assertEqual(call.status, CodingModelCall.Status.RELEASED)

    def test_predispatch_failure_releases_and_settlement_is_rejected(self):
        admitted = self.admit(start_dispatch=False)
        settlement = self.client.post(
            reverse("kimi-call-settle"),
            {
                "reservation_id": admitted.data["reservation_id"],
                "turn_id": str(self.turn.id),
                "call_id": str(self.call_id),
                "dispatch_owner": self.dispatch_owner,
                "input_tokens": 10,
                "cached_input_tokens": 0,
                "output_tokens": 10,
            },
            format="json",
        )
        failed = self.client.post(
            reverse("kimi-call-fail"),
            {
                "reservation_id": admitted.data["reservation_id"],
                "turn_id": str(self.turn.id),
                "call_id": str(self.call_id),
                "dispatch_owner": self.dispatch_owner,
                "reason": "provider_timeout",
                "ambiguous": True,
            },
            format="json",
        )

        self.assertEqual(settlement.status_code, status.HTTP_409_CONFLICT)
        self.assertEqual(settlement.data["code"], "dispatch_not_started")
        self.assertEqual(failed.status_code, status.HTTP_200_OK)
        self.assertEqual(failed.data["status"], CodingModelCall.Status.RELEASED)
        call = CodingModelCall.objects.get(id=admitted.data["reservation_id"])
        self.assertIsNone(call.reconcile_after)

    def test_reconciliation_releases_an_expired_unstarted_dispatch_lease(self):
        admitted = self.admit(start_dispatch=False)
        call = CodingModelCall.objects.get(id=admitted.data["reservation_id"])
        call.dispatch_lease_expires_at = timezone.now() - timedelta(seconds=1)
        call.save(update_fields=("dispatch_lease_expires_at", "updated_at"))

        result = reconcile_coding_reservations(
            user=self.user,
            turn_id=self.turn.id,
        )

        call.refresh_from_db()
        self.assertEqual(result["released_unstarted_calls"], 1)
        self.assertEqual(result["released_ambiguous_calls"], 0)
        self.assertEqual(call.status, CodingModelCall.Status.RELEASED)
        self.assertEqual(call.failure_reason, "dispatch_lease_expired")

    def test_low_balance_clamps_output_without_over_reserving(self):
        account = PointsAccount.objects.get(user=self.user)
        account.balance = 0
        account.earned_balance = 0
        account.balance_microroo = 10_000
        account.earned_balance_microroo = 10_000
        account.microroo_initialized = True
        account.save()
        self.turn.reserved_microroo = 10_000
        self.turn.save(update_fields=("reserved_microroo", "updated_at"))
        response = self.admit()
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(response.data["max_output_tokens"], 141)
        self.assertLessEqual(int(response.data["reserved_microroo"]), 10_000)

    def test_multiple_admitted_calls_collectively_never_reserve_past_turn_balance(self):
        self.turn.reserved_microroo = 50_000
        self.turn.save(update_fields=("reserved_microroo", "updated_at"))
        first = self.admit()
        self.call_id = uuid.uuid4()
        second = self.admit()
        self.assertEqual(first.status_code, status.HTTP_201_CREATED)
        self.assertEqual(second.status_code, status.HTTP_201_CREATED)
        reserved = sum(
            self.turn.model_calls.values_list("reserved_microroo", flat=True)
        )
        self.assertLessEqual(reserved, self.turn.reserved_microroo)
        self.assertEqual(
            int(second.data["remaining_microroo"]),
            self.turn.reserved_microroo - reserved,
        )

    def test_settlement_uses_immutable_call_pricing_snapshot(self):
        admitted = self.admit()
        call = CodingModelCall.objects.get(id=admitted.data["reservation_id"])
        self.assertEqual(call.pricing_version_snapshot, self.pricing.version)
        self.assertEqual(call.usd_aud_rate, Decimal("1.500000"))
        self.pricing.usd_aud_rate = Decimal("9.000000")
        self.pricing.save(update_fields=("usd_aud_rate",))
        response = self.client.post(
            reverse("kimi-call-settle"),
            {
                "reservation_id": admitted.data["reservation_id"],
                "turn_id": str(self.turn.id),
                "call_id": str(self.call_id),
                "dispatch_owner": self.dispatch_owner,
                "provider_request_id": "provider-snapshot",
                "provider_trace_id": "trace-snapshot",
                "input_tokens": 1_000,
                "cached_input_tokens": 250,
                "output_tokens": 200,
            },
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(response.data["charged_microroo"], "10530")

    def test_stale_active_turn_without_calls_releases_full_reservation(self):
        self.turn.expires_at = timezone.now() - timedelta(seconds=1)
        self.turn.save(update_fields=("expires_at", "updated_at"))
        result = reconcile_coding_reservations()
        self.turn.refresh_from_db()
        self.assertEqual(result["expired_turns"], 1)
        self.assertEqual(self.turn.status, CodingTurn.Status.FAILED)
        self.assertEqual(self.turn.released_microroo, 2_000_000)

    def test_stale_active_turn_with_admitted_call_holds_then_releases_for_reconciliation(self):
        admitted = self.admit()
        self.turn.expires_at = timezone.now() - timedelta(seconds=1)
        self.turn.save(update_fields=("expires_at", "updated_at"))
        first = reconcile_coding_reservations()
        self.turn.refresh_from_db()
        call = CodingModelCall.objects.get(id=admitted.data["reservation_id"])
        self.assertEqual(first["expired_turns"], 1)
        self.assertEqual(self.turn.status, CodingTurn.Status.RECONCILING)
        self.assertEqual(call.status, CodingModelCall.Status.AMBIGUOUS)
        self.assertEqual(call.failure_reason, "settlement_unconfirmed")
        call.reconcile_after = timezone.now() - timedelta(seconds=1)
        call.save(update_fields=("reconcile_after", "updated_at"))
        second = reconcile_coding_reservations()
        self.turn.refresh_from_db()
        call.refresh_from_db()
        self.assertEqual(second["released_calls"], 1)
        self.assertEqual(call.status, CodingModelCall.Status.RELEASED)
        self.assertEqual(self.turn.status, CodingTurn.Status.FAILED)
        self.assertEqual(self.turn.released_microroo, 2_000_000)


class CodingSchedulerRegistrationTests(TestCase):
    def test_global_reconciliation_runs_from_the_production_scheduler(self):
        from core.management.commands.run_scheduled_discovery import Command

        source = inspect.getsource(Command.handle)
        self.assertIn(
            '("coding_reconciliation", reconcile_coding_reservations)',
            source,
        )


@override_settings(**CODING_SETTINGS)
@skipUnless(connection.vendor == "postgresql", "requires PostgreSQL row locks")
class CodingConcurrentAdmissionTests(TransactionTestCase):
    """Exercise the real select-for-update reservation path concurrently."""

    reset_sequences = True

    def setUp(self):
        pricing = ensure_pricing()
        user = User.objects.create_user(email="concurrent-pilot@mlai.au")
        self.user = user
        PointsAccount.objects.create(user=user, balance=1, earned_balance=1)
        session, _ = create_account_session(user)
        self.turn = CodingTurn.objects.create(
            user=user,
            account_session=session,
            device_id=session.installation_id,
            local_session_id=uuid.uuid4(),
            idempotency_key=uuid.uuid4(),
            pricing_version=pricing,
            reserved_microroo=50_000,
        )

    def test_concurrent_calls_cannot_collectively_reserve_past_turn_balance(self):
        barrier = threading.Barrier(3)

        def reserve(call_id):
            close_old_connections()
            try:
                barrier.wait(timeout=5)
                return admit_call(
                    turn_id=self.turn.id,
                    call_id=call_id,
                    subject=self.user.community_chat_profile_id,
                    device_id=self.turn.device_id,
                    estimated_input_tokens=1_000,
                    requested_output_tokens=1_000,
                    dispatch_owner=uuid.uuid4().hex,
                )[0].id
            finally:
                close_old_connections()

        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(reserve, uuid.uuid4()) for _ in range(2)]
            barrier.wait(timeout=5)
            reservation_ids = [future.result(timeout=10) for future in futures]

        self.assertEqual(len(set(reservation_ids)), 2)
        reserved = sum(
            CodingModelCall.objects.filter(turn=self.turn).values_list(
                "reserved_microroo", flat=True
            )
        )
        self.assertLessEqual(reserved, self.turn.reserved_microroo)
