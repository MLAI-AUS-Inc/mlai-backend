"""Compile catalogue queries without opening a database or running migrations."""
import os
import unittest
from unittest.mock import patch


class SlackCatalogQueryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("DJANGO_SETTINGS_MODULE", "mlai.settings")
        import django
        django.setup()

    def test_catalog_prefetches_shared_json_and_preserves_owner_filter(self):
        from integrations.models import SlackDmMirrorConversation
        from integrations.services.slack_chat_catalog import catalog_conversations

        with patch(
            "django.db.backends.base.base.BaseDatabaseWrapper.ensure_connection",
            side_effect=AssertionError("Query tests must not open a database"),
        ):
            source = SlackDmMirrorConversation.objects.filter(
                grant_id=17, status="live"
            ).select_related("grant__connection")
            query = catalog_conversations(source)
            sql, params = query.query.sql_with_params()
            self.assertNotIn("provider_metadata", sql)
            self.assertNotIn(" JOIN ", sql)
            self.assertIn("live", params)
            self.assertIn("grant_id", sql)
            self.assertEqual(query._prefetch_related_lookups, ("grant__connection",))
            self.assertEqual(query.query.where, source.query.where)
