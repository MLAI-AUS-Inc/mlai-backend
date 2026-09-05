import uuid

from django.db import models
from django.contrib.auth.models import AbstractBaseUser, PermissionsMixin, BaseUserManager
from django.db.models.functions import Lower
from django.utils import timezone


class CustomUserManager(BaseUserManager):
    @classmethod
    def normalize_email(cls, email):
        """Return the canonical account identifier used by every auth flow."""

        return super().normalize_email(str(email or "").strip()).lower()

    def create_user(self, email, role=None, password=None, **extra_fields):
        if not email:
            raise ValueError('Email is required.')
        email = self.normalize_email(email)
        user = self.model(email=email, **extra_fields)
        if password:
            user.set_password(password)
        else:
            user.set_unusable_password()
        user.is_active = True
        user.save(using=self._db)
        return user

    def create_superuser(self, email, role=None, password=None, **extra_fields):
        extra_fields.setdefault('is_staff', True)
        extra_fields.setdefault('is_superuser', True)
        return self.create_user(email, role, password, **extra_fields)

class User(AbstractBaseUser, PermissionsMixin):
    email = models.EmailField(unique=True)
    community_chat_profile_id = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    slack_id = models.CharField(max_length=50, blank=True, null=True, unique=True)
    first_name = models.CharField(max_length=150, blank=True)
    last_name = models.CharField(max_length=150, blank=True)
    
    @property
    def full_name(self):
        return f"{self.first_name} {self.last_name}".strip()
    
    @full_name.setter
    def full_name(self, value):
        parts = value.strip().split(' ', 1)
        self.first_name = parts[0]
        if len(parts) > 1:
            self.last_name = parts[1]
        else:
            self.last_name = ''
    phone = models.CharField(max_length=20, blank=True, null=True)
    about = models.TextField(blank=True, null=True)
    is_superuser = models.BooleanField(default=False)
    is_active = models.BooleanField(default=True)
    is_staff = models.BooleanField(default=False)  # Required for admin interface
    date_joined = models.DateTimeField(default=timezone.now)
    updated_at = models.DateTimeField(auto_now=True, null=True)
    email_verified_at = models.DateTimeField(blank=True, null=True)
    password_set_at = models.DateTimeField(blank=True, null=True)
    auth_version = models.PositiveIntegerField(default=1)
    avatar_url = models.URLField(blank=True, null=True)
    personas = models.JSONField(default=list, blank=True)

    USERNAME_FIELD = 'email'
    REQUIRED_FIELDS = []

    objects = CustomUserManager()

    class Meta:
        constraints = [
            models.UniqueConstraint(
                Lower('email'),
                name='core_user_email_ci_unique',
            ),
        ]

    def save(self, *args, **kwargs):
        self.email = type(self).objects.normalize_email(self.email)
        super().save(*args, **kwargs)

    def set_password(self, raw_password):
        super().set_password(raw_password)
        self.password_set_at = timezone.now() if raw_password is not None else None

    def __str__(self):
        return self.email


class PasswordResetChallenge(models.Model):
    """One-use password setup/reset secret; only the secret hash is persisted."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.ForeignKey(
        User,
        on_delete=models.PROTECT,
        related_name='password_reset_challenges',
    )
    secret_hash = models.CharField(max_length=64)
    expires_at = models.DateTimeField()
    consumed_at = models.DateTimeField(blank=True, null=True)
    requested_ip_hash = models.CharField(max_length=64, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ('-created_at',)
        indexes = [
            models.Index(fields=('user', 'created_at'), name='password_reset_user_idx'),
            models.Index(fields=('expires_at',), name='password_reset_expiry_idx'),
        ]

    def __str__(self):
        return f"{self.user_id}:{self.id}"


class PasswordResetDeliveryStatus(models.TextChoices):
    PENDING = 'pending', 'Pending'
    SENDING = 'sending', 'Sending'
    SENT = 'sent', 'Sent'
    FAILED = 'failed', 'Failed'
    CANCELLED = 'cancelled', 'Cancelled'


class PasswordResetEmailDelivery(models.Model):
    """Durable outbox row containing an encrypted reset link."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    challenge = models.OneToOneField(
        PasswordResetChallenge,
        on_delete=models.CASCADE,
        related_name='email_delivery',
    )
    encrypted_reset_link = models.TextField()
    status = models.CharField(
        max_length=16,
        choices=PasswordResetDeliveryStatus.choices,
        default=PasswordResetDeliveryStatus.PENDING,
    )
    attempts = models.PositiveSmallIntegerField(default=0)
    available_at = models.DateTimeField(default=timezone.now)
    claimed_at = models.DateTimeField(blank=True, null=True)
    sent_at = models.DateTimeField(blank=True, null=True)
    last_error_code = models.CharField(max_length=120, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ('created_at',)
        indexes = [
            models.Index(
                fields=('status', 'available_at', 'created_at'),
                name='password_delivery_pending_idx',
            ),
        ]

    def __str__(self):
        return f"{self.challenge_id}:{self.status}"
class Hackathon(models.Model):
    name = models.CharField(max_length=255)
    slug = models.SlugField(unique=True)
    description = models.TextField()
    start_date = models.DateField()
    end_date = models.DateField()
    bg_image_url = models.URLField(blank=True, null=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return self.name

from django.core.cache import cache

class GlobalSettings(models.Model):
    is_obscured = models.BooleanField(default=True, help_text="If set to True, submission scores will be hidden from users.")

    class Meta:
        verbose_name = "Global Settings"
        verbose_name_plural = "Global Settings"

    def save(self, *args, **kwargs):
        self.pk = 1
        super(GlobalSettings, self).save(*args, **kwargs)
        cache.set('global_settings', self)

    def delete(self, *args, **kwargs):
        pass

    @classmethod
    def load(cls):
        obj, created = cls.objects.get_or_create(pk=1)
        return obj

    def __str__(self):
        return "Global Settings"

# Compatibility imports for one release after the Content Factory app split.
# New code should import these models from organizations, workflow_runs, or content_factory.
from organizations.models import Organization
from workflow_runs.models import (
    ContentFactoryApprovalState,
    ContentFactoryRun,
    ContentFactoryRunStatus,
    ContentFactoryRunStep,
    ContentFactoryRunStepAttempt,
    ContentFactoryStepStatus,
)
from content_factory.models import (
    AISaturation,
    AutomationRun,
    AutomationRunStatus,
    ClusterMembership,
    ComponentMapping,
    ContentFactoryHealingPromotionState,
    ContentFactoryHealingRecord,
    ContentFactoryJob,
    GeneratedComponent,
    KeywordSource,
    KeywordStatus,
    KeywordTier,
    KeywordVelocity,
    NotificationChannel,
    NotificationChannelType,
    NotificationConsentState,
    NotificationDelivery,
    NotificationDeliveryStatus,
    OrganizationContentConfig,
    PAQuestion,
    ResearchAutomation,
    ResearchAutomationStatus,
    ResearchedKeyword,
    ScheduledDiscoveryDispatch,
    ScheduledDiscoveryDispatchState,
    SemanticCluster,
    TrendStatus,
    WebsiteDesignSnapshot,
    WrittenArticle,
)
