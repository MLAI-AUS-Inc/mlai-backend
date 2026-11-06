"""Safety ordering and payload binding without database or network access."""
import unittest
from types import SimpleNamespace
from unittest.mock import patch
import uuid

from mentoring import instant_booking as instant
from mentoring.service import MentoringError


class InstantBookingUnitTests(unittest.TestCase):
    def test_existing_matching_attempt_returns_before_quote_or_provider_validation(self):
        user=SimpleNamespace(pk=10)
        existing=object()
        with patch.object(instant,'_previous',return_value=existing), \
                patch.object(instant,'_load_quote',side_effect=AssertionError('expired quote revalidated')), \
                patch.object(instant,'_calendars',side_effect=AssertionError('provider queried')):
            result=instant.book(founder=user,quote='previous-quote',brief={'a':'b'},client_request_id=str(uuid.uuid4()))
        self.assertIs(result,existing)

    def test_digest_captures_brief_and_quote_changes(self):
        a=instant._json_digest({'quote':'one','brief':{'stage':'idea'}})
        self.assertEqual(a,instant._json_digest({'brief':{'stage':'idea'},'quote':'one'}))
        self.assertNotEqual(a,instant._json_digest({'quote':'one','brief':{'stage':'changed'}}))
        self.assertNotEqual(a,instant._json_digest({'quote':'two','brief':{'stage':'idea'}}))

    def test_cross_account_quote_is_rejected(self):
        with patch.object(instant.signing,'loads',return_value={'founder_id':99}):
            with self.assertRaises(MentoringError) as caught:
                instant._load_quote('token',SimpleNamespace(pk=10))
        self.assertEqual(caught.exception.code,'invalid_quote')

    def test_invalid_or_expired_quote_is_recoverable(self):
        with self.assertRaises(MentoringError) as caught:
            instant._load_quote('not-signed',SimpleNamespace(pk=10))
        self.assertEqual(caught.exception.code,'quote_expired')


class ReservationBackfillUnitTests(unittest.TestCase):
    def backfill(self, rows):
        import importlib
        from unittest.mock import MagicMock
        module=importlib.import_module('mentoring.migrations.0003_instant_bookings')
        session_model=MagicMock();reservation_model=MagicMock()
        session_model.objects.using.return_value.filter.return_value.select_related.return_value.order_by.return_value.iterator.return_value=iter(rows)
        apps=MagicMock();apps.get_model.side_effect=[session_model,reservation_model]
        editor=SimpleNamespace(connection=SimpleNamespace(alias='synthetic'))
        return module,apps,editor,reservation_model

    def test_backfill_uses_legacy_meeting_times_without_new_buffers(self):
        from datetime import datetime,timedelta,timezone
        start=datetime(2026,11,8,10,tzinfo=timezone.utc)
        row=SimpleNamespace(pk=uuid.uuid4(),founder_id=2,mentor=SimpleNamespace(user_id=1),starts_at=start,ends_at=start+timedelta(minutes=30))
        module,apps,editor,reservation=self.backfill([row])
        module.backfill_reservations(apps,editor)
        self.assertEqual(reservation.call_count,2)
        for call in reservation.call_args_list:
            self.assertEqual(call.kwargs['starts_at'],row.starts_at)
            self.assertEqual(call.kwargs['ends_at'],row.ends_at)
        reservation.objects.using.return_value.bulk_create.assert_called_once()

    def test_cross_role_legacy_overlap_aborts_before_any_backfill_write(self):
        from datetime import datetime,timedelta,timezone
        start=datetime(2026,11,8,10,tzinfo=timezone.utc)
        rows=[SimpleNamespace(pk=uuid.uuid4(),founder_id=2,mentor=SimpleNamespace(user_id=1),starts_at=start,ends_at=start+timedelta(minutes=30)),
              SimpleNamespace(pk=uuid.uuid4(),founder_id=1,mentor=SimpleNamespace(user_id=3),starts_at=start+timedelta(minutes=15),ends_at=start+timedelta(minutes=45))]
        module,apps,editor,reservation=self.backfill(rows)
        with self.assertRaisesRegex(RuntimeError,'overlapping sessions'):
            module.backfill_reservations(apps,editor)
        reservation.objects.using.return_value.bulk_create.assert_not_called()


class WorkerBudgetUnitTests(unittest.TestCase):
    def test_quarantined_cleanup_backoff_is_capped(self):
        first=SimpleNamespace(status='cancelled',cleanup_pending=True,generation=4)
        later=SimpleNamespace(status='cancelled',cleanup_pending=True,generation=5)
        old=SimpleNamespace(status='cancelled',cleanup_pending=True,generation=100)
        self.assertEqual(instant._retry_wait_seconds(first),30)
        self.assertEqual(instant._retry_wait_seconds(later),60)
        self.assertEqual(instant._retry_wait_seconds(old),3600)

    def test_tick_stops_between_provider_operations_after_wall_clock_budget(self):
        from unittest.mock import MagicMock
        attempts=MagicMock()
        attempts.filter.return_value.annotate.return_value.order_by.return_value.values_list.return_value.__getitem__.return_value=[1,2,3]
        sessions=MagicMock()
        sessions.filter.return_value.order_by.return_value.values_list.return_value.__getitem__.return_value=[]
        with patch.object(instant.MentorBookingAttempt,'objects',attempts),                 patch.object(instant.MentorSession,'objects',sessions),                 patch.object(instant,'monotonic',side_effect=[0,0,40]),                 patch.object(instant,'process_attempt') as process:
            result=instant.process_due_bookings(limit=5,time_budget_seconds=30)
        process.assert_called_once_with(1)
        self.assertEqual(result,{'processed':1})
