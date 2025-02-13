# app/views.py
import csv
import logging
import datetime
from django.http import JsonResponse
from django.views.decorators.csrf import csrf_exempt
from .serializers import MyTokenObtainPairSerializer
from .models import Submission
from rest_framework.permissions import AllowAny
from rest_framework.decorators import permission_classes
import logging
from dotenv import load_dotenv
from django.db import transaction
from django.http import JsonResponse
from django.views.decorators.csrf import csrf_exempt
from django.contrib.auth import get_user_model
from rest_framework import permissions, status
from rest_framework.response import Response
from rest_framework.permissions import IsAuthenticated
from rest_framework.views import APIView
from rest_framework_simplejwt.tokens import RefreshToken
from rest_framework.permissions import AllowAny
from rest_framework.decorators import api_view, permission_classes
from rest_framework_simplejwt.views import TokenObtainPairView, TokenRefreshView
from .models import User, Prediction
from .customerio_utils import generate_magic_link, send_magic_link_email, verify_magic_link



load_dotenv()
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)  


# Map the original state_label (0-17) to your 4 classes (0-3)
def map_state_label(state_label):
    mapping = {
        0: 0,
        9: 0,
        10: 0,
        1: 1,
        2: 1,
        3: 1,
        4: 1,
        5: 1,
        6: 1,
        7: 1,
        8: 1,
        11: 2,
        12: 2,
        13: 2,
        14: 2,
        15: 2,
        16: 3,
    }
    return mapping.get(int(state_label), -1)

# Load the full ground truth CSV as a list of dictionaries.
def load_ground_truth():
    gt_rows = []
    with open('./hospital/Eval_Labels.csv', 'r') as f:
        reader = csv.DictReader(f)
        for row in reader:
            gt_rows.append(row)
    return gt_rows

def custom_score(true_labels, pred_labels):
    # (Your custom scoring logic remains unchanged.)
    normal = {0, 9, 10}
    warning = {1, 2, 3, 4, 5, 6, 7, 8}
    crisis = {11, 12, 13, 14, 15}
    total_score = 0
    for t, p in zip(true_labels, pred_labels):
        if t in normal:
            total_score += 0 if p in normal else -2
        elif t in warning:
            if p in warning:
                total_score += 2
            elif p in crisis:
                total_score -= 1
            else:
                total_score -= 3
        elif t in crisis:
            if p in crisis:
                total_score += 3
            elif p in warning:
                total_score -= 3
            else:
                total_score -= 10
    return total_score

@csrf_exempt
@permission_classes([IsAuthenticated])
def submit_predictions(request):
    if request.method == 'POST':
        # Get the authenticated user if available; otherwise, use participant_name from POST.
        user = request.user if request.user.is_authenticated else None
        participant_name = request.POST.get('participant_name', 'Anonymous')
        
        # Get the CSV file with predicted labels.
        csv_file = request.FILES.get('predictions_csv')
        if not csv_file:
            return JsonResponse({'error': 'No CSV file uploaded'}, status=400)
        
        # Parse predictions CSV (assume it has a header and one column: predicted_label)
        pred_labels = []
        file_data = csv_file.read().decode('utf-8').splitlines()
        reader = csv.reader(file_data)
        next(reader, None)  # skip header
        for row in reader:
            pred_labels.append(int(row[0]))
        
        # Load full ground truth rows (each row is a dict with all info)
        gt_rows = load_ground_truth()
        
        # Extract true labels (using the state_label from CSV and mapping them)
        true_labels = [map_state_label(row['state_label']) for row in gt_rows]
        score = custom_score(true_labels, pred_labels)
        
        # Create a Submission instance linked to the user if available.
        submission = Submission.objects.create(
            user=user,
            participant_name=participant_name,
            score=score
        )
        
        # Loop through each row and save a Prediction record with all desired fields.
        for idx, (pred, gt) in enumerate(zip(pred_labels, gt_rows), start=1):
            # Parse timestamp (adjust format as needed)
            ts = datetime.datetime.strptime(gt['timestamp'], '%Y-%m-%d %H:%M:%S')
            Prediction.objects.create(
                submission=submission,
                row_id=idx,
                predicted_label=pred,
                correct_label=map_state_label(gt['state_label']),
                timestamp=ts,
                diastolic_bp=float(gt['diastolic_bp']),
                systolic_bp=float(gt['systolic_bp']),
                heart_rate=float(gt['heart_rate']),
                respiratory_rate=float(gt['respiratory_rate']),
                oxygen_saturation=float(gt['oxygen_saturation'])
            )
        
        return JsonResponse({
            'message': 'Submission scored successfully',
            'participant_name': participant_name,
            'score': score
        })
    return JsonResponse({'error': 'Invalid request'}, status=405)


@csrf_exempt
@permission_classes([IsAuthenticated])  # Switch to IsAuthenticated if you require auth
def get_submission(request):
    if request.method == 'GET':
        if not request.user.is_authenticated:
            return JsonResponse({'error': 'Authentication required'}, status=401)
        submission = Submission.objects.filter(user=request.user).order_by('-submitted_at').first()
        if not submission:
            return JsonResponse({'error': 'No submission found'}, status=404)
        
        predictions = submission.predictions.all().order_by('row_id')
        submission_data = {
            'submission_id': submission.id,
            'participant_name': submission.participant_name,
            'score': submission.score,
            'submitted_at': submission.submitted_at.isoformat(),
            'predictions': [{
                'row_id': p.row_id,
                'predicted_label': p.predicted_label,
                'correct_label': p.correct_label,
                'timestamp': p.timestamp.isoformat() if p.timestamp else None,
                'diastolic_bp': p.diastolic_bp,
                'systolic_bp': p.systolic_bp,
                'heart_rate': p.heart_rate,
                'respiratory_rate': p.respiratory_rate,
                'oxygen_saturation': p.oxygen_saturation,
            } for p in predictions]
        }
        return JsonResponse(submission_data)
    return JsonResponse({'error': 'Invalid request'}, status=405)


class SendMagicLinkView(APIView):
    authentication_classes = []
    permission_classes = [permissions.AllowAny]

    def post(self, request):
        data = request.data

        email = data.get('email')
        if not email:
            return Response({"error": "Email is required."}, status=status.HTTP_400_BAD_REQUEST)

        full_name = data.get('fullName', '')
        role = data.get('role', 'participant')

        try:
            with transaction.atomic():
                # Check if user already exists
                user, created = User.objects.get_or_create(email=email)

                if created:
                    # New user, set additional fields
                    user.full_name = full_name
                    user.role = role
                    user.is_active = False  # User is inactive until they verify email
                    user.save()
                    logger.info(f"Created new user: {email}")
                else:
                    # Existing user, update fields if necessary
                    updated = False
                    if full_name and user.full_name != full_name:
                        user.full_name = full_name
                        updated = True
                    if user.role != role:
                        user.role = role
                        updated = True
                    if updated:
                        user.save()
                        logger.info(f"Updated user information for: {email}")
                    else:
                        logger.info(f"No updates needed for existing user: {email}")

                # Generate magic link and send email
                magic_link = generate_magic_link(user)
                send_magic_link_email(user, magic_link)
                logger.info(f"Sent magic link to: {email}")

                return Response({"message": "Magic link sent to your email."}, status=status.HTTP_200_OK)
        except Exception as e:
            logger.exception(f"Error in SendMagicLinkView: {str(e)}")
            return Response({"error": "An error occurred while processing your request."}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

# Custom Token View
class MyTokenObtainPairView(TokenObtainPairView):
    serializer_class = MyTokenObtainPairSerializer

    def post(self, request, *args, **kwargs):
        response = super().post(request, *args, **kwargs)
        access_token = response.data.get('access')
        refresh_token = response.data.get('refresh')

        response.set_cookie(
            key='access_token',
            value=access_token,
            httponly=True,
            secure=False,  # True in production with HTTPS
            samesite='None',  # 'Lax' is acceptable for same-origin requests
        )
        response.set_cookie(
            key='refresh_token',
            value=refresh_token,
            httponly=True,
            secure=False,  # True in production with HTTPS
            samesite='None',
        )
        # Remove tokens from response body
        response.data = {}
        return response
    
class CookieTokenRefreshView(TokenRefreshView):
    def post(self, request, *args, **kwargs):
        refresh_token = request.COOKIES.get('refresh_token')
        if refresh_token is None:
            return Response({'error': 'Refresh token not found in cookies'}, status=400)
        serializer = self.get_serializer(data={'refresh': refresh_token})
        try:
            serializer.is_valid(raise_exception=True)
        except:
            return Response({'error': 'Invalid token'}, status=401)
        access_token = serializer.validated_data['access']
        response = Response()
        response.set_cookie(
            key='access_token',
            value=access_token,
            httponly=True,
            secure=True,  # Must be True when SameSite=None
            samesite='None',
            path='/',
        )
        return response

    
@csrf_exempt
@api_view(['POST'])
@permission_classes([IsAuthenticated])
def logout_view(request):
    try:
        # Create response object
        response = Response({'message': 'Logged out successfully'}, status=status.HTTP_200_OK)
        
        # Delete authentication cookies
        response.delete_cookie('access_token', path='/')
        response.delete_cookie('refresh_token', path='/')
        
        # Delete any session-related cookies
        response.delete_cookie('sessionid', path='/')
        response.delete_cookie('csrftoken', path='/')
        
        return response
    except Exception as e:
        return Response({'error': 'Logout failed'}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)
    

class MagicLinkVerifyView(APIView):
    authentication_classes = []
    permission_classes = [AllowAny]

    def get(self, request):
        token = request.query_params.get('token')
        logger.info("Received magic link verification request.")
        email = verify_magic_link(token)
        if email:
            try:
                user = User.objects.get(email=email)
                if not user.is_active:
                    user.is_active = True
                    user.save()
                    logger.info(f"Activated user account for {email}")

                # Generate tokens
                refresh = RefreshToken.for_user(user)
                access_token = str(refresh.access_token)
                refresh_token = str(refresh)

                # Determine the user's avatar URL from the ProfessionalProfile
                default_avatar_url = (
                    "https://firebasestorage.googleapis.com/v0/b/a-duet.appspot.com/o/"
                    "default_avatar.jpg?alt=media&token=c77bbde6-e898-4acd-8bb9-29d210064153"
                )

                # Prepare the response
                response = Response({
                    'message': 'Login successful',
                    'user': {
                        'email': user.email,
                        'full_name': user.full_name,
                        'role': user.role,
                        'is_superuser': user.is_superuser,
                        'is_active': user.is_active,
                    }
                }, status=status.HTTP_200_OK)

                # Set cookies with explicit configuration
                response.set_cookie(
                    key='access_token',
                    value=access_token,
                    max_age=86400,  # 1 day
                    httponly=True,
                    secure=True,  # Set to True in production
                    samesite='None',
                    path='/',
                )
                response.set_cookie(
                    key='refresh_token',
                    value=refresh_token,
                    max_age=172800,  # 2 days
                    httponly=True,
                    secure=True,  # Set to True in production
                    samesite='None',
                    path='/',
                )

                return response

            except User.DoesNotExist:
                logger.error(f"User with email {email} does not exist.")
                return Response({"error": "User does not exist."}, 
                              status=status.HTTP_400_BAD_REQUEST)
        else:
            logger.warning("Invalid or expired magic link token.")
            return Response({"error": "Invalid or expired token."}, 
                          status=status.HTTP_400_BAD_REQUEST)

class CurrentUserView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        user = request.user
        if not user.is_authenticated:
            return Response({'error': 'Not authenticated'}, status=status.HTTP_401_UNAUTHORIZED)
            
        data = {
            'full_name': user.full_name,
            'email': user.email,
            'role': user.role,
            'is_superuser': user.is_superuser,
        }

        
        return Response(data, status=status.HTTP_200_OK)
