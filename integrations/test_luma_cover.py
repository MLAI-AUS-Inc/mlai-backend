"""Pure contract tests: no database setup or migrations required."""

from datetime import datetime, timezone
from unittest import TestCase

from integrations.services.luma import _public_event_cover_url, _public_upcoming_event


class LumaCoverTests(TestCase):
    def test_public_cover_survives_event_projection(self):
        cover = "https://images.lumacdn.com/event-cover.png"
        event = dict(
            id="event",
            name="Meetup",
            visibility="public",
            url="https://luma.com/meetup",
            start_at="2099-09-10T08:00:00Z",
            end_at="2099-09-10T09:00:00Z",
            timezone="Australia/Melbourne",
            cover_url=cover,
        )
        self.assertEqual(
            _public_upcoming_event(
                event, now_utc=datetime(2026, 1, 1, tzinfo=timezone.utc)
            )["cover_url"],
            cover,
        )
        event["visibility"] = "private"
        self.assertIsNone(
            _public_upcoming_event(
                event, now_utc=datetime(2026, 1, 1, tzinfo=timezone.utc)
            )
        )

    def test_missing_or_non_public_images_are_omitted(self):
        for value in [
            None,
            "",
            {},
            "http://images.lumacdn.com/a.png",
            "https://example.com/a.png",
            "https://images.lumacdn.com.evil.test/a.png",
            "https://user:secret@images.lumacdn.com/a.png",
            "https://images.lumacdn.com:invalid/a.png",
        ]:
            with self.subTest(value=value):
                self.assertEqual(_public_event_cover_url(value), "")
