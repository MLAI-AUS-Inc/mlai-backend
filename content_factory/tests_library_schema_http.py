"""Actual authenticated library/group wire pairs for the additive v2 DTOs."""
from copy import deepcopy
import json
import os
from pathlib import Path

from django.test import TransactionTestCase, override_settings

from . import tests_founder_library_commands as library
from . import tests_founder_run_commands as controls
from .founder_journey_contract import FounderContractMismatch, _validate
from .founder_journey_testing import ALL_P1_FLAGS_ON
from .tests_run_event_authority import FLAGS


def wire_rule(node):
    if isinstance(node, list): return [wire_rule(item) for item in node]
    if not isinstance(node, dict): return node
    result = {key: wire_rule(value) for key, value in node.items() if key not in {"properties", "required"}}
    if "properties" in node:
        names = {key: value.get("x-wire-name", key) for key, value in node["properties"].items()}
        result["properties"] = {names[key]: wire_rule(value) for key, value in node["properties"].items()}
        if "required" in node: result["required"] = [names[key] for key in node["required"]]
    return result


def projected(value, rule, definitions):
    if "$ref" in rule: return projected(value, definitions[rule["$ref"][8:]], definitions)
    if isinstance(value, dict) and rule.get("x-wire-project") is True:
        return {key: projected(item, rule["properties"][key], definitions) for key, item in value.items() if key in rule.get("properties", {})}
    if isinstance(value, list) and "items" in rule:
        return [projected(item, rule["items"], definitions) for item in value]
    if isinstance(value, dict):
        return {key: projected(item, rule.get("properties", {}).get(key, {}), definitions) for key, item in value.items()}
    return value


def check(case, request, response, request_name, response_name):
    source = Path(__file__).resolve().parent.parent / "contracts/founder-article-v2.schema.json"
    definitions = {name: wire_rule(rule) for name, rule in json.loads(source.read_text())["$defs"].items()}
    _validate(request, definitions[request_name], definitions)
    _validate(projected(response, definitions[response_name], definitions), definitions[response_name], definitions)
    invalid = {**request, "not_a_declared_field": "must not disappear"}
    try: _validate(invalid, definitions[request_name], definitions)
    except FounderContractMismatch: pass
    else: raise AssertionError("Strict requests may not project away unknown input")
    return definitions


def export(case, *, company_id, route, request, response, replay, request_name, response_name, key):
    target = os.environ.get("FOUNDER_EXPORT_LIBRARY_SCHEMA")
    if not target: return
    path = Path(target)
    value = json.loads(path.read_text()) if path.exists() else {
        "source": "Real bearer/My startup HTTP and UTF8 PostgreSQL; only external CF/provider requests faked",
        "schema": "Additive library/group DTOs; original62 and prior73 definitions unchanged",
        "cases": []}
    value["cases"].append({"case": case, "company_id": str(company_id), "route": route,
        "request": request, "idempotency_key": key, "status": response.status_code,
        "response": response.data, "replay": {"status": replay.status_code, "response": replay.data},
        "request_definition": request_name, "response_definition": response_name})
    path.write_text(json.dumps(value, indent=2) + "\n")


@override_settings(**FLAGS, **ALL_P1_FLAGS_ON, CONTENT_FACTORY_URL="https://cf.fixture.invalid", CONTENT_FACTORY_API_KEY="synthetic-library-key")
class LibrarySchemaHTTPTests(TransactionTestCase):
    setUp = library.FounderLibraryCommandTests.setUp
    send = library.FounderLibraryCommandTests.send
    article = library.FounderLibraryCommandTests.article
    cancellation = library.FounderLibraryCommandTests.cancellation

    def test_exact_mounted_topic_ignore_and_restore_body_response_pair(self):
        body = {"companyId": str(self.company.pk), "keyword": "A retained topic", "sessionId": "original-research",
            "feedbackType": "declined", "reasonCode": "not_appropriate", "reasonText": None,
            "declineScope": "similar", "source": "homepage_topic_card"}
        first = self.send("topic-feedback", body, key="actual-topic-ignore"); replay = self.send("topic-feedback", body, key="actual-topic-ignore")
        self.assertEqual((first.status_code, replay.status_code), (201, 201), first.data)
        self.assertEqual(first.data, replay.data)
        check("ignore", body, first.data, "FounderArticleTopicFeedbackRequest", "FounderArticleTopicFeedbackResponse")
        export("topic_ignore", company_id=self.company.pk, route="topic-feedback", request=body,
            response=first, replay=replay, request_name="FounderArticleTopicFeedbackRequest",
            response_name="FounderArticleTopicFeedbackResponse", key="actual-topic-ignore")
        restore = {"companyId": str(self.company.pk)}; route = f"topic-feedback/{first.data['id']}/restore"
        restored = self.send(route, restore, key="actual-topic-restore"); again = self.send(route, restore, key="actual-topic-restore")
        self.assertEqual((restored.status_code, again.status_code), (200, 200), restored.data)
        self.assertEqual(restored.data, again.data); self.assertFalse(restored.data["active"])
        check("restore", restore, restored.data, "FounderArticleTopicRestoreRequest", "FounderArticleTopicRestoreResponse")
        export("topic_restore", company_id=self.company.pk, route=route, request=restore,
            response=restored, replay=again, request_name="FounderArticleTopicRestoreRequest",
            response_name="FounderArticleTopicRestoreResponse", key="actual-topic-restore")

    def test_exact_author_settings_body_and_current_workspace_response(self):
        first = self.send("settings", self.author, key="actual-author-save", method="put")
        replay = self.send("settings", self.author, key="actual-author-save", method="put")
        self.assertEqual((first.status_code, replay.status_code), (200, 200), first.data)
        self.assertEqual(first.data["settings"]["authors"], replay.data["settings"]["authors"])
        definitions = check("authors", self.author, first.data, "FounderArticleAuthorSettingsRequest", "FounderArticleWorkspaceInputs")
        invalid = deepcopy(self.author); invalid["authors"][0]["unverifiedAuthority"] = True
        with self.assertRaises(FounderContractMismatch):
            _validate(invalid, definitions["FounderArticleAuthorSettingsRequest"], definitions)
        export("author_settings", company_id=self.company.pk, route="settings", request=self.author,
            response=first, replay=replay, request_name="FounderArticleAuthorSettingsRequest",
            response_name="FounderArticleWorkspaceInputs", key="actual-author-save")

    def test_exact_mounted_discard_body_replays_after_original_row_deleted(self):
        article, run = self.article(); body = {"companyId": str(self.company.pk)}
        route = f"written-articles/{article.pk}/discard"
        with self.cancellation(run) as outbound:
            first = self.send(route, body, key="actual-discard"); replay = self.send(route, body, key="actual-discard")
        self.assertEqual((first.status_code, replay.status_code), (200, 200), first.data)
        self.assertEqual(first.data, replay.data); self.assertEqual(outbound.call_count, 1)
        self.assertEqual(first.data["articleId"], str(article.pk))
        check("discard", body, first.data, "FounderArticleDiscardRequest", "FounderArticleDiscardResponse")
        export("written_discard", company_id=self.company.pk, route=route, request=body,
            response=first, replay=replay, request_name="FounderArticleDiscardRequest",
            response_name="FounderArticleDiscardResponse", key="actual-discard")


@override_settings(**FLAGS, **ALL_P1_FLAGS_ON, ARTICLES_FREE_FIRST_ARTICLE_ENABLED=True,
    CONTENT_FACTORY_URL="https://cf.fixture.invalid", CONTENT_FACTORY_API_KEY="synthetic-paired-key")
class GroupSchemaHTTPTests(TransactionTestCase):
    fixture_name = "r3_owned_portable_review_approval.json"
    reads = controls.FounderRunCommandTests.reads
    setUp = controls.FounderRunCommandTests.setUp
    post = controls.FounderRunCommandTests.post
    remote = controls.FounderRunCommandTests.remote

    def test_exact_mounted_group_body_keeps_cancel_group_and_original_scope(self):
        body = {"companyId": str(self.company.pk), "cancel_group": True}
        with self.remote("cancel") as outbound:
            first = self.post("cancel", body, key="actual-group-cancel")
            replay = self.post("cancel", body, key="actual-group-cancel")
            changed = self.post("cancel", {**body, "cancel_group": False}, key="actual-group-cancel")
        self.assertEqual((first.status_code, replay.status_code, changed.status_code), (202, 202, 409), first.data)
        self.assertEqual(outbound.call_count, 1)
        self.assertEqual(first.data["cancelledRunIds"], [self.run.run_id])
        self.assertEqual(replay.data["cancelledRunIds"], first.data["cancelledRunIds"])
        check("group", body, first.data, "FounderArticleCancelGroupRequest", "FounderArticleCancelGroupResponse")
        export("cancel_group", company_id=self.company.pk, route=f"runs/{self.run.run_id}/cancel", request=body,
            response=first, replay=replay, request_name="FounderArticleCancelGroupRequest",
            response_name="FounderArticleCancelGroupResponse", key="actual-group-cancel")
