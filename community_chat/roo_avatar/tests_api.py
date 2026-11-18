"""Account/rank/storage regressions; run only after exact migration approval."""

import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from threading import Barrier
from types import SimpleNamespace

from django.contrib.auth import get_user_model
from django.db import close_old_connections, connection
from django.test import TestCase, TransactionTestCase, override_settings
from django.urls import reverse
from django.utils import timezone
from rest_framework.exceptions import AuthenticationFailed
from rest_framework.test import APIClient

from community_chat.account_sessions import issue_account_session
from community_chat.models import CommunityChatAccountSession
from community_chat.roo_avatar.models import CommunityRooAvatar
from community_chat.roo_avatar.policy import RooAvatarError, empty_outfit
from community_chat.roo_avatar.services import read_avatar, update_avatar
from community_chat.volunteer.models import VolunteerMemberState
from community_chat.volunteer.policy import MICROROO
from roo.models import Ledger, PointsAccount

ORIGIN = "https://chat.example.test"
COMMUNITY = "roo-avatar-tests"
SETTINGS = {
    "COMMUNITY_CHAT_ALLOWED_ORIGINS": [ORIGIN, "mlaichat://callback"],
    "COMMUNITY_CHAT_RELAY_URL": "wss://chat.example.test",
    "COMMUNITY_CHAT_VOLUNTEER_COMMUNITY": COMMUNITY,
    "COMMUNITY_CHAT_SIGNUP_ENABLED": False,
}


def credentials_for(user, *, web=False):
    """Mint only a synthetic, owner-bound account credential."""
    return issue_account_session(
        user,
        SimpleNamespace(
            client_id="mlai-chat-web" if web else "mlai-chat-ios",
            installation_id=uuid.uuid4(),
            origin=ORIGIN if web else "mlaichat://callback",
            platform="web" if web else "ios",
            device_name="Roo avatar test",
            public_key="a" * 64,
        ),
    )


@override_settings(**SETTINGS)
class RooAvatarApiTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(
            email="avatar@example.test",
            first_name="Alex",
            avatar_url="https://example.test/photo.png",
        )
        self.credentials = credentials_for(self.user)
        self.client = APIClient()
        self.client.credentials(
            HTTP_AUTHORIZATION=f"Bearer {self.credentials.access_token}"
        )
        self.url = reverse("community_chat_roo_avatar")
        self.state = VolunteerMemberState.objects.create(
            user=self.user,
            community=COMMUNITY,
            historical_microroo=0,
            reconciled_at=timezone.now(),
        )

    def patch(self, outfit, version=0, **extra):
        return self.client.patch(
            self.url, {"version": version, "outfit": outfit, **extra}, format="json"
        )

    def test_default_owner_scope_starter_catalog_and_no_wallet_awards(self):
        before = Ledger.objects.count()
        result = self.client.get(self.url)
        self.assertEqual(result.status_code, 200)
        self.assertEqual(
            result.data["account_id"], str(self.user.community_chat_profile_id)
        )
        self.assertNotEqual(result.data["account_id"], str(self.user.pk))
        self.assertEqual(result.data["community_id"], COMMUNITY)
        self.assertEqual(result.data["relay_url"], "wss://chat.example.test")
        self.assertEqual(result.data["version"], 0)
        self.assertEqual(result.data["current_level"], 0)
        self.assertEqual(result.data["outfit"], empty_outfit())
        self.assertEqual(result["Cache-Control"], "private, no-store")
        self.assertFalse(CommunityRooAvatar.objects.exists())
        self.assertEqual(Ledger.objects.count(), before)

    def test_saved_outfit_is_shared_across_devices_but_not_accounts(self):
        selected = {**empty_outfit(), "hair": "honey_tuft", "glasses": "classic_black"}
        saved = self.patch(selected)
        self.assertEqual(saved.status_code, 200)
        self.assertEqual(saved.data["version"], 1)
        second = credentials_for(self.user)
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {second.access_token}")
        self.assertEqual(self.client.get(self.url).data["outfit"], selected)
        other = get_user_model().objects.create_user(email="other-avatar@example.test")
        other_token = credentials_for(other)
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {other_token.access_token}")
        self.assertEqual(self.client.get(self.url).data["outfit"], empty_outfit())
        self.user.refresh_from_db()
        self.assertEqual(self.user.avatar_url, "https://example.test/photo.png")

    def test_stale_version_and_forged_locked_item_do_not_change_saved_outfit(self):
        self.assertEqual(self.patch(empty_outfit()).status_code, 200)
        stale = self.patch({**empty_outfit(), "hair": "honey_tuft"})
        self.assertEqual(stale.status_code, 409)
        self.assertEqual(stale.data["error"], "roo_avatar_version_conflict")
        locked = self.patch({**empty_outfit(), "hair": "mint_mohawk"}, 1)
        self.assertEqual(locked.status_code, 403)
        self.assertEqual(locked.data["slots"], ["hair"])
        row = CommunityRooAvatar.objects.get(user=self.user)
        self.assertEqual(row.version, 1)
        self.assertIsNone(row.hair)

    def test_rank_correction_and_unknown_history_suppress_but_retain_preferences(self):
        self.state.historical_microroo = 250 * MICROROO
        self.state.save()
        selected = {
            "fur": "snow_gum",
            "hair": "mint_mohawk",
            "glasses": "lilac_stars",
            "jacket": "lilac_puffer",
            "shoes": "teal_runners",
        }
        self.assertEqual(self.patch(selected).status_code, 200)
        for total in [0, None]:
            self.state.historical_microroo = total
            self.state.save()
            result = self.client.get(self.url).data
            self.assertEqual(result["outfit"], selected)
            self.assertEqual(
                result["effective_outfit"], {**empty_outfit(), "fur": "snow_gum"}
            )
            self.assertEqual(result["history_reconciled"], total is not None)
            self.assertEqual(result["current_level"], 0 if total == 0 else None)
        self.assertEqual(
            self.patch({**empty_outfit(), "glasses": "classic_black"}, 1).status_code,
            200,
        )

    def test_fur_catalog_is_fully_unlocked_and_hair_names_are_styles(self):
        for total in [0, None]:
            self.state.historical_microroo = total
            self.state.save()
            result = self.client.get(self.url).data
            self.assertEqual(result["catalog_version"], "roo-avatar-v2")
            self.assertEqual(result["history_reconciled"], total is not None)
            for key in ["outfit", "effective_outfit", "catalog"]:
                self.assertEqual(
                    list(result[key]), ["fur", "hair", "glasses", "jacket", "shoes"]
                )
            self.assertIsNone(result["outfit"]["fur"])
            self.assertEqual(
                [(item["id"], item["name"]) for item in result["catalog"]["fur"]],
                [
                    ("golden_sand", "Golden sand"),
                    ("blue_flier", "Blue flier"),
                    ("ironbark", "Ironbark"),
                    ("snow_gum", "Snow gum"),
                    ("midnight_wallaroo", "Midnight wallaroo"),
                ],
            )
            for item in result["catalog"]["fur"]:
                self.assertEqual(item["required_level"], 0)
                self.assertTrue(item["unlocked"])
                self.assertIsNone(item["unavailable_reason"])
            self.assertEqual(
                [(item["id"], item["name"]) for item in result["catalog"]["hair"]],
                [
                    ("honey_tuft", "Tuft"),
                    ("cocoa_curls", "Curls"),
                    ("peach_fringe", "Swept fringe"),
                    ("lilac_bob", "Bob"),
                    ("mint_mohawk", "Mohawk"),
                ],
            )

    def test_fur_saves_for_unreconciled_history_and_can_return_to_default(self):
        self.state.historical_microroo = None
        self.state.save()
        saved = self.patch({**empty_outfit(), "fur": "midnight_wallaroo"})
        self.assertEqual(saved.status_code, 200)
        self.assertIsNone(saved.data["current_level"])
        self.assertEqual(saved.data["outfit"]["fur"], "midnight_wallaroo")
        self.assertEqual(saved.data["effective_outfit"]["fur"], "midnight_wallaroo")
        self.assertEqual(
            CommunityRooAvatar.objects.get(user=self.user).fur, "midnight_wallaroo"
        )
        cleared = self.patch(empty_outfit(), 1)
        self.assertEqual(cleared.status_code, 200)
        self.assertIsNone(cleared.data["outfit"]["fur"])
        self.assertIsNone(CommunityRooAvatar.objects.get(user=self.user).fur)

    def test_four_slot_outfit_from_an_earlier_client_keeps_the_saved_fur(self):
        earlier = {
            "hair": "honey_tuft",
            "glasses": None,
            "jacket": None,
            "shoes": "red_sneakers",
        }
        first = self.patch(earlier)
        self.assertEqual(first.status_code, 200)
        self.assertEqual(first.data["version"], 1)
        self.assertEqual(first.data["outfit"], {"fur": None, **earlier})
        self.assertIsNone(CommunityRooAvatar.objects.get(user=self.user).fur)
        chosen = self.patch({**empty_outfit(), "fur": "ironbark"}, 1)
        self.assertEqual(chosen.status_code, 200)
        kept = self.patch({**earlier, "glasses": "classic_black"}, 2)
        self.assertEqual(kept.status_code, 200)
        self.assertEqual(kept.data["version"], 3)
        self.assertEqual(
            kept.data["outfit"],
            {"fur": "ironbark", **earlier, "glasses": "classic_black"},
        )
        self.assertEqual(kept.data["effective_outfit"], kept.data["outfit"])
        row = CommunityRooAvatar.objects.get(user=self.user)
        self.assertEqual(
            (row.fur, row.glasses, row.version), ("ironbark", "classic_black", 3)
        )

    def test_unknown_fur_is_rejected_without_changing_the_saved_outfit(self):
        self.assertEqual(
            self.patch({**empty_outfit(), "fur": "golden_sand"}).status_code, 200
        )
        for fur in ["outback_red", "honey_tuft", 7]:
            result = self.patch({**empty_outfit(), "fur": fur}, 1)
            self.assertEqual(result.status_code, 400)
            self.assertEqual(result.data["error"], "invalid_roo_avatar")
            self.assertEqual(result.data["slots"], ["fur"])
        row = CommunityRooAvatar.objects.get(user=self.user)
        self.assertEqual((row.fur, row.version), ("golden_sand", 1))

    def test_large_spendable_balance_is_not_contribution_rank(self):
        PointsAccount.objects.create(
            user=self.user,
            purchased_topup_balance_microroo=500_000 * MICROROO,
            balance_microroo=500_000 * MICROROO,
        )
        result = self.client.get(self.url).data
        self.assertEqual(result["current_level"], 0)
        self.assertFalse(result["catalog"]["hair"][-1]["unlocked"])

    def test_invalid_fields_categories_and_body_identity_are_rejected(self):
        for extra in [
            {"account_id": "other"},
            {"community_id": "forged"},
            {"current_level": 6},
        ]:
            self.assertEqual(self.patch(empty_outfit(), **extra).status_code, 400)
        self.assertEqual(
            self.patch({**empty_outfit(), "hair": "classic_black"}).status_code, 400
        )
        self.assertEqual(self.patch({"hair": None}).status_code, 400)
        self.assertFalse(CommunityRooAvatar.objects.exists())

    def test_community_scope_is_server_owned_and_preferences_do_not_cross_it(self):
        self.assertEqual(
            self.patch({**empty_outfit(), "hair": "honey_tuft"}).status_code, 200
        )
        with override_settings(COMMUNITY_CHAT_VOLUNTEER_COMMUNITY="other-community"):
            result = self.client.get(self.url).data
        self.assertEqual(result["community_id"], "other-community")
        self.assertEqual(result["outfit"], empty_outfit())
        self.assertEqual(CommunityRooAvatar.objects.count(), 1)

    def test_non_account_credentials_cannot_read_or_save(self):
        for token in [
            "mlai_chat_bootstrap-only",
            "mlai_usage_reporter-only",
            "invalid",
        ]:
            self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {token}")
            self.assertEqual(self.client.get(self.url).status_code, 401)
            self.assertEqual(self.patch(empty_outfit()).status_code, 401)

    def test_cookie_writes_require_the_exact_bound_origin(self):
        from community_chat.account_cookies import ACCESS_COOKIE

        web = credentials_for(self.user, web=True)
        self.client.credentials()
        self.client.cookies[ACCESS_COOKIE] = web.access_token
        body = {"version": 0, "outfit": empty_outfit()}
        self.assertEqual(
            self.client.patch(self.url, body, format="json").status_code, 401
        )
        self.assertEqual(
            self.client.patch(
                self.url, body, format="json", HTTP_ORIGIN="https://wrong.example.test"
            ).status_code,
            401,
        )
        self.assertEqual(
            self.client.patch(
                self.url, body, format="json", HTTP_ORIGIN=ORIGIN
            ).status_code,
            200,
        )

    def test_revocation_rotation_and_account_deactivation_are_rechecked_in_service(
        self,
    ):
        old_session = self.credentials.session
        CommunityChatAccountSession.objects.filter(pk=old_session.pk).update(
            revoked_at=timezone.now()
        )
        with self.assertRaises(AuthenticationFailed):
            update_avatar(
                authenticated_session=old_session,
                values={"version": 0, "outfit": empty_outfit()},
            )
        self.assertFalse(CommunityRooAvatar.objects.exists())
        fresh = credentials_for(self.user)
        CommunityChatAccountSession.objects.filter(pk=fresh.session.pk).update(
            access_token_hash="f" * 64
        )
        with self.assertRaises(AuthenticationFailed):
            read_avatar(authenticated_session=fresh.session)
        fresh = credentials_for(self.user)
        get_user_model().objects.filter(pk=self.user.pk).update(is_active=False)
        with self.assertRaises(AuthenticationFailed):
            update_avatar(
                authenticated_session=fresh.session,
                values={"version": 0, "outfit": empty_outfit()},
            )

    def test_expired_session_is_rechecked_under_lock(self):
        CommunityChatAccountSession.objects.filter(
            pk=self.credentials.session.pk
        ).update(access_expires_at=timezone.now() - timedelta(seconds=1))
        with self.assertRaises(AuthenticationFailed):
            update_avatar(
                authenticated_session=self.credentials.session,
                values={"version": 0, "outfit": empty_outfit()},
            )


@override_settings(**SETTINGS)
class RooAvatarConcurrencyTests(TransactionTestCase):
    def test_two_devices_cannot_silently_overwrite_the_same_version(self):
        if connection.vendor != "postgresql":
            self.skipTest("Row-lock concurrency requires PostgreSQL")
        user = get_user_model().objects.create_user(
            email="concurrent-avatar@example.test"
        )
        creds = [credentials_for(user), credentials_for(user)]
        VolunteerMemberState.objects.create(
            user=user, community=COMMUNITY, historical_microroo=0
        )
        barrier = Barrier(2)

        def save(index):
            close_old_connections()
            try:
                barrier.wait(timeout=10)
                result = update_avatar(
                    authenticated_session=creds[index].session,
                    values={
                        "version": 0,
                        "outfit": {
                            **empty_outfit(),
                            "hair": "honey_tuft" if index else None,
                        },
                    },
                )
                return result["version"]
            except RooAvatarError as exc:
                return exc.code
            finally:
                close_old_connections()

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(save, range(2)))
        self.assertCountEqual(results, [1, "roo_avatar_version_conflict"])
        self.assertEqual(CommunityRooAvatar.objects.filter(user=user).count(), 1)
