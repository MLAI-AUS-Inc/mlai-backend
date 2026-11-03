"""Actual account HTTP/SQL proofs for finite v2 catalogue command receipts."""
from copy import deepcopy
from contextlib import ExitStack
from datetime import datetime, timezone as dt_timezone
import hashlib
import json
import os
from pathlib import Path
from threading import Barrier, Thread
from types import SimpleNamespace
from unittest.mock import patch
from uuid import uuid4

from django.core.cache import cache
from django.db import connection, connections, transaction
from django.test import TransactionTestCase, override_settings
from rest_framework.test import APIClient

from community_chat.account_sessions import issue_account_session
from community_chat.models import CommunityMemberProfile
from core.models import User
from founder_tools import startup_team
from founder_tools.models import VibeRaisingCompany
from founder_tools.services import get_founder_company_context
from roo.models import Ledger
from .catalog_commands import KEY, MAX_COMMANDS
from .editorial_catalog import CATALOG_KEY, merge_strategy, public_strategy, review_payload
from .editorial_views import mutate_catalog_response
from .founder_journey_contract import FounderContractMismatch, _validate
from .founder_journey_testing import ALL_P1_FLAGS_ON
from .models import OrganizationContentConfig
from .self_heal_testing import make_p2_org


def bearer_client(user):
    CommunityMemberProfile.objects.update_or_create(user=user, defaults={"status": "approved"})
    credential = issue_account_session(user, SimpleNamespace(client_id="mlai-chat-ios", installation_id=uuid4(),
        origin="mlaichat://callback", platform="ios", device_name="Synthetic catalogue regression",
        public_key=hashlib.sha256(f"catalogue-regression:{user.pk}".encode()).hexdigest()))
    client = APIClient()
    client.credentials(HTTP_AUTHORIZATION=f"Bearer {credential.access_token}")
    return client


def wire_rule(node):
    if isinstance(node, list): return [wire_rule(item) for item in node]
    if not isinstance(node, dict): return node
    result = {key: wire_rule(value) for key, value in node.items() if key not in {"properties", "required"}}
    if "properties" in node:
        names = {key: value.get("x-wire-name", key) for key, value in node["properties"].items()}
        result["properties"] = {names[key]: wire_rule(value) for key, value in node["properties"].items()}
        if "required" in node: result["required"] = [names[key] for key in node["required"]]
    return result


def projection(value, rule, definitions):
    if "$ref" in rule: return projection(value, definitions[rule["$ref"][8:]], definitions)
    if isinstance(value, dict) and rule.get("x-wire-project") is True:
        return {key: projection(item, rule["properties"][key], definitions)
            for key, item in value.items() if key in rule.get("properties", {})}
    if isinstance(value, list) and "items" in rule:
        return [projection(item, rule["items"], definitions) for item in value]
    if isinstance(value, dict):
        return {key: projection(item, rule.get("properties", {}).get(key, {}), definitions) for key, item in value.items()}
    return value


@override_settings(**ALL_P1_FLAGS_ON)
class CatalogCommandTests(TransactionTestCase):
    def setUp(self):
        cache.clear()
        built = make_p2_org(domain="catalog-command.example")
        self.org, self.user, self.company = built.organization, built.owner, built.company
        self.client = bearer_client(self.user)
        self.foreign_built = make_p2_org(domain="foreign-catalog-command.example")
        self.foreign = self.foreign_built.owner
        self.base = "/api/v1/my-startup/vibe-marketing/editorial-catalog/"

    def url(self, *, action="edit", company=None):
        suffix = {"edit": "", "approve": "approve/", "first_audience": "first-audience/"}[action]
        return f"{self.base}{suffix}?company_id={(company or self.company).pk}"

    def send(self, body, *, key="catalog-command-one", action="edit", company=None, client=None):
        headers = {"HTTP_IDEMPOTENCY_KEY": key} if key is not None else {}
        return getattr(client or self.client, "put" if action == "edit" else "post")(
            self.url(action=action, company=company), body, format="json", **headers)

    def current(self):
        response = self.client.get(self.url())
        self.assertEqual(response.status_code, 200, response.data)
        return response.data

    def strategy(self):
        return deepcopy(OrganizationContentConfig.objects.get(organization=self.org).pillar_strategy)

    def initial(self):
        return {"expected_editorial_catalog_version": self.current()["editorial_catalog_version"],
            "audience_options": [{"id": "BUILDERS", "reader_task": "Show tested work", "status": "draft"}]}

    def assert_no_spend(self): self.assertFalse(Ledger.objects.filter(kind="SPEND").exists())

    def test_edit_lost_ack_and_cache_loss_replay_current_data_without_second_effect(self):
        body = self.initial()
        first = self.send(body)
        self.assertEqual(first.status_code, 200, first.data)
        before = self.strategy()
        cache.clear()
        with patch("requests.sessions.Session.request", side_effect=AssertionError("Catalogue replay cannot call providers")):
            replay = self.send(body)
        self.assertEqual(replay.status_code, 200, replay.data)
        self.assertEqual(replay.data, first.data)
        self.assertEqual(replay["Idempotent-Replayed"], "true")
        self.assertEqual(self.strategy(), before)
        self.assertEqual(len(before[KEY]), 1)
        receipt = next(iter(before[KEY].values()))
        self.assertEqual(receipt["catalog_version"], 1)
        self.assertNotIn("audience_options", receipt)
        self.assert_no_spend()

    def test_approval_replay_preserves_original_receipt_revision_and_history(self):
        seeded = self.send(self.initial(), key=None)
        self.assertEqual(seeded.status_code, 200, seeded.data)
        body = {"expected_editorial_catalog_version": seeded.data["editorial_catalog_version"],
            "entries": seeded.data["review_entries"]}
        first = self.send(body, action="approve")
        self.assertEqual(first.status_code, 200, first.data)
        before = self.strategy()
        replay = self.send(body, action="approve")
        self.assertEqual(replay.status_code, 200, replay.data)
        self.assertEqual(replay.data, first.data)
        self.assertEqual(self.strategy(), before)
        self.assertEqual(len(before[CATALOG_KEY]["approval_history"]), 1)
        self.assert_no_spend()

    def test_same_key_changed_body_action_and_same_org_company_are_closed_conflicts(self):
        body = self.initial()
        first = self.send(body); self.assertEqual(first.status_code, 200, first.data)
        before = self.strategy()
        changed = deepcopy(body); changed["audience_options"][0]["reader_task"] = "Different request"
        self.assertEqual(self.send(changed).status_code, 409)
        self.assertEqual(self.send({"expected_editorial_catalog_version": 1, "entries": first.data["review_entries"]}, action="approve").status_code, 409)
        alias = VibeRaisingCompany.objects.create(profile=self.company.profile, organization=self.org,
            domain=None, name="Same owner's alternate company selector")
        self.assertEqual(self.send(body, company=alias).status_code, 409)
        self.assertEqual(self.strategy(), before)

    def test_foreign_actor_is_hidden_and_changed_current_principal_cannot_replay_previous_owner(self):
        body = self.initial()
        self.assertEqual(self.send(body).status_code, 200)
        before = self.strategy()
        foreign = bearer_client(self.foreign)
        self.assertEqual(self.send(body, client=foreign).status_code, 404)
        VibeRaisingCompany.objects.filter(pk=self.company.pk).update(profile=self.foreign_built.company.profile)
        replay = self.send(body, client=foreign)
        self.assertEqual(replay.status_code, 403, replay.data)
        self.assertEqual(self.strategy(), before)

    def test_independently_owned_organization_has_its_own_key_namespace(self):
        body = self.initial(); self.assertEqual(self.send(body).status_code, 200)
        foreign = bearer_client(self.foreign)
        result = self.send({"expected_editorial_catalog_version": 0,
            "audience_options": [{"id": "OTHER", "reader_task": "Own independent intent"}]},
            company=self.foreign_built.company, client=foreign)
        self.assertEqual(result.status_code, 200, result.data)
        self.assertIn(KEY, OrganizationContentConfig.objects.get(organization=self.foreign_built.organization).pillar_strategy)

    def test_frozen_or_malformed_roster_never_replays_or_mutates_receipt(self):
        body = self.initial(); self.assertEqual(self.send(body).status_code, 200)
        with startup_team._locked(self.org, create=True) as (config, roster):
            roster["frozen"] = True
            startup_team._save(config, roster)
        before = self.strategy()
        replay = self.send(body)
        self.assertEqual(replay.status_code, 403, replay.data)
        self.assertEqual(self.strategy(), before)
        config = OrganizationContentConfig.objects.get(organization=self.org)
        config.pillar_strategy = {**config.pillar_strategy, startup_team.KEY: {"version": "malformed"}}
        config.save(update_fields=["pillar_strategy"])
        corrupt = self.strategy()
        self.assertIn(self.send(body).status_code, (403, 404))
        self.assertEqual(self.strategy(), corrupt)

    def test_real_account_deactivation_and_stale_context_are_both_refused(self):
        body = self.initial(); self.assertEqual(self.send(body).status_code, 200)
        context = get_founder_company_context(self.user, company_id=self.company.pk)
        before = self.strategy()
        User.objects.filter(pk=self.user.pk).update(is_active=False)
        self.assertEqual(self.send(body).status_code, 401)
        result = mutate_catalog_response({"pk": self.org.pk}, body, owner_context=context,
            owner_user_id=self.user.pk, command_key="catalog-command-one", command_body=body)
        self.assertEqual(result.status_code, 403, result.data)
        self.assertEqual(self.strategy(), before)

    def test_concurrent_same_command_has_one_revision_and_one_private_receipt(self):
        body = self.initial(); barrier = Barrier(2); results, errors = [], []
        def worker():
            try:
                client = bearer_client(User.objects.get(pk=self.user.pk))
                barrier.wait(timeout=10)
                result = self.send(body, client=client)
                results.append((result.status_code, result.data))
            except Exception as exc: errors.append(repr(exc))
            finally: connections.close_all()
        threads = [Thread(target=worker) for _ in range(2)]
        for thread in threads: thread.start()
        for thread in threads: thread.join(timeout=30)
        self.assertFalse(any(thread.is_alive() for thread in threads))
        self.assertEqual(errors, [])
        self.assertEqual([row[0] for row in results], [200, 200], results)
        self.assertEqual(results[0][1], results[1][1])
        self.assertEqual(self.current()["editorial_catalog_version"], 1)
        self.assertEqual(len(self.strategy()[KEY]), 1)

    def test_catalog_and_command_rollback_together_then_same_key_can_commit_once(self):
        body = self.initial(); before = self.strategy()
        with self.assertRaisesRegex(RuntimeError, "Synthetic process rollback"):
            with transaction.atomic():
                self.assertEqual(self.send(body).status_code, 200)
                self.assertIn(KEY, self.strategy())
                raise RuntimeError("Synthetic process rollback")
        self.assertEqual(self.strategy(), before)
        self.assertEqual(self.send(body).status_code, 200)
        self.assertEqual(len(self.strategy()[KEY]), 1)

    def test_replay_returns_current_authorized_projection_without_reapplying_old_body(self):
        original = self.initial(); first = self.send(original)
        changed = {"expected_editorial_catalog_version": 1,
            "audience_options": [{**first.data["audience_options"][0], "reader_task": "A newer owner-approved save", "version": 2}]}
        current = self.send(changed, key="new-intent"); self.assertEqual(current.status_code, 200, current.data)
        before = self.strategy()
        replay = self.send(original)
        self.assertEqual(replay.status_code, 200, replay.data)
        self.assertEqual(replay.data, current.data)
        self.assertEqual(replay["X-Catalog-Command-Revision"], "1")
        self.assertEqual(self.strategy(), before)

    def test_completed_receipt_survives_flag_off_and_generated_strategy_replacement(self):
        body = self.initial(); self.assertEqual(self.send(body).status_code, 200)
        original = self.strategy(); command = deepcopy(original[KEY])
        merged = merge_strategy(original, {"pillars": ["new generated pillar"], KEY: {"forged": True}})
        self.assertEqual(merged[KEY], command)
        self.assertNotIn(KEY, public_strategy(merged))
        self.assertEqual(public_strategy(merged), {"pillars": ["new generated pillar"], CATALOG_KEY: original[CATALOG_KEY]})
        with override_settings(MY_STARTUP_FOUNDER_JOURNEY_V2_ENABLED=False):
            replay = self.send(body)
            new = self.send({**body, "expected_editorial_catalog_version": 1}, key="new-disabled-command")
            self.assertEqual(new.status_code, 409, new.data)
        self.assertEqual(replay.status_code, 200, replay.data)
        self.assertEqual(self.strategy(), original)

    def test_invalid_receipt_or_same_revision_content_drift_fails_closed_without_repair(self):
        body = self.initial(); self.assertEqual(self.send(body).status_code, 200)
        original = self.strategy(); config = OrganizationContentConfig.objects.get(organization=self.org)
        for corrupt in ("malformed", {"a" * 64: {"schema_version": True}}, {"a" * 64: {"action": []}}):
            value = {**original, KEY: corrupt}
            config.pillar_strategy = value; config.save(update_fields=["pillar_strategy"])
            self.assertEqual(self.send(body).status_code, 409)
            self.assertEqual(self.strategy(), value)
        value = deepcopy(original)
        value[CATALOG_KEY]["audience_options"][0]["reader_task"] = "Corrupt unversioned write"
        config.pillar_strategy = value; config.save(update_fields=["pillar_strategy"])
        self.assertEqual(self.send(body).status_code, 409)
        self.assertEqual(self.strategy(), value)

    def test_capacity_holds_new_commands_without_evicting_old_replay(self):
        body = self.initial(); self.assertEqual(self.send(body).status_code, 200)
        value = self.strategy(); one = next(iter(value[KEY].values()))
        records = deepcopy(value[KEY])
        for index in range(MAX_COMMANDS - 1): records[hashlib.sha256(f"fixture-capacity:{index}".encode()).hexdigest()] = deepcopy(one)
        value[KEY] = records
        config = OrganizationContentConfig.objects.get(organization=self.org)
        config.pillar_strategy = value; config.save(update_fields=["pillar_strategy"])
        self.assertEqual(self.send(body).status_code, 200)
        self.assertEqual(self.send({**body, "expected_editorial_catalog_version": 1}, key="new-after-capacity").status_code, 409)
        self.assertEqual(self.strategy(), value)

    def test_keyless_legacy_and_service_catalog_semantics_remain_unchanged(self):
        body = self.initial()
        self.assertEqual(self.send(body, key=None).status_code, 200)
        self.assertEqual(self.send(body, key=None).status_code, 409)
        self.assertNotIn(KEY, self.strategy())
        with override_settings(MY_STARTUP_FOUNDER_JOURNEY_V2_ENABLED=False):
            current = self.current()
            value = self.send({"expected_editorial_catalog_version": current["editorial_catalog_version"],
                "audience_options": current["audience_options"]}, key="old-client-key")
        self.assertEqual(value.status_code, 200, value.data)
        self.assertNotIn(KEY, self.strategy())
        result = mutate_catalog_response({"pk": self.org.pk}, {"expected_editorial_catalog_version": 1,
            "audience_options": self.current()["audience_options"]})
        self.assertEqual(result.status_code, 200, result.data)
        self.assertNotIn(KEY, self.strategy())

    def test_first_audience_replays_exact_selection_and_conflicting_body_is_not_adopted(self):
        body = {"description": "Australian founders validating a practical pilot", "expected_editorial_catalog_version": 0}
        first = self.send(body, action="first_audience")
        self.assertEqual(first.status_code, 201, first.data)
        before = self.strategy()
        replay = self.send(body, action="first_audience")
        self.assertEqual(replay.status_code, 200, replay.data)
        self.assertEqual(first.data, replay.data)
        self.assertEqual(self.strategy(), before)
        changed = self.send({**body, "description": "Different people under an old command"}, action="first_audience")
        self.assertEqual(changed.status_code, 409)
        self.assertEqual(self.strategy(), before)
        self.assert_no_spend()
        target = os.environ.get("FOUNDER_EXPORT_CATALOG_COMMANDS")
        if target:
            self._export_case(target, "first_audience", body, first, replay)

    def test_first_audience_new_command_does_not_ack_an_unrequested_existing_audience(self):
        body = {"description": "Australian founders validating a practical pilot", "expected_editorial_catalog_version": 0}
        self.assertEqual(self.send(body, action="first_audience").status_code, 201)
        before = self.strategy()
        different = self.send({"description": "An unrelated requested audience", "expected_editorial_catalog_version": 1},
            action="first_audience", key="new-first-audience-intent")
        self.assertEqual(different.status_code, 409, different.data)
        self.assertEqual(self.strategy(), before)

    def test_first_audience_current_catalog_can_advance_but_selected_definition_cannot_be_replaced(self):
        body = {"description": "Australian founders validating a practical pilot", "expected_editorial_catalog_version": 0}
        first = self.send(body, action="first_audience"); self.assertEqual(first.status_code, 201)
        existing = self.current()["audience_options"]
        added = self.send({"expected_editorial_catalog_version": 1, "audience_options": [*existing,
            {"id": "OTHER", "reader_task": "Another draft audience"}]}, key="add-other")
        self.assertEqual(added.status_code, 200, added.data)
        replay = self.send(body, action="first_audience")
        self.assertEqual(replay.status_code, 200, replay.data)
        self.assertEqual(replay.data["catalog"]["editorial_catalog_version"], 2)
        self.assertEqual(replay.data["audience_id"], first.data["audience_id"])
        changed = deepcopy(added.data["audience_options"])
        changed[0].update(version=2, status="draft", approved_by=None, approved_at=None,
            description="The audience has a newer definition", reader_task="Understand the new definition")
        saved = self.send({"expected_editorial_catalog_version": 2, "audience_options": changed,
            "activate_entries": [{"kind": "audience", "id": changed[0]["id"], "version": 2}]}, key="replace-selected")
        self.assertEqual(saved.status_code, 200, saved.data)
        before = self.strategy()
        self.assertEqual(self.send(body, action="first_audience").status_code, 409)
        self.assertEqual(self.strategy(), before)

    def test_first_audience_keyless_if08_existing_selection_behavior_is_unchanged(self):
        body = {"description": "Australian founders validating a practical pilot", "expected_editorial_catalog_version": 0}
        first = self.send(body, action="first_audience", key=None); self.assertEqual(first.status_code, 201)
        again = self.send({**body, "description": "Legacy helper returns the established selection"}, action="first_audience", key=None)
        self.assertEqual(again.status_code, 200, again.data)
        self.assertEqual(first.data["audience_id"], again.data["audience_id"])
        self.assertNotIn(KEY, self.strategy())

    def test_offer_check_is_scheduled_once_and_never_before_outer_commit(self):
        body = {"expected_editorial_catalog_version": 0, "cta_options": [{"id": "brief", "title": "A bounded offer",
            "body": "Bring your workflow baseline.", "button_text": "Start a brief", "button_href": "/brief"}]}
        with override_settings(OFFER_PAGE_CHECK_ENABLED=True, OPENAI_API_KEY="synthetic-never-used"), \
                patch("requests.sessions.Session.request", side_effect=AssertionError("No provider before commit")):
            with transaction.atomic():
                first = self.send(body); self.assertEqual(first.status_code, 200, first.data)
                queued = len(connection.run_on_commit)
                self.assertEqual(queued, 1)
                before = self.strategy()
                replay = self.send(body); self.assertEqual(replay.status_code, 200, replay.data)
                self.assertEqual(len(connection.run_on_commit), queued)
                self.assertEqual(self.strategy(), before)
                transaction.set_rollback(True)
        self.assertNotIn(KEY, self.strategy())

    def test_actual_edit_activation_approval_and_first_audience_wire_match_finite_schema(self):
        body = {"expected_editorial_catalog_version": 0,
            "audience_options": [{"id": "BUILDERS", "reader_task": "Choose a bounded pilot", "status": "draft", "allow_no_offer": True}],
            "activate_entries": [{"kind": "audience", "id": "BUILDERS", "version": 1}]}
        edited = self.send(body); self.assertEqual(edited.status_code, 200, edited.data)
        approval = {"expected_editorial_catalog_version": edited.data["editorial_catalog_version"], "entries": edited.data["review_entries"]}
        approved = self.send(approval, action="approve", key="approved-schema-case")
        self.assertEqual(approved.status_code, 200, approved.data)
        schema = json.loads((Path(__file__).resolve().parent.parent / "contracts/founder-article-v2.schema.json").read_text())
        definitions = {name: wire_rule(rule) for name, rule in schema["$defs"].items()}
        self.assertEqual(len(definitions), 73)
        _validate(body, definitions["FounderEditorialCatalogEditRequest"], definitions)
        _validate(approval, definitions["FounderEditorialCatalogApproveRequest"], definitions)
        _validate(projection(approved.data, definitions["FounderEditorialCatalog"], definitions), definitions["FounderEditorialCatalog"], definitions)
        invalid = deepcopy(body); invalid["audience_options"][0]["private_guess"] = True
        with self.assertRaises(FounderContractMismatch): _validate(invalid, definitions["FounderEditorialCatalogEditRequest"], definitions)
        invalid = deepcopy(body); invalid["expected_editorial_catalog_version"] = True
        with self.assertRaises(FounderContractMismatch): _validate(invalid, definitions["FounderEditorialCatalogEditRequest"], definitions)
        target = os.environ.get("FOUNDER_EXPORT_CATALOG_COMMANDS")
        if target:
            self._export_case(target, "edit_activation", body, edited, None)
            self._export_case(target, "approve", approval, approved, None)

    def test_actual_suggestion_reference_retains_source_provenance_once(self):
        from workflow_runs.models import ContentFactoryRun
        source = ContentFactoryRun.objects.create(organization=self.org, domain=self.org.domain,
            workflow="startup_autofill", run_id="catalogue-research-provenance", result={"autofill": {
                "editorialSuggestions": {"domain": self.org.domain, "researchRunId": "catalogue-research-provenance",
                    "profiles": [{"id": "reviewed-builders"}]}}})
        body = self.initial()
        body["suggestion_reference"] = {"kind": "audience", "research_run_id": source.run_id,
            "suggestion_id": "reviewed-builders", "entry_id": "BUILDERS", "entry_version": 1}
        with patch("requests.sessions.Session.request", side_effect=AssertionError("Local research review cannot call providers")):
            first = self.send(body); before = self.strategy(); replay = self.send(body)
        self.assertEqual(first.status_code, 200, first.data); self.assertEqual(replay.status_code, 200, replay.data)
        self.assertEqual(self.strategy(), before); self.assertEqual(len(before["editorial_suggestion_reviews"]), 1)
        self.assertEqual(before["editorial_suggestion_reviews"][0]["research_run_id"], source.run_id)
        schema = json.loads((Path(__file__).resolve().parent.parent / "contracts/founder-article-v2.schema.json").read_text())
        definitions = {name: wire_rule(rule) for name, rule in schema["$defs"].items()}
        _validate(body, definitions["FounderEditorialCatalogEditRequest"], definitions)
        target = os.environ.get("FOUNDER_EXPORT_CATALOG_COMMANDS")
        if target: self._export_case(target, "edit_suggestion_reference", body, first, replay)

    def _export_case(self, target, name, body, response, replay):
        path = Path(target)
        existing = json.loads(path.read_text()) if path.exists() else {"source": "Actual real-account bearer My startup HTTP/UTF8 PostgreSQL catalogue writes, provider-free local mutation", "cases": []}
        existing["cases"].append({"name": name, "company_id": str(self.company.pk), "request": body,
            "status": response.status_code, "response": response.data,
            "replay": {"status": replay.status_code, "response": replay.data} if replay is not None else None})
        path.write_text(json.dumps(existing, indent=2) + "\n")
