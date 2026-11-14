"""Durable checkout receipts; Roo's existing ledger remains authoritative."""
import uuid
from django.conf import settings
from django.db import models


class CybertestCheckout(models.Model):
    class Status(models.TextChoices):
        QUOTED = 'quoted'
        AUTHORIZED = 'authorized'
        CHARGED = 'charged'
        STARTED = 'started'
        REFUNDED = 'refunded'
        CANCELLED = 'cancelled'

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True,
        on_delete=models.SET_NULL, related_name='cybertest_checkouts')
    binding = models.JSONField()
    binding_sha256 = models.CharField(max_length=64)
    handoff_hash = models.CharField(max_length=64)
    points = models.PositiveIntegerField()
    test_mode = models.BooleanField(default=True)
    status = models.CharField(max_length=16, choices=Status.choices, default=Status.QUOTED)
    expires_at = models.DateTimeField()
    audit_id = models.UUIDField(null=True, blank=True, unique=True)
    spend_ledger = models.ForeignKey('roo.Ledger', null=True, blank=True,
        on_delete=models.PROTECT, related_name='cybertest_spends')
    refund_ledger = models.ForeignKey('roo.Ledger', null=True, blank=True,
        on_delete=models.PROTECT, related_name='cybertest_refunds')
    dispatched_at = models.DateTimeField(null=True, blank=True)
    failure_code = models.CharField(max_length=64, blank=True, default='')
    pricing_snapshot = models.JSONField(default=dict)
    settlement = models.JSONField(null=True, blank=True)
    settled_at = models.DateTimeField(null=True, blank=True)
    parent_checkout = models.ForeignKey('self', null=True, blank=True,
        on_delete=models.PROTECT, related_name='upgrade_checkouts')
    phase_contract = models.JSONField(default=dict)
    delivery_evidence = models.JSONField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        indexes = [models.Index(fields=['user', 'test_mode', 'status'], name='roo_cybertest_user_status')]
        constraints = [
            models.CheckConstraint(condition=models.Q(points__gte=1), name='roo_cybertest_positive_price'),
            models.UniqueConstraint(fields=['parent_checkout'],
                condition=models.Q(parent_checkout__isnull=False, status__in=['quoted', 'authorized', 'charged', 'started']),
                name='cybertest_one_active_upgrade'),
            models.CheckConstraint(
                condition=models.Q(parent_checkout__isnull=True) | ~models.Q(parent_checkout=models.F('id')),
                name='cybertest_not_own_parent'),
        ]


class CybertestCompanyBinding(models.Model):
    """Explicit, verified company ownership of an audit history workspace."""
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    company = models.OneToOneField('founder_tools.VibeRaisingCompany',
        on_delete=models.PROTECT, related_name='cybertest_binding')
    workspace_id = models.UUIDField(unique=True)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT,
        related_name='cybertest_company_bindings')
    is_active = models.BooleanField(default=False)
    verified_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [models.CheckConstraint(
            condition=models.Q(is_active=False) | models.Q(verified_at__isnull=False),
            name='cybertest_active_verified')]
