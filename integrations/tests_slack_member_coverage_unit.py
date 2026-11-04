"""Synthetic, database-free fixtures for the read-only coverage report."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json
import unittest
import uuid

from integrations.services.slack_member_coverage import (
    REQUIRED_SCOPES, build_coverage,
)

NOW = datetime(2026, 10, 21, 1, 0, tzinfo=timezone.utc)


def grant_fixture(grant_id=1, *, workspace="TTEST", slack_user="UONE", user_id=10):
    scopes = list(REQUIRED_SCOPES)
    identity = {"grant_id": grant_id, "connection_id": grant_id + 100,
                "workspace_id": workspace, "slack_user_id": slack_user,
                "consent_generation": NOW.isoformat(), "consent_version": "existing-consent",
                "oauth_generation": 0, "scopes": scopes}
    return {"id": grant_id, "user_id": user_id, "slack_workspace_id": workspace,
            "slack_user_id": slack_user, "status": "active", "revoked_at": None,
            "user_active": True, "connection_id": grant_id + 100,
            "connection_user_id": user_id, "provider": "slack", "connection_status": "connected",
            "connection_workspace_id": workspace, "connection_slack_user_id": slack_user,
            "scopes": scopes, "consented_at": NOW, "consent_version": "existing-consent",
            "inventory_consent": identity, "inventory_state": {
                **identity, "coverage": {"public_channel": "complete"},
                "public_sweep": "", "public_cursor": "", "last_public_at": NOW.isoformat()}}


def link_fixture(grant):
    return {"user_id": grant["user_id"], "slack_workspace_id": grant["slack_workspace_id"],
            "slack_user_id": grant["slack_user_id"], "revoked_at": None}


def channel_fixture(channel_id="CONE", **overrides):
    return {"slack_workspace_id": "TTEST", "slack_channel_id": channel_id,
            "enabled": True, "destination_platform": "buzz", "destination_channel_id": "room-one",
            "lifecycle_source": "", "slack_visibility": "public", "mirror_decision": "mirror", **overrides}


def inventory_fixture(grant_id=1, channel_id="CONE", **overrides):
    return {"grant_id": grant_id, "slack_conversation_id": channel_id, "kind": "public_channel",
            "eligibility": "eligible", "source_archived": False, "last_seen_at": NOW,
            "last_seen_sweep_id": "old-sweep", **overrides}


class SlackMemberCoverageUnitTests(unittest.TestCase):
    def report(self, *, grants=None, links=None, channels=None, inventory=None):
        grants = [grant_fixture()] if grants is None else grants
        return build_coverage(workspace_id="TTEST", now=NOW, grants=grants,
                              identity_links=[link_fixture(row) for row in grants] if links is None else links,
                              channels=[channel_fixture()] if channels is None else channels,
                              inventory=[inventory_fixture()] if inventory is None else inventory)

    def test_scope_qualified_linked_members_and_zero_coverage_channels(self):
        second = grant_fixture(2, slack_user="UTWO", user_id=20)
        result = self.report(grants=[grant_fixture(), second], channels=[channel_fixture(), channel_fixture("CTWO")],
                             inventory=[inventory_fixture(), inventory_fixture(2)])
        self.assertEqual(result["scope_qualified_linked_member_count"], 2)
        self.assertEqual([row["observed_member_count"] for row in result["channels"]], [2, 0])
        self.assertEqual(result["inventory_gaps"], {})
        self.assertIn("not_live_membership", result["evidence"])

    def test_inventory_duplicates_cannot_inflate_membership_count(self):
        result = self.report(inventory=[inventory_fixture(), inventory_fixture(), inventory_fixture(kind="private_channel")])
        self.assertEqual(result["channels"][0]["observed_member_count"], 1)

    def test_duplicate_slack_grants_fail_closed(self):
        first, duplicate = grant_fixture(), grant_fixture(2, user_id=20)
        result = self.report(grants=[first, duplicate])
        self.assertEqual(result["scope_qualified_linked_member_count"], 0)
        self.assertEqual(len(result["excluded_grants"]), 2)
        self.assertEqual(result["channels"][0]["observed_member_count"], 0)

    def test_other_workspaces_and_non_community_mappings_are_not_mixed(self):
        foreign = grant_fixture(2, workspace="TOTHER", slack_user="UTWO")
        result = self.report(grants=[grant_fixture(), foreign],
                             channels=[channel_fixture(), channel_fixture("COTHER", slack_workspace_id="TOTHER"),
                                       channel_fixture("CDISABLED", enabled=False),
                                       channel_fixture("CDISCORD", destination_platform="discord")],
                             inventory=[inventory_fixture(), inventory_fixture(2)])
        self.assertEqual(result["scope_qualified_linked_member_count"], 1)
        self.assertEqual([row["slack_channel_id"] for row in result["channels"]], ["CONE"])
        self.assertEqual(result["channels"][0]["observed_member_count"], 1)

    def test_unexpected_enabled_mappings_are_visible(self):
        result = self.report(channels=[channel_fixture(), channel_fixture("CNEW", lifecycle_source="event"),
                                      channel_fixture("CPRIVATE", slack_visibility="private"),
                                      channel_fixture("CHOLD", mirror_decision="hold"),
                                      channel_fixture("CUNMAPPED", destination_channel_id="")])
        self.assertEqual(len(result["channels"]), 1)
        self.assertEqual(len(result["unexpected_enabled_mappings"]), 4)

    def test_grant_link_account_and_connection_fences(self):
        for field, value in [("status", "paused"), ("revoked_at", NOW), ("user_active", False),
                             ("connection_user_id", 99), ("provider", "github"),
                             ("connection_status", "disconnected"), ("connection_workspace_id", "TOTHER"),
                             ("connection_slack_user_id", "UOTHER")]:
            with self.subTest(field=field):
                row = grant_fixture()
                row[field] = value
                self.assertEqual(self.report(grants=[row])["scope_qualified_linked_member_count"], 0)
        self.assertEqual(self.report(links=[])["scope_qualified_linked_member_count"], 0)
        link = link_fixture(grant_fixture())
        link["revoked_at"] = NOW
        self.assertEqual(self.report(links=[link])["scope_qualified_linked_member_count"], 0)
        self.assertEqual(self.report(grants=[{**grant_fixture(), "connection_status": "syncing"}])
                         ["scope_qualified_linked_member_count"], 1)

    def test_missing_malformed_and_mixed_scope_values(self):
        for scopes, absent, invalid in [(["chat:write"], ["channels:history", "channels:read"], False),
                                        ("channels:read,channels:history,chat:write", list(REQUIRED_SCOPES), True),
                                        (None, list(REQUIRED_SCOPES), True),
                                        ([*REQUIRED_SCOPES, None, {}], [], True)]:
            with self.subTest(scopes=scopes):
                result = self.report(grants=[{**grant_fixture(), "scopes": scopes}])
                self.assertEqual(result["scope_qualified_linked_member_count"], 0)
                self.assertEqual(result["members_missing_scopes"][0]["missing_scopes"], absent)
                self.assertEqual(result["members_missing_scopes"][0]["invalid_scopes"], invalid)

    def test_scope_whitespace_and_duplicates_are_normalized(self):
        row = grant_fixture()
        row["scopes"] = [" chat:write ", "channels:read", "channels:history", "chat:write"]
        self.assertEqual(self.report(grants=[row])["channels"][0]["observed_member_count"], 1)

    def test_old_metadata_consent_or_inventory_epoch_is_not_membership_evidence(self):
        for field in ["inventory_consent", "inventory_state"]:
            with self.subTest(field=field):
                row = deepcopy(grant_fixture())
                row[field]["oauth_generation"] = 5
                result = self.report(grants=[row])
                self.assertEqual(result["scope_qualified_linked_member_count"], 1)
                self.assertEqual(result["channels"][0]["observed_member_count"], 0)
                self.assertEqual(result["inventory_gaps"], {"not_current_or_not_consented": 1})

    def test_partial_sweep_counts_only_current_fresh_observations(self):
        row = deepcopy(grant_fixture())
        row["inventory_state"].update(public_sweep="a" * 32, public_cursor="next")
        result = self.report(grants=[row])
        self.assertEqual(result["inventory_gaps"], {"partial": 1})
        self.assertEqual(result["channels"][0]["observed_member_count"], 0)
        self.assertEqual(result["channels"][0]["excluded_observations"], {"previous_sweep": 1})
        result = self.report(grants=[row], inventory=[inventory_fixture(last_seen_sweep_id=uuid.UUID("a" * 32))])
        self.assertEqual(result["channels"][0]["observed_member_count"], 1)
        self.assertEqual(result["inventory_gaps"], {"partial": 1})

    def test_stale_future_external_archived_and_permission_limited_rows_do_not_count(self):
        for override in [{"last_seen_at": NOW - timedelta(days=2)}, {"last_seen_at": NOW + timedelta(seconds=1)},
                         {"last_seen_at": None}, {"eligibility": "unsupported_external"},
                         {"eligibility": "permission_limited"}, {"source_archived": True}]:
            with self.subTest(override=override):
                result = self.report(inventory=[inventory_fixture(**override)])
                self.assertEqual(result["channels"][0]["observed_member_count"], 0)
                self.assertTrue(result["channels"][0]["excluded_observations"])

    def test_stale_pending_and_permission_required_sweeps_are_disclosed(self):
        for state, expected in [({"last_public_at": (NOW - timedelta(days=2)).isoformat()}, "stale"),
                                ({"coverage": {"public_channel": "pending"}}, "pending"),
                                ({"coverage": {"public_channel": "permission_required"}}, "permission_required")]:
            with self.subTest(expected=expected):
                row = deepcopy(grant_fixture())
                row["inventory_state"].update(state)
                result = self.report(grants=[row])
                self.assertEqual(result["inventory_gaps"], {expected: 1})
                self.assertEqual(result["channels"][0]["observed_member_count"], 0)

    def test_output_allowlist_omits_untrusted_content_and_credentials(self):
        row = grant_fixture()
        row.update(access_token="synthetic-secret-sentinel", refresh_token="synthetic-secret-sentinel",
                   account_label="private-name-sentinel")
        inventory = inventory_fixture(source_name="private-channel-name-sentinel", text="private-body-sentinel")
        result = json.dumps(self.report(grants=[row], inventory=[inventory]))
        for sentinel in ["synthetic-secret-sentinel", "private-name-sentinel", "private-channel-name-sentinel", "private-body-sentinel"]:
            self.assertNotIn(sentinel, result)


class SlackMemberCoverageCommandUnitTests(unittest.TestCase):
    def test_command_validates_workspace_and_age_before_any_query(self):
        from django.core.management import call_command, CommandError
        from io import StringIO
        from unittest.mock import patch
        for workspace, age in [("", 24), ("TTEST,TOTHER", 24), ("TTEST", 0), ("TTEST", -1),
                               ("TTEST", float("nan")), ("TTEST", float("inf")), ("TTEST", 8761)]:
            with self.subTest(workspace=workspace, age=age), patch(
                "integrations.management.commands.slack_member_coverage.load_coverage",
            ) as load:
                with self.assertRaises(CommandError):
                    call_command("slack_member_coverage", workspace=workspace, max_age_hours=age, stdout=StringIO())
                load.assert_not_called()

    def test_command_emits_only_report_json(self):
        from django.core.management import call_command
        from io import StringIO
        from unittest.mock import patch
        output = StringIO()
        with patch("integrations.management.commands.slack_member_coverage.load_coverage", return_value={"safe": True}) as load:
            call_command("slack_member_coverage", workspace=" TTEST ", max_age_hours=6, stdout=output)
        self.assertEqual(json.loads(output.getvalue()), {"safe": True})
        self.assertEqual(load.call_args.kwargs["workspace_id"], "TTEST")
        self.assertEqual(load.call_args.kwargs["max_age_hours"], 6)

    def test_malformed_sweep_cannot_reuse_inventory(self):
        row = deepcopy(grant_fixture())
        row["inventory_state"]["public_sweep"] = "not-a-uuid"
        result = build_coverage(workspace_id="TTEST", now=NOW, grants=[row], identity_links=[link_fixture(row)],
                                channels=[channel_fixture()], inventory=[inventory_fixture(last_seen_sweep_id="not-a-uuid")])
        self.assertEqual(result["inventory_gaps"], {"invalid_sweep": 1})
        self.assertEqual(result["channels"][0]["observed_member_count"], 0)

    def test_public_cursor_without_sweep_cannot_reuse_prior_inventory(self):
        row = deepcopy(grant_fixture())
        row["inventory_state"].update(public_cursor="next-page", public_sweep="")
        result = build_coverage(workspace_id="TTEST", now=NOW, grants=[row], identity_links=[link_fixture(row)],
                                channels=[channel_fixture()], inventory=[inventory_fixture()])
        self.assertEqual(result["inventory_gaps"], {"invalid_sweep": 1})
        self.assertEqual(result["channels"][0]["observed_member_count"], 0)
