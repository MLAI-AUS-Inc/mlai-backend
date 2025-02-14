from django.db import models
from django.contrib.auth.models import AbstractBaseUser, PermissionsMixin, BaseUserManager
from django.utils import timezone

class CustomUserManager(BaseUserManager):
    def create_user(self, email, role='professional', password=None, **extra_fields):
        if not email:
            raise ValueError('Email is required.')
        email = self.normalize_email(email)
        user = self.model(email=email, role=role, **extra_fields)
        if password:
            user.set_password(password)
        else:
            user.set_unusable_password()
        user.is_active = True
        user.save(using=self._db)
        return user

    def create_superuser(self, email, role='admin', password=None, **extra_fields):
        extra_fields.setdefault('is_staff', True)
        extra_fields.setdefault('is_superuser', True)
        return self.create_user(email, role, password, **extra_fields)

class User(AbstractBaseUser, PermissionsMixin):
    ROLE_CHOICES = (
        ('admin', 'Admin'),
        ('participant', 'Participant'),
    )
    email = models.EmailField(unique=True)
    full_name = models.CharField(max_length=255, blank=True)
    role = models.CharField(max_length=20, choices=ROLE_CHOICES, default='participant')
    is_superuser = models.BooleanField(default=False)
    is_active = models.BooleanField(default=True)
    has_team = models.BooleanField(default=False)
    is_staff = models.BooleanField(default=False)  # Required for admin interface
    date_joined = models.DateTimeField(default=timezone.now)

    USERNAME_FIELD = 'email'
    REQUIRED_FIELDS = ['role']

    objects = CustomUserManager()

    def __str__(self):
        return self.email

class Team(models.Model):
    # team_id is now a positive integer unique field
    team_id = models.PositiveIntegerField(unique=True, blank=True, null=True)
    team_name = models.CharField(max_length=100)
    members = models.ManyToManyField(User, related_name='teams')

    def save(self, *args, **kwargs):
        if self.team_id is None:
            # Automatically assign next available team_id starting from 1
            last_team = Team.objects.all().order_by('-team_id').first()
            if last_team and last_team.team_id < 100:
                self.team_id = last_team.team_id + 1
            else:
                # If no team exists, assign 1
                self.team_id = 1
        # Validate team_id is between 1 and 100
        if self.team_id < 1 or self.team_id > 100:
            raise ValueError("team_id must be between 1 and 100")
        super().save(*args, **kwargs)

    def __str__(self):
        return f"{self.team_name} (ID: {self.team_id})"

class Submission(models.Model):
    # Associate a submission with a user and a team (if available)
    user = models.ForeignKey(User, on_delete=models.CASCADE)
    team = models.ForeignKey(Team, on_delete=models.CASCADE, null=True, blank=True)
    participant_name = models.CharField(max_length=100)
    score = models.FloatField()
    accuracy = models.FloatField(default=0.0)  # Overall accuracy
    submitted_at = models.DateTimeField(auto_now_add=True)

class Prediction(models.Model):
    submission = models.ForeignKey(Submission, related_name='predictions', on_delete=models.CASCADE)
    row_id = models.IntegerField()  # row number (order in the CSV)
    predicted_label = models.IntegerField()
    correct_label = models.IntegerField()
    timestamp = models.DateTimeField(null=True, blank=True)
    diastolic_bp = models.FloatField(null=True, blank=True)
    systolic_bp = models.FloatField(null=True, blank=True)
    heart_rate = models.FloatField(null=True, blank=True)
    respiratory_rate = models.FloatField(null=True, blank=True)
    oxygen_saturation = models.FloatField(null=True, blank=True)
