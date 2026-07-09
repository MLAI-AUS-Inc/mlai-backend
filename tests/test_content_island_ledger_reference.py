"""A long content-island client_request_id must not blow the varchar(100) Ledger column.

arb-gen.com POST /api/v1/vibe-marketing/discovery 500'd (2026-07-09) with
psycopg StringDataRightTruncation: the generated client_request_id embeds the island
slug (e.g. "australian-standards-aligned-arboricultural-documentation"), pushing it
past 100 chars, and it was written straight into roo.Ledger.reference_id
(CharField(max_length=100)). NOTE: sqlite does NOT enforce varchar lengths, so these
tests assert the resulting length explicitly rather than relying on a DB error.
"""
from django.contrib.auth import get_user_model
from django.test import TestCase

from integrations.services.article_generation import (
    _ledger_reference_id,
    _LEDGER_REFERENCE_ID_MAX,
    charge_content_factory_topic_generation_for_user,
)

User = get_user_model()

PAID_DOMAIN = "arb-gen.com"
_LONG_SLUG = "australian-standards-aligned-arboricultural-documentation"
_LONG_CRID = f"vibe-content-island-topics:395:{_LONG_SLUG}:{'a' * 32}"  # ~130 chars


class LedgerReferenceIdHelperTests(TestCase):
    def test_short_id_is_unchanged(self):
        short = "vibe-article:1:abc123"
        self.assertEqual(_ledger_reference_id(short), short)
        self.assertLessEqual(len(short), _LEDGER_REFERENCE_ID_MAX)

    def test_long_id_is_clamped_to_column_limit(self):
        out = _ledger_reference_id(_LONG_CRID)
        self.assertLessEqual(len(out), _LEDGER_REFERENCE_ID_MAX)
        self.assertGreater(len(_LONG_CRID), _LEDGER_REFERENCE_ID_MAX)  # precondition
        self.assertTrue(out.startswith("vibe-content-island-topics:"))  # readable prefix kept

    def test_clamp_is_deterministic_and_distinct(self):
        # Same id → same reference (so a charge and its later refund stay linked).
        self.assertEqual(_ledger_reference_id(_LONG_CRID), _ledger_reference_id(_LONG_CRID))
        # Distinct long ids → distinct references (sha1 tail).
        other = _LONG_CRID[:-4] + "zzzz"
        self.assertNotEqual(_ledger_reference_id(_LONG_CRID), _ledger_reference_id(other))

    def test_empty_id_is_handled(self):
        self.assertEqual(_ledger_reference_id(""), "")
        self.assertEqual(_ledger_reference_id(None), "")


class ContentIslandTopicChargeTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(
            email="island-charge@example.com", password="password", role="participant"
        )
        from roo.models import PointsAccount

        PointsAccount.objects.create(user=self.user, balance=1000)

    def test_topic_charge_with_long_client_request_id_does_not_overflow_reference(self):
        # Reproduces the arb-gen 500: a 130-char client_request_id used to blow
        # Ledger.reference_id(varchar(100)). The charge must now succeed with a
        # clamped reference_id, while idempotency_key keeps the FULL id.
        _user, ledger, cost = charge_content_factory_topic_generation_for_user(
            user=self.user,
            actor_id="U0FOUNDER",
            article_request={"client_request_id": _LONG_CRID},
            resolved_domain=PAID_DOMAIN,
        )
        self.assertIsNotNone(ledger)
        self.assertGreater(cost, 0)
        self.assertLessEqual(len(ledger.reference_id), 100)
        # The full, untruncated id is preserved on the idempotency key (which is
        # what actually dedupes / matches refunds — and its column is varchar(255)).
        self.assertEqual(
            ledger.idempotency_key,
            f"content_factory:topic_generation:charge:{_LONG_CRID}",
        )
        self.assertEqual(ledger.delta, -cost)

    def test_topic_charge_is_idempotent_on_repeat(self):
        # A second charge with the same long id returns the same ledger (idempotency
        # key match), never a duplicate spend — unaffected by reference clamping.
        _u1, first, _c1 = charge_content_factory_topic_generation_for_user(
            user=self.user, actor_id="U0FOUNDER",
            article_request={"client_request_id": _LONG_CRID}, resolved_domain=PAID_DOMAIN,
        )
        _u2, second, _c2 = charge_content_factory_topic_generation_for_user(
            user=self.user, actor_id="U0FOUNDER",
            article_request={"client_request_id": _LONG_CRID}, resolved_domain=PAID_DOMAIN,
        )
        self.assertEqual(first.id, second.id)
