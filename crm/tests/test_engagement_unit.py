"""Rating presentation, filtering and review-hold protection without a database."""

from copy import deepcopy
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import Mock, patch

from django.db.models import Q
from django.test import SimpleTestCase

from community_chat.crm import serializers as wire
from community_chat.crm.errors import CrmError
from community_chat.crm.services import _apply_fields
from crm import blast_email, community_optins
from crm.community_optins import contact_is_opted_in
from crm.engagement import engagement_rating, marketing_review_hold
from crm.models import Blast, Contact
from crm.queries import contacts_queryset, engagement_stars_q, marketing_consent_q, sort_contacts
from crm.segments import SegmentError, definition_q, validate_definition

NOW = datetime(2026, 11, 2, tzinfo=timezone.utc)
RATING = {
    "stars": 4,
    "score": 48.5,
    "version": "engagement-v1",
    "as_of": "2026-11-02",
    "reason": "Attended twice recently; app activity unavailable.",
    "coverage": {"provisional": True, "app_activity_available": False},
}


class RatingSnapshotTests(SimpleTestCase):
    def test_numeric_rating_preserves_explanation_and_unknown_source_coverage(self):
        attributes = {"engagement_rating": deepcopy(RATING)}
        result = engagement_rating(attributes)
        self.assertEqual(result, RATING)
        self.assertIsInstance(result["stars"], int)
        self.assertIsInstance(result["score"], float)
        result["coverage"]["provisional"] = False
        self.assertTrue(attributes["engagement_rating"]["coverage"]["provisional"])

    def test_date_and_timestamp_snapshots_are_accepted(self):
        for value in ("2026-11-02", "2026-11-02T02:00:00Z", "2026-11-02T13:00:00+11:00"):
            with self.subTest(value=value):
                rating = {**RATING, "as_of": value}
                self.assertEqual(engagement_rating({"engagement_rating": rating})["as_of"], value)

    def test_missing_and_malformed_snapshots_stay_unrated(self):
        for attributes in (None, [], {}, {"engagement_rating": "5"}, {"engagement_rating": {}}):
            with self.subTest(attributes=attributes):
                self.assertIsNone(engagement_rating(attributes))
        for field, values in {
            "stars": (0, 6, True, "5", 4.5),
            "score": (-1, True, "48", float("inf"), float("nan")),
            "version": (None, "", " ", 1),
            "as_of": (None, "not a date", "2026-02-30"),
            "reason": (None, [], "x" * 2001),
            "coverage": (None, "complete", []),
        }.items():
            for value in values:
                with self.subTest(field=field, value=value):
                    self.assertIsNone(engagement_rating({"engagement_rating": {**RATING, field: value}}))

    def test_rating_does_not_make_an_unsubscribed_contact_eligible(self):
        contact = Contact(
            email="synthetic@example.test",
            attributes={"engagement_rating": {**RATING, "stars": 5}},
            email_status=Contact.EmailStatus.UNSUBSCRIBED,
            marketing_consent=Contact.MarketingConsent.WITHDRAWN,
        )
        result = wire.contact_row(contact)
        self.assertEqual(result["engagement_stars"], 5)
        self.assertEqual(result["email_status"], Contact.EmailStatus.UNSUBSCRIBED)
        self.assertEqual(result["marketing_consent"], Contact.MarketingConsent.WITHDRAWN)
        self.assertFalse(result["marketing_eligible"])

    def test_detail_exposes_rating_separately_from_editable_attributes(self):
        contact = Contact(
            email="synthetic@example.test",
            last_attended_at=NOW,
            attributes={"engagement_rating": RATING, "marketing_review_hold": True, "city_note": "Sydney"},
        )
        result = wire.contact_detail(contact, now=NOW, registrations=[], deliveries=[], replies=[], history=[])
        self.assertEqual(result["engagement_rating"], RATING)
        self.assertEqual(result["engagement_score"], 48.5)
        self.assertTrue(result["marketing_review_hold"])
        self.assertEqual(result["attributes"], {"city_note": "Sydney"})
        self.assertIsNone(result["consent"]["email"]["inferred_until"])


class RatingFiltersTests(SimpleTestCase):
    def test_star_range_and_sort_are_strictly_parsed(self):
        filters = wire.contact_list_filters({"min_stars": "4", "max_stars": "5", "sort": "engagement"})
        self.assertEqual((filters["min_stars"], filters["max_stars"], filters["sort"]), (4, 5, "engagement"))
        self.assertEqual(wire.contact_list_filters({"stars": "1"})["stars"], 1)
        for params in (
            {"stars": "0"}, {"stars": "6"}, {"stars": True}, {"stars": "4.0"},
            {"min_stars": "bad"}, {"max_stars": "4,5"},
            {"min_stars": "5", "max_stars": "4"},
            {"stars": "3", "min_stars": "4"}, {"stars": "5", "max_stars": "4"},
        ):
            with self.subTest(params=params), self.assertRaises(CrmError):
                wire.contact_list_filters(params)

    def test_numeric_range_never_includes_zero_out_of_range_or_unrated(self):
        expected = (("eq", 1, [1]), ("gte", 4, [4, 5]), ("lte", 2, [1, 2]))
        for op, value, allowed in expected:
            with self.subTest(op=op, value=value):
                self.assertEqual(
                    engagement_stars_q(op, value),
                    Q(attributes__engagement_rating__stars__in=allowed),
                )
        for value in (0, 6, "5", True):
            with self.subTest(value=value), self.assertRaises(ValueError):
                engagement_stars_q("eq", value)

    def test_sort_keeps_unrated_last_and_is_stable(self):
        query = sort_contacts(Contact.objects.all(), "engagement").query
        stars, score, tie = query.order_by
        self.assertTrue(stars.descending)
        self.assertTrue(stars.nulls_last)
        self.assertTrue(score.descending)
        self.assertTrue(score.nulls_last)
        self.assertEqual(tie, "pk")
        self.assertIn("engagement_sort_stars", query.annotations)

    def test_list_and_segment_share_star_range_filter(self):
        rule = {"type": "engagement_stars", "op": "gte", "value": 4}
        canonical = validate_definition({"rules": [rule]})
        self.assertEqual(canonical["rules"], [rule])
        self.assertEqual(definition_q(canonical, now=NOW), engagement_stars_q("gte", 4))
        with patch("crm.queries.engagement_stars_q", wraps=engagement_stars_q) as predicate:
            contacts_queryset(now=NOW, stars=4, min_stars=2, max_stars=5)
        self.assertEqual([call.args for call in predicate.call_args_list], [("eq", 4), ("gte", 2), ("lte", 5)])

    def test_segment_rejects_invalid_rating_bounds_before_database_work(self):
        for value in (0, 6, True, "4"):
            with self.subTest(value=value), self.assertRaises(SegmentError) as caught:
                validate_definition({"rules": [{"type": "engagement_stars", "op": "gte", "value": value}]})
            self.assertEqual(caught.exception.pointer, "/rules/0/value")


class ReviewHoldTests(SimpleTestCase):
    def test_review_hold_blocks_queued_marketing_but_not_event_logistics(self):
        contact = Contact(
            email="synthetic@example.test", marketing_consent=Contact.MarketingConsent.GRANTED,
            attributes={"marketing_review_hold": True},
        )
        self.assertEqual(
            blast_email._drop_reason(Blast(purpose=Blast.Purpose.MARKETING), contact, False),
            "marketing_review_hold",
        )
        self.assertIsNone(blast_email._drop_reason(Blast(purpose=Blast.Purpose.EVENT_UPDATE), contact, False))
        self.assertEqual(contact.marketing_consent, Contact.MarketingConsent.GRANTED)

    def test_review_hold_blocks_warmup_before_source_queries(self):
        contact = Contact(
            email="synthetic@example.test", user_id=1,
            marketing_consent=Contact.MarketingConsent.GRANTED,
            attributes={"marketing_review_hold": True},
        )
        with patch("crm.community_optins.CommunityMemberProfile.objects") as profiles:
            self.assertFalse(contact_is_opted_in(contact))
        profiles.filter.assert_not_called()

    def test_source_sync_does_not_turn_a_review_hold_into_withdrawal(self):
        contact = Contact(
            email="synthetic@example.test", user_id=1,
            marketing_consent=Contact.MarketingConsent.GRANTED,
            marketing_consent_source="community_onboarding:synthetic",
            attributes={"marketing_review_hold": True},
        )
        profile = SimpleNamespace(user=SimpleNamespace(email=contact.email))
        for evidence, should_withdraw in ((object(), False), (None, True)):
            with (
                self.subTest(source_withdrawn=should_withdraw),
                patch.object(community_optins.transaction, "atomic"),
                patch.object(community_optins.CommunityMemberProfile, "objects") as profiles,
                patch.object(community_optins.Contact, "objects") as contacts,
                patch.object(community_optins.AccountBan, "objects") as bans,
                patch.object(community_optins.CrmAuditEvent, "objects"),
                patch.object(community_optins, "current_consent", return_value=evidence),
                patch.object(community_optins, "contact_is_opted_in", wraps=contact_is_opted_in) as check,
                patch("crm.listmonk_sync.record_email_opt_out") as opt_out,
            ):
                profiles.select_related.return_value.filter.return_value.iterator.return_value = []
                profiles.select_related.return_value.filter.return_value.first.return_value = profile
                contacts.filter.return_value.select_related.return_value = [contact]
                bans.filter.return_value.exists.return_value = False
                result = community_optins.sync_community_optins()
                check.assert_called_once_with(contact, include_review_hold=False)
                self.assertEqual(opt_out.call_count, int(should_withdraw))
                self.assertEqual(result.get("withdrawn", 0), int(should_withdraw))
                if not should_withdraw:
                    self.assertEqual(contact.marketing_consent, Contact.MarketingConsent.GRANTED)
                    self.assertEqual(contact.email_status, Contact.EmailStatus.ACTIVE)
                    self.assertTrue(contact.attributes["marketing_review_hold"])

    def test_hold_added_after_subscriber_loading_prevents_provider_start(self):
        blast = Blast(purpose=Blast.Purpose.MARKETING)
        contacts = Mock()
        contacts.select_for_update.return_value.filter.return_value.order_by.return_value = [
            Contact(attributes={"marketing_review_hold": True})
        ]
        sender_rows = Mock()
        sender_rows.select_for_update.return_value.filter.return_value.order_by.return_value = []
        with (
            patch.object(blast_email.transaction, "atomic"),
            patch.object(blast_email.SenderMailbox, "objects", sender_rows),
            patch.object(blast_email.Blast, "objects") as blasts,
            patch.object(blast_email.Contact, "objects", contacts),
            patch.object(blast_email, "email_block", return_value=None),
            patch.object(blast_email.listmonk, "set_campaign_status") as start,
        ):
            blasts.select_for_update.return_value.get.return_value = blast
            blasts.values_list.return_value.get.return_value = (None, {})
            self.assertEqual(blast_email.start_campaign(blast.pk, 123, now=NOW), "held")
        start.assert_not_called()
        contacts.select_for_update.assert_called_once_with()

    def test_only_missing_or_boolean_false_clears_a_review_hold(self):
        self.assertFalse(marketing_review_hold({}))
        self.assertFalse(marketing_review_hold({"marketing_review_hold": False}))
        for value in (True, None, "false", 0, [], {}):
            with self.subTest(value=value):
                self.assertTrue(marketing_review_hold({"marketing_review_hold": value}))

    def test_consent_predicate_gates_both_explicit_and_inferred_permission(self):
        predicate = marketing_consent_q(NOW, prefix="contact__")
        reviewed = predicate.children[0]
        self.assertEqual(
            reviewed,
            Q(contact__attributes__marketing_review_hold__isnull=True)
            | Q(contact__attributes__marketing_review_hold=False),
        )

    def test_ordinary_attribute_edits_preserve_rating_and_hold(self):
        contact = Contact(attributes={"engagement_rating": deepcopy(RATING), "marketing_review_hold": True, "note": "old"})
        changed = _apply_fields(contact, {"attributes": {"note": "new"}}, now=NOW)
        self.assertEqual(changed, ["attributes"])
        self.assertEqual(contact.attributes["engagement_rating"], RATING)
        self.assertIs(contact.attributes["marketing_review_hold"], True)
        self.assertEqual(contact.attributes["note"], "new")

    def test_api_and_direct_service_cannot_set_or_clear_reserved_metadata(self):
        for key in ("engagement_rating", "marketing_review_hold"):
            with self.subTest(key=key):
                serializer = wire.ContactFieldsInput(data={"attributes": {key: "false"}}, partial=True)
                self.assertFalse(serializer.is_valid())
                self.assertEqual(serializer.errors["attributes"][0].code, "reserved_attribute")
                contact = Contact(attributes={"marketing_review_hold": True})
                with self.assertRaises(CrmError) as caught:
                    _apply_fields(contact, {"attributes": {key: False}}, now=NOW)
                self.assertEqual(caught.exception.code, "reserved_attribute")
                self.assertIs(contact.attributes["marketing_review_hold"], True)
