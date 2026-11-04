"""Combining approved calendars never multiplies guests or borrows access."""
from unittest.mock import Mock, patch
from django.test import SimpleTestCase
from crm.source_accounts import CombinedLumaSource
from integrations.services.luma import LumaAPIError


class CombinedCalendarTests(SimpleTestCase):
    def source(self, rows):
        source = Mock()
        source.list_crm_events.return_value = rows
        source.list_guests.return_value = [{"guest":{"id":"one"}}]
        return source

    def test_view_listing_and_host_account_collapse_to_one_event(self):
        a = self.source([{"id":"event","platform":"luma","access":"view","name":"Sydney"}])
        b = self.source([{"id":"event","platform":"luma","access":"manage","name":"Sydney"}])
        source = CombinedLumaSource({"calendar":a,"host":b})
        rows = source.list_crm_events()
        self.assertEqual(len(rows),1)
        self.assertEqual(rows[0]["access"],"manage")
        self.assertEqual(set(rows[0]["crm_sources"]),{"calendar","host"})
        self.assertEqual(len(source.list_guests(event_id="event")),1)
        a.list_guests.assert_not_called()
        b.list_guests.assert_called_once()

    def test_external_and_view_only_events_never_read_guests(self):
        a = self.source([{"id":"external","platform":"external","access":"manage"},
                         {"id":"view","platform":"luma","access":"view"}])
        source = CombinedLumaSource({"calendar":a})
        self.assertEqual(len(source.list_crm_events()),2)
        for event_id in ("external","view"):
            with self.assertRaises(LumaAPIError): source.list_guests(event_id=event_id)
        a.list_guests.assert_not_called()

    def test_source_failure_cannot_claim_a_complete_catalogue(self):
        a = self.source([{"id":"a"}]); b = self.source([])
        b.list_crm_events.side_effect = LumaAPIError("unavailable",status_code=503)
        source = CombinedLumaSource({"a":a,"b":b})
        with self.assertRaises(LumaAPIError): source.list_crm_events()
        a.list_guests.assert_not_called(); b.list_guests.assert_not_called()

    def test_resume_does_not_use_disabled_account_or_unrelated_key(self):
        a = self.source([])
        source = CombinedLumaSource({"a":a})
        with patch("crm.models.CrmEvent.objects") as events:
            events.filter.return_value.values_list.return_value.first.return_value = {
                "crm_sources":{"disabled":{"access":"manage","platform":"luma"}}}
            with self.assertRaises(LumaAPIError): source.list_guests(event_id="event")
        a.list_guests.assert_not_called()
