"""Founder-owned overview and update titles, without database access."""
from datetime import date
from types import SimpleNamespace as Obj
from unittest.mock import patch

from django.test import SimpleTestCase

from startup_updates import update_identity
from startup_updates.overview import founder_overview
from vibe_raising.serializers import VibeRaisingMonthlyUpdateUpsertSerializer
from vibe_raising.views import _build_manual_structured_memo


class FounderOverviewTests(SimpleTestCase):
    generated = {"summary": "x", "startupOverview": "An AI rewrite."}

    def test_run_overview_wins_verbatim_including_a_deliberate_blank(self):
        for chosen in ("We help nurses plan visits.", ""):
            memo = founder_overview(self.generated, {"startup_overview": f"  {chosen} "}, {"startupOverview": "Old."})
            self.assertEqual(memo["startupOverview"], chosen)

    def test_saved_overview_then_startup_description_are_kept(self):
        request = {"startup_context": {"short_description": "From signup."}}
        self.assertEqual(founder_overview(self.generated, request, {"startupOverview": "Saved."})["startupOverview"], "Saved.")
        self.assertEqual(founder_overview(self.generated, request, {"startupOverview": " "})["startupOverview"], "From signup.")

    def test_generation_only_fills_when_founder_has_nothing(self):
        self.assertEqual(founder_overview(self.generated, {}, {})["startupOverview"], "An AI rewrite.")
        self.assertEqual(self.generated["startupOverview"], "An AI rewrite.")


class CustomTitleTests(SimpleTestCase):
    def identity(self, memo):
        draft = Obj(pk=3, month=date(2026, 9, 1), update_date=None, creation_key=None,
            first_published_at=None, published_at=None, published_revision_id=None)
        with patch.object(update_identity, "monthly_identity", return_value={"monthSequence": 4, "updateTitle": "September update #4"}):
            return update_identity.identity_payload(draft, memo)

    def test_custom_title_replaces_default_and_blank_restores_it(self):
        self.assertEqual(self.identity({"custom_title": "Our first paying customer"})["updateTitle"], "Our first paying customer")
        self.assertEqual(self.identity({"custom_title": "  "})["updateTitle"], "September update #4")
        self.assertEqual(self.identity({})["updateTitle"], "September update #4")
        self.assertEqual(self.identity({"custom_title": "Big month"})["monthSequence"], 4)
        self.assertEqual(self.identity({"custom_title": "Big month"})["customTitle"], "Big month")
        self.assertIsNone(self.identity({})["customTitle"])

    def test_save_payload_accepts_and_normalises_title(self):
        serializer = VibeRaisingMonthlyUpdateUpsertSerializer(data={
            "month": "September", "year": 2026, "updateTitle": "  Our   first  customer "})
        self.assertTrue(serializer.is_valid(), serializer.errors)
        memo = _build_manual_structured_memo(serializer.validated_data)
        self.assertEqual(memo["custom_title"], "Our first customer")
        self.assertNotIn("custom_title", _build_manual_structured_memo({}))
        too_long = VibeRaisingMonthlyUpdateUpsertSerializer(data={"month": "September", "year": 2026, "updateTitle": "x" * 121})
        self.assertFalse(too_long.is_valid())
