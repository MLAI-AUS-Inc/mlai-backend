
import os
import django
from datetime import date

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "mlai.settings")
django.setup()

from points.services import CoworkingService
from core.models import User
from points.models import CoworkingBooking, Ledger

def reproduce():
    # Create a dummy user
    user, created = User.objects.get_or_create(
        email='test@example.com', 
        defaults={'slack_id': 'U_TEST', 'first_name': 'Test'}
    )
    
    # Ensure user has points
    from points.services import PointsService
    PointsService.award(user, 100, 'MANUAL', 'Test funding', 'U_ADMIN', 'funding_test_user')
    
    # Book for today
    today = date.today()
    
    # Clean up previous bookings
    CoworkingBooking.objects.filter(user=user, date=today).delete()
    
    print(f"Booking for {today}...")
    try:
        booking = CoworkingService.book(
            user=user,
            booking_date=today,
            created_by_slack_id='U_TEST'
        )
        print(f"Booking successful. Cost: {booking.points_cost}")
        
        if booking.points_cost == 1:
            print("ISSUE REPRODUCED: Cost is 1 point.")
        else:
            print(f"Cost is {booking.points_cost} points.")
            
    except Exception as e:
        print(f"Booking failed: {e}")

if __name__ == "__main__":
    reproduce()
