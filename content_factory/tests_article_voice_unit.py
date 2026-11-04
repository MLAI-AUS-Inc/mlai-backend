"""Article voice validation, storage and dispatch contract, with no database or network."""
from contextlib import nullcontext
from datetime import datetime, timezone
from types import SimpleNamespace as Obj
from unittest.mock import Mock, patch

from django.test import SimpleTestCase
from rest_framework.exceptions import ValidationError

from content_factory import article_voice as voice
from content_factory import vibe_marketing_views as views
from content_factory.editorial_catalog import merge_strategy, public_strategy
from content_factory.models import OrganizationContentConfig
from organizations.models import Organization

PRESETS = {
    "builder_peer": {"register": 2, "stance": 2, "energy": 0, "humour": 1, "convention": 0},
    "calm_guide": {"register": 1, "stance": 1, "energy": -2, "humour": -1, "convention": -1},
    "challenger": {"register": 0, "stance": 2, "energy": 1, "humour": 0, "convention": 2},
    "trusted_expert": {"register": -1, "stance": 1, "energy": -1, "humour": -2, "convention": -1},
    "upbeat_coach": {"register": 2, "stance": 0, "energy": 2, "humour": 1, "convention": 0},
}


def profile(**changes):
    base = {
        "schemaVersion": 1,
        "preset": "builder_peer",
        "category": "general",
        "dimensions": dict(PRESETS["builder_peer"]),
        "readerExpertise": "practitioner",
        "narrator": "brand_we",
        "voiceCard": [{"is": "Direct", "isNot": "blunt"}],
        "stances": ["A stop prompt is not an access control."],
        "avoidWords": ["leverage"],
        "preferWords": ["builders"],
    }
    base.update(changes)
    return base


class ArticleVoiceValidationTests(SimpleTestCase):
    def test_every_preset_spends_the_budget_and_validates(self):
        for name, dimensions in PRESETS.items():
            with self.subTest(preset=name):
                spent = sum(voice.level_cost(level) for level in dimensions.values())
                self.assertEqual(spent, voice.POINT_BUDGET)
                self.assertEqual(voice.validate_article_voice(profile(preset=name, dimensions=dimensions))["preset"], name)

    def test_budget_caps_and_ranges_are_enforced(self):
        cases = {
            "over budget": profile(dimensions={**PRESETS["builder_peer"], "energy": 2}),
            "humour capped for health": profile(category="health", dimensions={**PRESETS["builder_peer"]}),
            "energy capped for legal": profile(category="legal", preset=None,
                                               dimensions={"register": 0, "stance": 0, "energy": 1, "humour": 0, "convention": 0}),
            "out of range": profile(dimensions={**PRESETS["builder_peer"], "register": 3}),
            "boolean is not a level": profile(dimensions={**PRESETS["builder_peer"], "humour": True}),
            "missing trait": profile(dimensions={"register": 1}),
            "unknown key": profile(tone="sassy"),
            "unknown preset": profile(preset="pirate"),
            "wrong schema": profile(schemaVersion=2),
            "half a voice line": profile(voiceCard=[{"is": "Direct"}]),
            "too many stances": profile(stances=["One."] * 6),
            "long word": profile(avoidWords=["x" * 41]),
            "empty stance": profile(stances=["  "]),
        }
        for label, raw in cases.items():
            with self.subTest(label), self.assertRaises(voice.ArticleVoiceError):
                voice.validate_article_voice(raw)

    def test_free_text_is_cleaned_and_server_fields_are_ignored(self):
        clean = voice.validate_article_voice(profile(
            version=99, updatedAt="1999-01-01T00:00:00Z",
            stances=["  We ship\u0007 small.  "],
            avoidWords=["Leverage", "leverage", " synergy "],
        ))
        self.assertNotIn("version", clean)
        self.assertNotIn("updatedAt", clean)
        self.assertEqual(clean["stances"], ["We ship  small."])
        self.assertEqual(clean["avoidWords"], ["Leverage", "synergy"])

    def test_stored_values_read_tolerantly(self):
        self.assertIsNone(voice.article_voice_payload(None))
        self.assertIsNone(voice.article_voice_payload({"article_voice": {"schemaVersion": 7}}))
        stored = {**profile(), "version": 3, "updatedAt": "2026-10-23T04:12:00Z"}
        read = voice.article_voice_payload({"article_voice": stored})
        self.assertEqual(read["version"], 3)
        self.assertEqual(read["updatedAt"], "2026-10-23T04:12:00Z")

    def test_dispatch_digest_is_canonical_and_order_independent(self):
        stored = {**profile(), "version": 1, "updatedAt": "2026-10-23T04:12:00Z"}
        reordered = dict(reversed(list(stored.items())))
        first = voice.article_voice_for_dispatch({"article_voice": stored})
        second = voice.article_voice_for_dispatch({"article_voice": reordered})
        self.assertEqual(first["sha256"], second["sha256"])
        without = {key: value for key, value in first.items() if key != "sha256"}
        self.assertEqual(first["sha256"], voice.canonical_sha256(without))
        self.assertIsNone(voice.article_voice_for_dispatch({}))


class ArticleVoiceStorageTests(SimpleTestCase):
    def save(self, config, value, now):
        organization = Obj(pk=5)
        with patch("django.db.transaction.atomic", return_value=nullcontext()), \
             patch.object(Organization.objects, "select_for_update") as orgs, \
             patch.object(OrganizationContentConfig.objects, "select_for_update") as configs:
            orgs.return_value.get.return_value = organization
            configs.return_value.get_or_create.return_value = (config, False)
            return voice.save_article_voice(organization, value, now=now)

    def test_save_versions_the_voice_without_touching_other_namespaces(self):
        config = Obj(pillar_strategy={"editorial_catalog": {"version": 2}, "startup_branding": {"avatarUrl": "logo"}},
                     save=Mock())
        first = self.save(config, voice.validate_article_voice(profile()), datetime(2026, 10, 23, 4, 12, tzinfo=timezone.utc))
        second = self.save(config, voice.validate_article_voice(profile(preset=None)),
                           datetime(2026, 10, 24, tzinfo=timezone.utc))
        self.assertEqual((first["version"], second["version"]), (1, 2))
        self.assertEqual(second["updatedAt"], "2026-10-24T00:00:00Z")
        self.assertEqual(config.pillar_strategy["editorial_catalog"], {"version": 2})
        self.assertEqual(config.pillar_strategy["startup_branding"], {"avatarUrl": "logo"})
        config.save.assert_called_with(update_fields=["pillar_strategy", "updated_at"])
        self.assertIsNone(self.save(config, None, None))
        self.assertNotIn("article_voice", config.pillar_strategy)

    def test_scans_cannot_erase_it_and_workers_do_not_receive_it_as_strategy(self):
        existing = {"article_voice": {**profile(), "version": 4}, "pillars": ["old"]}
        incoming = {"pillars": ["new"], "article_voice": {"schemaVersion": 1, "dimensions": {}}}
        merged = merge_strategy(existing, incoming)
        self.assertEqual(merged["article_voice"]["version"], 4)
        self.assertEqual(merged["pillars"], ["new"])
        self.assertNotIn("article_voice", public_strategy(merged))
        self.assertNotIn("article_voice", merge_strategy({}, incoming))


class ArticleVoiceSettingsTests(SimpleTestCase):
    def put(self, data):
        view = views.VibeMarketingSettingsView()
        with patch.object(views.VibeMarketingSettingsView, "_save_settings", return_value=Mock()) as save, \
             patch.object(views, "_serialize_bootstrap", return_value={}):
            response = views.VibeMarketingSettingsView.put(view, Obj(data=data, user=Obj(pk=1)))
        return view, response, save

    def test_invalid_voice_is_rejected_before_any_write(self):
        with patch.object(views.VibeMarketingSettingsView, "_save_settings") as save:
            with self.assertRaises(ValidationError) as raised:
                views.VibeMarketingSettingsView.put(
                    views.VibeMarketingSettingsView(),
                    Obj(data={"companyId": "c", "articleVoice": profile(dimensions={"register": 9})}, user=Obj(pk=1)),
                )
        self.assertIn("articleVoice", raised.exception.detail)
        save.assert_not_called()

    def test_sparse_saves_validate_clear_or_leave_the_voice(self):
        view, response, save = self.put({"companyId": "c", "articleVoice": profile()})
        self.assertEqual(response.status_code, 200)
        self.assertTrue(view.voice_submitted)
        self.assertEqual(view.prepared_voice["preset"], "builder_peer")
        view, _, _ = self.put({"companyId": "c", "articleVoice": None})
        self.assertTrue(view.voice_submitted)
        self.assertIsNone(view.prepared_voice)
        view, _, _ = self.put({"companyId": "c", "articleDeliveryMode": "review_draft"})
        self.assertFalse(view.voice_submitted)
