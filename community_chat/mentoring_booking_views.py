"""Explicit instant-booking endpoints; legacy request semantics stay unchanged."""
from django.db import transaction
from django.utils.decorators import method_decorator
from rest_framework.response import Response
from mentoring import instant_booking
from .mentoring_views import MentoringBaseView, _body


@method_decorator(transaction.non_atomic_requests, name='dispatch')
class InstantBookingBaseView(MentoringBaseView):
    """Provider diagnostics are redacted before returning to a member."""
    def handle_exception(self, exc):
        from mentoring.calendars.errors import CalendarError
        if isinstance(exc, CalendarError):
            return Response({'code': 'calendar_unavailable', 'detail': 'The mentor calendar cannot be checked right now. Please try again.'}, status=503)
        return super().handle_exception(exc)


class MentorBookingQuoteView(InstantBookingBaseView):
    """Validate and quote a member-selected slot without reserving or charging it."""
    def post(self, request):
        data = _body(request)
        return Response(instant_booking.make_quote(founder=request.user,
            mentor_id=data.get('mentor_id'), starts_at=data.get('starts_at'),
            expected_price=data.get('expected_price')))


class MentorInstantBookingView(InstantBookingBaseView):
    """Explicit payment consent lives on this route, never the old request route."""
    def post(self, request):
        data = _body(request)
        attempt = instant_booking.book(founder=request.user, quote=data.get('quote'),
            brief=data.get('brief'), client_request_id=data.get('client_request_id'))
        # Durable worker owns the remote operation. Request does no provider I/O
        # inside ATOMIC_REQUESTS or nested database transactions.
        return Response(instant_booking.serialize_attempt(attempt, request.user),
                        status=201 if attempt.status == 'confirmed' else 202)


class MentorBookingAttemptView(InstantBookingBaseView):
    """A persisted attempt remains recoverable after navigation or quote expiry."""
    def get(self, request, attempt_id):
        attempt = instant_booking.get_attempt(attempt_id, request.user)
        return Response(instant_booking.serialize_attempt(attempt, request.user))


class MentorBookingAttemptCancelView(InstantBookingBaseView):
    """Cancel unconfirmed work and refund atomically while cleanup continues."""
    def post(self, request, attempt_id):
        attempt = instant_booking.cancel_attempt(attempt_id, request.user)
        return Response(instant_booking.serialize_attempt(attempt, request.user))


class MentorBookingAttemptListView(InstantBookingBaseView):
    """Recover participant-scoped pending work after a lost creation response."""
    def get(self, request):
        return Response({'attempts': instant_booking.list_attempts(request.user)})
