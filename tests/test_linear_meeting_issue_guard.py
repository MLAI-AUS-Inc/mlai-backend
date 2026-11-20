from types import SimpleNamespace
from unittest.mock import Mock, patch

from django.test import SimpleTestCase

from integrations.api_views_connectors import LinearMeetingIssueCreateView
from integrations.services import linear_meeting_actions as actions


def row(key="S-1", title="Export signed invoices", description="Export only signed invoices.", **changes):
    return {
        "id": key, "identifier": key, "title": title, "description": description,
        "team": {"id": "studio"}, "updatedAt": "2024-05-12T00:00:00Z",
        "url": "https://linear.example/issue/" + key, **changes,
    }


def page(nodes, *, cursor=None, project="p"):
    return {
        "project": {"id": project}, "nodes": nodes,
        "pageInfo": {"hasNextPage": bool(cursor), "endCursor": cursor},
    }


class ReviewedMeetingIssueTests(SimpleTestCase):
    def setUp(self):
        self.payload = {
            "title": "Export signed invoices", "project_id": "p", "team_id": "studio",
            "project_work_outcome": "Export only signed invoices.",
            "project_work_snapshot_at": "2024-05-13T00:00:00Z",
            "description": "### Meeting action\nExport only signed invoices.",
            "recheck_project_work": True, "idempotency_key": "a" * 64,
        }
        self.inventory = self.enterContext(patch.object(actions, "get_linear_project_issue_page"))
        self.graphql_write = self.enterContext(patch.object(actions, "_graphql_write"))
        self.transport = SimpleNamespace(pk=1)
        self.claim = self.enterContext(patch.object(actions, "_claim_linear_issue_receipt"))
        self.claim.side_effect = lambda key, payload: (self.transport, None, payload)
        self.enterContext(patch.object(actions, "_enforce_project_sizing", return_value=None))
        self.enterContext(patch.object(actions.transaction, "atomic"))
        self.receipts = self.enterContext(patch.object(actions.LinearIssueCreationReceipt, "objects"))
        self.guard = SimpleNamespace(pk=2, status=actions.LinearIssueCreationReceipt.Status.PENDING)
        self.outcome_claim = self.receipts.select_for_update.return_value.get_or_create
        self.outcome_claim.return_value = self.guard, True

    def test_approval_links_closed_issue_from_later_page_and_completes_receipt(self):
        self.inventory.side_effect = [
            page([row("A-1", team={"id": "another-team"})], cursor="next"),
            page([row(state={"type": "completed"})]),
        ]
        matched = actions.create_linear_meeting_issue(self.payload)
        self.assertTrue(matched["alreadyTracked"])
        self.assertEqual(matched["identifier"], "S-1")
        self.assertEqual(self.inventory.call_args_list[1].kwargs["after"], "next")
        self.graphql_write.assert_not_called()
        stored = self.receipts.filter.return_value.update.call_args.kwargs
        self.assertEqual(stored["linear_issue_payload"], matched)

    def test_ambiguous_or_unread_inventory_stops_mutation_and_issue_view_returns_conflict(self):
        for pages in (
            [page([row("S-1"), row("S-2")])],
            [page([row(description="Export every invoice, including drafts.")])],
            [page([], cursor="loop"), page([], cursor="loop")],
            [page([], project="other")],
        ):
            with self.subTest(pages=pages):
                self.inventory.side_effect = pages
                result = LinearMeetingIssueCreateView().post(SimpleNamespace(data=self.payload))
                self.assertEqual(result.status_code, 409)
                self.assertEqual(result.data["code"], "linear_meeting_action_conflict")
        self.graphql_write.assert_not_called()

    def test_reviewed_distinct_scope_is_honoured_until_the_issue_changes(self):
        payload = {**self.payload, "project_work_distinct_issue_id": "S-1"}
        different = row(description="Export billing contact details.")
        self.inventory.return_value = page([different])
        self.graphql_write.return_value = {"issueCreate": {"success": True, "issue": row("S-2")}}
        self.assertEqual(actions.create_linear_meeting_issue(payload)["id"], "S-2")
        marker = self.graphql_write.call_args.args[1]["input"]["description"].splitlines()[-1]
        self.assertRegex(marker, r"^<!-- roo-meeting-work:[a-f0-9]{64} -->$")
        self.inventory.return_value = page([{**different, "updatedAt": "2024-05-14T00:00:00Z"}])
        with self.assertRaises(actions.LinearMeetingActionConflictError):
            actions.create_linear_meeting_issue(payload)
        self.graphql_write.assert_called_once()

    def test_later_similar_work_is_held_before_create(self):
        self.inventory.return_value = page([
            row(title="Build export signed invoices", updatedAt="2024-05-14T00:00:00Z")
        ])
        with self.assertRaises(actions.LinearMeetingActionConflictError):
            actions.create_linear_meeting_issue(self.payload)
        self.graphql_write.assert_not_called()

    def test_parallel_rephrasing_cannot_read_an_outdated_project_inventory(self):
        self.inventory.return_value = page([])
        other = {**self.payload, "title": "Download signed invoices", "idempotency_key": "b" * 64}

        def create_first(*args, **kwargs):
            with self.assertRaisesMessage(actions.LinearMeetingActionConflictError, "Another meeting action"):
                actions.create_linear_meeting_issue(other)
            return {"issueCreate": {"success": True, "issue": row()}}

        self.graphql_write.side_effect = create_first
        self.assertEqual(actions.create_linear_meeting_issue(self.payload)["id"], "S-1")
        self.inventory.assert_called_once()
        self.graphql_write.assert_called_once()

    def test_existing_meeting_acceptance_criteria_are_compared_as_part_of_outcome(self):
        self.inventory.return_value = page([row(description=(
            "### Meeting action\nExport only signed invoices.\n\n### Acceptance criteria\n"
            "- Include PDF.\n- Keep page order.\n\n### Evidence\nA meeting quote."
        ))])
        payload = {**self.payload, "project_work_outcome": "Export only signed invoices.\nInclude PDF.\nKeep page order."}
        self.assertTrue(actions.create_linear_meeting_issue(payload)["alreadyTracked"])
        self.graphql_write.assert_not_called()

    def test_lost_success_never_sends_again_and_can_reconcile_a_renamed_issue(self):
        self.inventory.return_value = page([])
        self.graphql_write.side_effect = actions.LinearChannelIssueWriteUncertainError("read timed out")
        with self.assertRaises(actions.LinearMeetingIdempotencyConflictError):
            actions.create_linear_meeting_issue(self.payload)
        marker = self.graphql_write.call_args.args[1]["input"]["description"].splitlines()[-1]
        first_guard_key = self.outcome_claim.call_args.kwargs["idempotency_key"]
        self.outcome_claim.return_value = self.guard, False
        retry = {**self.payload, "idempotency_key": "b" * 64}
        with self.assertRaises(actions.LinearMeetingIdempotencyConflictError):
            actions.create_linear_meeting_issue(retry)
        self.assertEqual(first_guard_key, self.outcome_claim.call_args.kwargs["idempotency_key"])
        self.inventory.return_value = page([row(title="Download reconciled invoices", description=marker)])
        self.assertTrue(actions.create_linear_meeting_issue(retry)["alreadyTracked"])
        self.graphql_write.assert_called_once()

    def test_legacy_issue_creation_retains_original_contract(self):
        legacy = {"team_id": "studio", "title": "Add a fixture"}
        result = {"issueCreate": {"success": True, "issue": {"id": "S-7"}}}
        with patch.object(actions, "_graphql", return_value=result) as old_writer:
            self.assertEqual(actions.create_linear_meeting_issue(legacy), {"id": "S-7"})
        self.assertEqual(old_writer.call_args.args[1]["input"], {"teamId": "studio", "title": "Add a fixture"})
        self.inventory.assert_not_called()
        self.graphql_write.assert_not_called()

    def test_batch_counts_distinguish_created_from_linked(self):
        def item(key, issue):
            return SimpleNamespace(id=key, position=0, status="approved", display={}, reason="", last_error="",
                                   linear_issue_payload=issue)

        batch = Mock()
        batch.items.all.return_value = [item("1", {"id": "S-1"}), item("2", {"id": "S-2", "alreadyTracked": True})]
        counts = actions._serialize_linear_meeting_action_batch(batch)["counts"]
        self.assertEqual((counts["approved"], counts["created"], counts["alreadyTracked"]), (2, 1, 1))


class ProjectInventoryValidationTests(SimpleTestCase):
    def test_issue_and_update_pages_reject_partial_or_mismatched_connections(self):
        readers = (("issues", actions.get_linear_project_issue_page), ("projectUpdates", actions.get_linear_project_update_page))
        valid = {"nodes": [], "pageInfo": {"hasNextPage": False}}
        for field, read in readers:
            with self.subTest(field=field), patch.object(actions, "_graphql") as get:
                for project in (
                    {"id": "other", field: valid}, {"id": "p"},
                    {"id": "p", field: {"pageInfo": {"hasNextPage": False}}},
                    {"id": "p", field: {"nodes": ["bad"], "pageInfo": {"hasNextPage": False}}},
                    {"id": "p", field: {"nodes": [], "pageInfo": {"hasNextPage": None}}},
                    {"id": "p", field: {"nodes": [], "pageInfo": {"hasNextPage": True}}},
                ):
                    get.return_value = {"project": project}
                    with self.assertRaises(actions.LinearMeetingGraphQLError):
                        read("p")
                get.return_value = {"project": {"id": "p", field: valid}}
                self.assertEqual(read("p")["nodes"], [])
