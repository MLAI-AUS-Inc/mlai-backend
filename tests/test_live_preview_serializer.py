from django.test import TestCase

from content_factory.vibe_marketing_views import _live_preview_from_run
from workflow_runs.models import ContentFactoryRun


class LivePreviewSerializerTests(TestCase):
    def _run_with_live_preview(self, payload):
        return ContentFactoryRun(
            run_id="run-preview-serializer",
            workflow="confirmed_topic",
            result={"livePreview": payload},
        )

    def test_content_only_fallback_fields_pass_through(self):
        run = self._run_with_live_preview(
            {
                "available": True,
                "status": "running",
                "previewUrl": "https://preview.example/articles/test",
                "exactRender": False,
                "previewMode": "content_only",
                "previewQuality": "content_only",
                "previewBanner": "Exact preview unavailable - showing content-only preview.",
                "fallbackReason": "generated_article_content_module_missing",
                "renderConfidence": "fallback",
            }
        )

        serialized = _live_preview_from_run(run)

        self.assertEqual(serialized["previewQuality"], "content_only")
        self.assertEqual(
            serialized["previewBanner"],
            "Exact preview unavailable - showing content-only preview.",
        )
        self.assertEqual(serialized["previewMode"], "content_only")
        self.assertEqual(serialized["fallbackReason"], "generated_article_content_module_missing")
        self.assertFalse(serialized["exactRender"])
        # The preview URL is rewritten through the backend live-preview proxy;
        # content-only previews must keep a usable URL after that rewrite.
        self.assertIn("/live-preview/proxy/articles/test", serialized["previewUrl"])

    def test_snake_case_fallback_fields_pass_through(self):
        run = self._run_with_live_preview(
            {
                "available": True,
                "preview_url": "https://preview.example/articles/test",
                "preview_quality": "content_only",
                "preview_banner": "Banner from snake_case payload.",
            }
        )

        serialized = _live_preview_from_run(run)

        self.assertEqual(serialized["previewQuality"], "content_only")
        self.assertEqual(serialized["previewBanner"], "Banner from snake_case payload.")

    def test_exact_preview_has_empty_banner_fields(self):
        run = self._run_with_live_preview(
            {
                "available": True,
                "previewUrl": "https://preview.example/articles/test",
                "exactRender": True,
            }
        )

        serialized = _live_preview_from_run(run)

        self.assertEqual(serialized["previewQuality"], "")
        self.assertEqual(serialized["previewBanner"], "")
