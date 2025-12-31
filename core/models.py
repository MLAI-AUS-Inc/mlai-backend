from django.db import models
from django.contrib.auth.models import AbstractBaseUser, PermissionsMixin, BaseUserManager
from django.utils import timezone

class CustomUserManager(BaseUserManager):
    def create_user(self, email, role='participant', password=None, **extra_fields):
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
        ('professional', 'Professional'), # Added for flexibility
    )
    email = models.EmailField(unique=True)
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
    role = models.CharField(max_length=20, choices=ROLE_CHOICES, default='participant')
    is_superuser = models.BooleanField(default=False)
    is_active = models.BooleanField(default=True)
    has_team = models.BooleanField(default=False)
    is_staff = models.BooleanField(default=False)  # Required for admin interface
    date_joined = models.DateTimeField(default=timezone.now)
    avatar_url = models.URLField(blank=True, null=True)
    personas = models.JSONField(default=list, blank=True)

    USERNAME_FIELD = 'email'
    REQUIRED_FIELDS = ['role']

    objects = CustomUserManager()

    def __str__(self):
        return self.email

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

class Organization(models.Model):
    """Organization that uses content factory."""
    name = models.CharField(max_length=255)
    domain = models.CharField(max_length=255, unique=True, db_index=True)
    created_at = models.DateTimeField(auto_now_add=True)
    
    class Meta:
        db_table = 'content_factory_organization'

class OrganizationContentConfig(models.Model):
    """Content factory configuration per organization."""
    organization = models.OneToOneField(
        Organization, on_delete=models.CASCADE, related_name='content_config'
    )
    article_template = models.TextField(blank=True, null=True)
    design_guide = models.TextField(blank=True, null=True)
    resource_prompt = models.TextField(blank=True, null=True)
    github_repo = models.CharField(max_length=255, blank=True, null=True)
    github_token_encrypted = models.TextField(blank=True, null=True)
    article_path_pattern = models.CharField(
        max_length=255, default="app/articles/content/{category}/{slug}.tsx"
    )
    registry_path = models.CharField(max_length=255, default="app/articles/registry.ts")
    scan_summary = models.TextField(blank=True, null=True)
    tech_stack = models.JSONField(default=dict, blank=True)
    brand_name = models.CharField(max_length=100, blank=True, null=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    
    class Meta:
        db_table = 'content_factory_org_config'
