"""Public event projection tests; no database or migration setup."""

from contextlib import ExitStack
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import patch

from django.core.cache import cache
from django.test import SimpleTestCase, override_settings

from community_chat.volunteer.events import public_event
from community_chat.volunteer.serializers import opportunity_dto
from integrations.services.luma import LumaAPIError, _public_upcoming_event


@override_settings(
    CACHES={"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}}
)
class VolunteerEventTests(SimpleTestCase):
    def setUp(self):
        cache.clear()

    def event(self, **changes):
        return dict(
            id="evt-public",
            name="AI Community Coffee",
            visibility="public",
            url="https://luma.com/coffee",
            timezone="Australia/Melbourne",
            start_at="2026-09-12T00:00:00Z",
            end_at="2026-09-12T02:00:00Z",
            description_md="Coffee, demos and questions.\n\nMeet your community.",
            **changes
        )

    def test_projects_public_description_without_host_metadata(self):
        raw = self.event(
            host_email="private@example.invalid",
            guests=[{"email": "private@example.invalid"}],
        )
        event = _public_upcoming_event(
            raw, now_utc=datetime(2026, 9, 1, tzinfo=timezone.utc)
        )
        self.assertEqual(event["description"], raw["description_md"])
        self.assertNotIn("guests", event)
        self.assertNotIn("host_email", event)

    def test_private_event_description_never_crosses_boundary(self):
        raw = self.event()
        raw["visibility"] = "private"
        self.assertIsNone(
            _public_upcoming_event(
                raw, now_utc=datetime(2026, 9, 1, tzinfo=timezone.utc)
            )
        )

    def test_structured_description_is_not_stringified(self):
        raw = self.event()
        raw.pop("description_md")
        raw["description"] = {"internal": "do not expose object representations"}
        self.assertEqual(
            _public_upcoming_event(
                raw, now_utc=datetime(2026, 9, 1, tzinfo=timezone.utc)
            )["description"],
            "",
        )

    @patch("community_chat.volunteer.events.LumaAttendeeReportService")
    def test_matches_exact_id_and_caches_calendar(self, service):
        service.return_value.list_upcoming_events.return_value = [
            {"id": "evt-public", "description": "From Luma"}
        ]
        self.assertEqual(public_event("evt-public")["description"], "From Luma")
        self.assertIsNone(public_event("evt-other"))
        service.return_value.list_upcoming_events.assert_called_once_with(limit=10)

    @patch("community_chat.volunteer.events.LumaAttendeeReportService")
    def test_unavailable_calendar_does_not_invent_event_copy(self, service):
        service.return_value.list_upcoming_events.side_effect = LumaAPIError(
            "Unavailable"
        )
        self.assertIsNone(public_event("evt-public"))
        self.assertIsNone(public_event("evt-public"))
        self.assertEqual(service.return_value.list_upcoming_events.call_count, 1)

    def opportunity(self, event):
        record = SimpleNamespace(
            pk="opportunity",
            kind="event",
            action_key="help_at_event",
            event_id="evt-public",
            title="Generic invitation",
            purpose="Make everyone feel welcome",
            description="Old generic copy",
            learning="",
            guide=None,
            reviewer=None,
            source={},
            project_id=None,
            starts_at=None,
            ends_at=None,
            reward_microroo=6000000,
            reward_max_microroo=18000000,
            recommended_level=0,
            status="open",
            version=1,
        )
        with ExitStack() as stack:
            replacements = {
                "public_event": event,
                "active_policy": {"help_at_event": {"requires_attendance": False}},
                "channels": {"volunteer": "configured-volunteer-channel"},
                "guide_contact": {},
                "member_dto": {},
                "public_source": {},
                "capabilities": {"can_request": False},
            }
            for name, value in replacements.items():
                stack.enter_context(
                    patch(
                        "community_chat.volunteer.serializers." + name,
                        return_value=value,
                    )
                )
            return opportunity_dto(record, None)

    def test_opportunity_uses_luma_copy_and_dedicated_volunteer_channel(self):
        data = self.opportunity(
            {
                "name": "AI Community Coffee",
                "description": "Exact Luma details.\n\nBring questions.",
                "url": "https://luma.com/coffee",
                "start_at": "2026-09-12T00:00:00Z",
            }
        )
        self.assertEqual(data["title"], "AI Community Coffee")
        self.assertEqual(data["description"], "Exact Luma details.\n\nBring questions.")
        self.assertEqual(data["purpose"], "")
        self.assertEqual(data["event_url"], "https://luma.com/coffee")
        self.assertEqual(data["volunteer_channel_id"], "configured-volunteer-channel")

    def test_missing_event_never_falls_back_to_generic_helping_copy(self):
        data = self.opportunity(None)
        self.assertEqual(data["description"], "")
        self.assertEqual(data["purpose"], "")
        self.assertEqual(data["title"], "Generic invitation")
