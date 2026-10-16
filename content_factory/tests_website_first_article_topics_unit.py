"""First-article topic/profile selection, without database or model calls."""
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import patch
from django.test import SimpleTestCase

from . import website_first_article_topics as topics
from .editorial_catalog import approve_catalog,update_catalog
from .tests_editorial_catalog_unit import approved_catalog,draft_catalog,approval_payload,edit_payload,NOW
from .website_contract import WebsiteAuthorityError


def candidate(identifier="best",score=100,**updates):
    return {"id":identifier,"title":"Show tested work","keyword":"tested work","opportunityScore":score,
        "sourceRunId":"research","status":"pending",**updates}


class FirstArticleTopicTests(SimpleTestCase):
    def select(self,candidates,**updates):
        return topics.choose(candidates,strategy=updates.get("strategy",{}),sources=updates.get("sources",{}))

    def test_highest_opportunity_wins_across_islands_with_stable_tie_breaks(self):
        rows=[candidate("lower",10,volume=1000),candidate("best",100,volume=50),candidate("tie",100,volume=40)]
        for items in (rows,list(reversed(rows))):
            self.assertEqual(self.select(items)["topic_candidate_id"],"best")

    def test_written_covered_skipped_and_in_progress_candidates_are_never_selected(self):
        for update in ({"alreadyWritten":True},{"writtenArticle":{"id":"old"}},{"coveredTopic":{"id":"old"}},
            {"status":"written"},{"status":"in_progress"},{"status":"skipped"},{"status":"declined"}):
            self.assertEqual(self.select([candidate("blocked",999,**update),candidate()])["topic_candidate_id"],"best")

    def test_duplicate_ids_and_nonfinite_metrics_cannot_hide_an_eligible_topic(self):
        selected=self.select([candidate("ambiguous",999),candidate("ambiguous",999),candidate("nan",float("nan")),candidate()])
        self.assertEqual(selected["topic_candidate_id"],"best")
        self.assertNotIn("editorial_brief",selected)
        self.assertEqual(self.select([candidate("number-string","700"),candidate("lower",100)])["topic_candidate_id"],"number-string")

    def test_empty_oversized_or_unusable_inventory_never_invents_a_topic(self):
        for rows in ([],[candidate()]*2001,[candidate(title="",keyword="")],[candidate(id="")]):
            with self.subTest(rows=len(rows)),self.assertRaises(WebsiteAuthorityError):self.select(rows)

    def test_current_receipts_filter_profiles_and_approved_offers(self):
        selected=self.select([candidate()],strategy=approved_catalog())
        brief=selected["editorial_brief"]
        self.assertEqual((brief["audience_id"],brief["offer_id"],brief["country"]),("BUILDER","studio","AU"))
        self.assertEqual(brief["reader_task"],"Show tested work")
        for strategy in (draft_catalog(),{"editorial_catalog":{}}):
            with self.assertRaises(WebsiteAuthorityError):self.select([candidate()],strategy=strategy)

    def test_retired_or_changed_customer_content_does_not_borrow_old_approval(self):
        strategy=approved_catalog();payload=edit_payload(strategy)
        payload["audience_options"][0]["reader_task"]="Changed customer task"
        for item in [*payload["audience_options"],*payload["cta_options"]]:
            item.update(version=item["version"]+1,status="draft",approved_by=None,approved_at=None)
        changed=update_catalog(strategy,payload)
        with self.assertRaises(WebsiteAuthorityError):self.select([candidate()],strategy=changed)

    def test_multiple_profiles_require_source_customer_binding(self):
        strategy=draft_catalog();payload=edit_payload(strategy)
        payload["audience_options"].append({"id":"OTHER","reader_task":"Another task","allow_no_offer":True})
        strategy=update_catalog(strategy,payload)
        strategy=approve_catalog(strategy,approval_payload(strategy),actor_id="owner",approved_at=NOW)
        with self.assertRaises(WebsiteAuthorityError):self.select([candidate()],strategy=strategy)
        selected=self.select([candidate()],strategy=strategy,sources={"research":{"preferred_audience_id":"BUILDER"}})
        self.assertEqual(selected["editorial_brief"]["audience_id"],"BUILDER")
        with self.assertRaises(WebsiteAuthorityError):
            self.select([candidate()],strategy=strategy,sources={"research":{"preferred_audience_id":"foreign"}})

    def test_no_offer_uses_explicit_approved_permission_and_reason(self):
        strategy=update_catalog({}, {"expected_editorial_catalog_version":0,
            "audience_options":[{"id":"BUILDER","reader_task":"Show tested work","allow_no_offer":True}],"cta_options":[]})
        strategy=approve_catalog(strategy,approval_payload(strategy),actor_id="owner",approved_at=NOW)
        selected=self.select([candidate()],strategy=strategy)
        self.assertEqual(selected["editorial_brief"]["conversion_intent"],"none")
        self.assertTrue(selected["editorial_brief"]["no_offer_reason"])
        payload=edit_payload(strategy);payload["audience_options"][0]["allow_no_offer"]=False
        payload["audience_options"][0].update(version=2,status="draft",approved_by=None,approved_at=None)
        strategy=update_catalog(strategy,payload)
        strategy=approve_catalog(strategy,approval_payload(strategy),actor_id="owner",approved_at=NOW)
        with self.assertRaises(WebsiteAuthorityError):self.select([candidate()],strategy=strategy)

    def test_existing_source_offer_country_is_retained_when_still_approved(self):
        strategy=draft_catalog();payload=edit_payload(strategy);payload["cta_options"][0]["countries"]=["NZ","AU"]
        payload["cta_options"][0]["version"]=2
        strategy=update_catalog(strategy,payload)
        strategy=approve_catalog(strategy,approval_payload(strategy),actor_id="owner",approved_at=NOW)
        selected=self.select([candidate()],strategy=strategy,sources={"research":{"editorial_brief":{
            "audience_id":"BUILDER","offer_id":"studio","country":"NZ"}}})
        self.assertEqual(selected["editorial_brief"]["country"],"NZ")

    def test_inventory_reader_uses_only_the_selected_tenants_source_runs(self):
        from workflow_runs.models import ContentFactoryRun
        from . import vibe_marketing_views as views
        context=SimpleNamespace(organization=SimpleNamespace(pk=7))
        config=SimpleNamespace(pillar_strategy=approved_catalog())
        source=SimpleNamespace(run_id="research",run_request={"preferred_audience_id":"BUILDER"})
        with patch.object(views,"_topic_selection_candidate_pool",return_value=[candidate()]),patch.object(ContentFactoryRun.objects,"filter") as rows:
            rows.return_value.only.return_value=[source]
            selected=topics.current_selection(context,config)
        self.assertEqual(rows.call_args.kwargs["organization_id"],7)
        self.assertEqual(rows.call_args.kwargs["run_id__in"],{"research"})
        self.assertEqual(selected["topic_candidate_id"],"best")
