"""Unified DM directory regressions with no database, migrations or network."""

from contextlib import nullcontext
from dataclasses import replace
from unittest.mock import patch

from django.core.cache import cache
from django.test import SimpleTestCase, override_settings

from integrations.services import slack_dm_directory as directory
from integrations.services import slack_dm_mirror as mirror
from integrations.services.slack_mentions import sanitized_directory_page


def person(user_id, name, **fields):
    return {"id": user_id, "team_id": "TMLAI", "name": name, **fields}


def snapshot(*people, version="a" * 32):
    return {
        "version": version,
        "users": sanitized_directory_page({"members": list(people)}, "TMLAI")["users"],
    }


@override_settings(
    CACHES={"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}}
)
class UnifiedDmDirectoryTests(SimpleTestCase):
    def setUp(self):
        cache.clear()
        self.authority = mirror._SlackGrantApiAuthority(
            grant_id=1,
            user_id=2,
            connection_id=3,
            consent_generation="consent",
            consent_version="v1",
            workspace_id="TMLAI",
            slack_user_id="UOWNER",
            oauth_generation=1,
            access_token="synthetic-test-token",
            scopes=tuple(mirror.DIRECT_DM_SCOPES),
        )
        patches = [
            patch.object(directory.transaction, "atomic", side_effect=nullcontext),
            patch.object(mirror, "_lock_slack_grant_api_authority"),
            patch.object(directory, "cached_workspace_snapshot", return_value=None),
            patch.object(mirror, "_call_slack_with_grant_authority"),
            patch.object(directory.CommunityBridgeIdentityLink, "objects"),
            patch.object(directory, "verified_identity_for_slack"),
        ]
        (
            self.atomic,
            self.validate,
            self.cached,
            self.read,
            self.links,
            self.identity,
        ) = [item.start() for item in patches]
        for item in patches:
            self.addCleanup(item.stop)
        self.links.filter.return_value.values_list.return_value = []

    def search(self, **options):
        return directory.search_dm_directory(self.authority, **options)

    def test_warm_snapshot_returns_people_without_slack_calls_or_private_fields(self):
        self.cached.return_value = snapshot(
            person("UOWNER", "Owner"),
            person("UBOT", "Bot", bot_id="B123"),
            person("UBOB", "Bob"),
            person("UALICE", "Alice", profile={"email": "secret@example.test"}),
        )
        result = self.search()
        self.assertEqual(
            [user["slack_user_id"] for user in result["users"]], ["UALICE", "UBOB"]
        )
        self.assertNotIn("secret@example.test", str(result))
        self.assertNotIn("search", result["users"][0])
        self.assertIsNone(result["users"][0]["pubkey"])
        self.read.assert_not_called()
        self.assertEqual(self.validate.call_count, 2)
        self.links.filter.assert_called_once()
        self.identity.assert_not_called()

    def test_links_resolve_again_after_revocation_and_are_never_cached(self):
        saved = snapshot(person("UALICE", "Alice"))
        self.cached.return_value = saved
        self.links.filter.return_value.values_list.return_value = ["UALICE"]
        self.identity.side_effect = [
            {"buzz_pubkey": "a" * 64, "user_profile_id": "profile-id"},
            None,
        ]
        self.assertEqual(self.search()["users"][0]["pubkey"], "a" * 64)
        self.assertIsNone(self.search()["users"][0]["pubkey"])
        self.assertNotIn("pubkey", saved["users"][0])
        self.assertEqual(self.identity.call_count, 2)

    def test_snapshot_paging_filters_first_and_keeps_original_version(self):
        first = snapshot(
            person("UOWNER", "Owner"),
            person("UBOT", "Bot", is_bot=True),
            person("UBOB", "Bob"),
            person("UCARL", "Carl"),
        )
        newer = snapshot(
            person("UAARON", "Aaron"),
            person("UBOB", "Bob"),
            person("UCARL", "Carl"),
            version="b" * 32,
        )
        current = [first]
        self.cached.side_effect = lambda workspace, version="": (
            first if version else current[0]
        )
        page = self.search(limit=1)
        self.assertEqual(page["users"][0]["slack_user_id"], "UBOB")
        current[0] = newer
        result = self.search(limit=1, cursor=page["next_cursor"])
        self.assertEqual(result["users"][0]["slack_user_id"], "UCARL")
        self.assertEqual(result["next_cursor"], "")
        self.cached.assert_called_with("TMLAI", version="a" * 32)
        self.read.assert_not_called()

    def test_name_search_uses_whole_snapshot_and_username(self):
        self.cached.return_value = snapshot(
            *[person(f"UP{i}", f"Person {i}") for i in range(100)],
            person("ULATER", "lookup", profile={"display_name": "Later Person"}),
        )
        result = self.search(query="lookup")
        self.assertEqual(result["users"][0]["slack_user_id"], "ULATER")
        self.read.assert_not_called()

    def test_fallback_pages_are_shared_between_searches_and_never_keep_links(self):
        self.read.return_value = {
            "members": [person("UALICE", "Alice"), person("UBOB", "Bob")]
        }
        self.assertEqual(
            self.search(query="ali")["users"][0]["slack_user_id"], "UALICE"
        )
        self.assertEqual(self.search(query="bob")["users"][0]["slack_user_id"], "UBOB")
        self.assertEqual(self.read.call_count, 1)
        self.assertEqual(self.links.filter.call_count, 2)

    def test_fallback_page_cursor_does_not_lose_users_on_first_slack_page(self):
        self.read.return_value = {
            "members": [person("UALICE", "Alice"), person("UBOB", "Bob")]
        }
        first = self.search(limit=1)
        self.assertTrue(first["next_cursor"])
        second = self.search(limit=1, cursor=first["next_cursor"])
        self.assertEqual(second["users"][0]["slack_user_id"], "UBOB")
        self.assertEqual(second["next_cursor"], "")
        self.assertEqual(self.read.call_count, 1)

    def test_fallback_follows_empty_matching_pages(self):
        self.read.side_effect = [
            {
                "members": [person("UALICE", "Alice")],
                "response_metadata": {"next_cursor": "second"},
            },
            {"members": [person("UBOB", "Bob")]},
        ]
        self.assertEqual(self.search(query="bob")["users"][0]["slack_user_id"], "UBOB")
        self.assertEqual(self.read.call_count, 2)
        self.assertEqual(self.read.call_args.kwargs["cursor"], "second")

    def test_fallback_consent_epoch_changes_do_not_reuse_owner_pages(self):
        self.read.return_value = {"members": [person("UALICE", "Alice")]}
        self.search()
        self.authority = replace(self.authority, oauth_generation=2)
        self.search()
        self.assertEqual(self.read.call_count, 2)

    def test_expired_snapshot_cursor_restarts_without_forwarding_internal_cursor(self):
        self.read.return_value = {"members": [person("UALICE", "Alice")]}
        cursor = mirror._encode_directory_cursor(f"snapshot-v1:{'a' * 32}:10", 0)
        self.search(cursor=cursor)
        self.assertEqual(self.read.call_args.kwargs["cursor"], "")

    def test_cached_directory_still_rechecks_owner_authority_before_return(self):
        self.cached.return_value = snapshot(person("UALICE", "Alice"))
        self.validate.side_effect = [
            None,
            mirror.SlackDmMirrorAuthorizationError("revoked"),
        ]
        with self.assertRaises(mirror.SlackDmMirrorAuthorizationError):
            self.search()
        self.read.assert_not_called()

    def test_revoked_owner_cannot_read_cached_names(self):
        self.validate.side_effect = mirror.SlackDmMirrorAuthorizationError("revoked")
        with self.assertRaises(mirror.SlackDmMirrorAuthorizationError):
            self.search()
        self.cached.assert_not_called()
        self.links.filter.assert_not_called()

    def test_malformed_snapshot_cursor_never_reaches_slack(self):
        cursor = mirror._encode_directory_cursor("snapshot-v1:invalid:1", 0)
        with self.assertRaises(mirror.SlackDmMirrorError):
            self.search(cursor=cursor)
        self.read.assert_not_called()
