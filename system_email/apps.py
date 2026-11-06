from django.apps import AppConfig


class SystemEmailConfig(AppConfig):
    """Transactional email sent by MLAI's own code through Resend.

    Deliberately separate from ``crm``: nothing here uses CRM sender mailboxes,
    warm-up caps or the CRM guardian, and CRM feedback never pauses it.
    """

    default_auto_field = "django.db.models.BigAutoField"
    name = "system_email"
    verbose_name = "System email"
