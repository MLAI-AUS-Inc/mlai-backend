"""Tests for core.0059_purge_stale_content_types.

The migration removes django_content_type rows left behind by DeleteModel in
the 2026-09 table cleanup, mirroring each content-type FK's declared on_delete:
Permission CASCADEs, LogEntry SET_NULLs, and org_memory's PROTECTed generic
references must block the purge rather than be silently orphaned.
"""
from importlib import import_module

from django.contrib.admin.models import CHANGE, LogEntry
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Permission
from django.contrib.contenttypes.models import ContentType
from django.db import connection
from django.test import TestCase

migration = import_module("core.migrations.0059_purge_stale_content_types")
User = get_user_model()


class _Apps:
    """Stand-in for the migration-state registry used by the RunPython body."""

    def __init__(self, overrides=None):
        self._overrides = overrides or {}

    def get_model(self, app_label, model_name):
        key = (app_label, model_name)
        if key in self._overrides:
            return self._overrides[key]
        from django.apps import apps as live_apps

        return live_apps.get_model(app_label, model_name)


class _SchemaEditor:
    def __init__(self):
        self.connection = connection

    def quote_name(self, name):
        return connection.ops.quote_name(name)


class _BlockingManager:
    def filter(self, **kwargs):
        return self

    def count(self):
        return 3


class _BlockingModel:
    class _Meta:
        db_table = "org_memory_memoryreviewitem"

    _meta = _Meta()
    objects = _BlockingManager()


class StaleContentTypePurgeTests(TestCase):
    def setUp(self):
        self.apps = _Apps()
        self.schema_editor = _SchemaEditor()

    def _make_stale(self, app_label="hospital", model="medhackcase"):
        content_type = ContentType.objects.create(app_label=app_label, model=model)
        permission = Permission.objects.create(
            content_type=content_type,
            codename=f"add_{model}",
            name=f"Can add {model}",
        )
        return content_type, permission

    def _run(self, apps=None):
        migration.purge_stale_content_types(apps or self.apps, self.schema_editor)

    def test_purges_content_type_and_cascades_its_permissions(self):
        content_type, permission = self._make_stale()
        self._run()
        self.assertFalse(ContentType.objects.filter(pk=content_type.pk).exists())
        self.assertFalse(Permission.objects.filter(pk=permission.pk).exists())

    def test_purges_every_listed_pair(self):
        made = [self._make_stale(app, model)[0] for app, model in migration.STALE_CONTENT_TYPES]
        self._run()
        self.assertFalse(ContentType.objects.filter(pk__in=[c.pk for c in made]).exists())

    def test_admin_log_entries_survive_with_a_null_content_type(self):
        content_type, _ = self._make_stale()
        user = User.objects.create_user(email="ops@mlai.au", password="pw-for-tests-1")
        entry = LogEntry.objects.log_action(
            user_id=user.pk,
            content_type_id=content_type.pk,
            object_id="1",
            object_repr="Case 1",
            action_flag=CHANGE,
            change_message="edited",
        )
        self._run()
        entry.refresh_from_db()
        self.assertIsNone(entry.content_type_id)
        self.assertEqual(entry.object_repr, "Case 1")
        self.assertFalse(ContentType.objects.filter(pk=content_type.pk).exists())

    def test_a_model_that_exists_again_is_never_purged(self):
        live = ContentType.objects.get_for_model(User)
        pair = (live.app_label, live.model)
        original = migration.STALE_CONTENT_TYPES
        migration.STALE_CONTENT_TYPES = original + (pair,)
        try:
            self._run()
        finally:
            migration.STALE_CONTENT_TYPES = original
        self.assertTrue(ContentType.objects.filter(pk=live.pk).exists())

    def test_refuses_to_orphan_a_protected_generic_reference(self):
        content_type, permission = self._make_stale()
        apps = _Apps(overrides={("org_memory", "MemoryReviewItem"): _BlockingModel})
        with self.assertRaises(RuntimeError) as ctx:
            self._run(apps=apps)
        self.assertIn("stale content type", str(ctx.exception))
        # Nothing was deleted: the guard runs before any write.
        self.assertTrue(ContentType.objects.filter(pk=content_type.pk).exists())
        self.assertTrue(Permission.objects.filter(pk=permission.pk).exists())

    def test_is_idempotent_and_a_noop_when_nothing_is_stale(self):
        content_type, _ = self._make_stale()
        self._run()
        self._run()  # must not raise on the second pass
        self.assertFalse(ContentType.objects.filter(pk=content_type.pk).exists())
