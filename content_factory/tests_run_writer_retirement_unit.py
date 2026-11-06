"""The bounded writer gate detects new sites and removed guard context."""
from copy import deepcopy
from pathlib import Path
from tempfile import TemporaryDirectory
from django.test import SimpleTestCase
from scripts.audit_run_writers import inventory
from scripts.check_run_writer_retirement import check

SOURCE = '''from workflow_runs.models import ContentFactoryRun

def old_callback(run):
    if authoritative_run(run):
        return
    ContentFactoryRun.objects.filter(pk=run.pk).update(status="failed")
'''


class RunWriterRetirementTests(SimpleTestCase):
    def test_same_reviewed_context_passes_and_changed_guard_requires_review(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "content_factory").mkdir()
            path = root / "content_factory/callback.py"
            path.write_text(SOURCE)
            current = inventory(root)
            self.assertEqual(len(current["sites"]), 1)
            manifest = {"schema_version": 1, "complete_for_reviewed_scope": True,
                "review_scope": "Exact bounded synthetic callback witness.", "sites": [
                    {**current["sites"][0], "classification": "legacy_fenced",
                    "reason": "The callback preserves the admitted owner.", "guard": "Fresh upstream proof."}]}
            self.assertEqual(check(manifest, current), [])
            path.write_text(SOURCE.replace("    if authoritative_run(run):\n        return\n", ""))
            self.assertIn("authority context requires review", " ".join(check(manifest, inventory(root))))
            path.write_text(SOURCE + '\ndef second(run):\n    run.save(update_fields=["updated_at"])\n')
            self.assertIn("Unreviewed Run writer", " ".join(check(manifest, inventory(root))))
            incomplete = deepcopy(manifest)
            incomplete["complete_for_reviewed_scope"] = False
            self.assertIn("incomplete", " ".join(check(incomplete, current)))
