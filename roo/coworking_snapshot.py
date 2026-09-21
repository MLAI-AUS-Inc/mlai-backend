"""Read-only active booking list, independent of the coworking report."""
from datetime import date

from .models import CoworkingBooking


def build_booking_snapshot(day: date) -> dict:
    users = {}
    bookings = CoworkingBooking.objects.filter(date=day, status='booked').select_related('user')
    for booking in bookings:
        user = booking.user
        name = ' '.join((user.full_name or '').split())
        users[str(user.pk)] = {'user_id': str(user.pk), 'name': name or f'Member {user.pk}'}
    people = sorted(users.values(), key=lambda row: (row['name'].casefold(), row['user_id']))
    return {'date': day.isoformat(), 'count': len(people), 'people': people}
