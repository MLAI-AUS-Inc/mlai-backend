import logging

from django.conf import settings

from .models import VictorApplication


logger = logging.getLogger(__name__)

VICTOR_APPLICATION_DEADLINE = '6 August 2026'


def send_registration_confirmation(application: VictorApplication):
    """Send the Victor:AI registration receipt through Customer.io."""
    api_key = str(getattr(settings, 'CUSTOMERIO_API_KEY', '') or '').strip()
    template_id = str(
        getattr(settings, 'CUSTOMERIO_VICTOR_REGISTRATION_TEMPLATE_ID', '') or ''
    ).strip()
    if not api_key or not template_id:
        logger.warning(
            'Victor:AI registration confirmation not sent: Customer.io is not configured'
        )
        return None

    # Import lazily so management commands that do not send email can still run
    # in environments where optional provider dependencies are unavailable.
    from customerio import APIClient

    full_name = f'{application.first_name} {application.last_name}'.strip()
    request_body = {
        'transactional_message_id': template_id,
        'message_data': {
            'first_name': application.first_name,
            'full_name': full_name,
            'team_name': application.team_name,
            'startup_stage': application.startup_stage,
            'application_deadline': VICTOR_APPLICATION_DEADLINE,
            'website_url': 'https://victorai.win',
        },
        'to': application.email,
        # Public applicants do not necessarily have an MLAI user id. Identifying
        # them by email keeps Customer.io from creating a client_ref-based person.
        'identifiers': {'email': application.email},
    }
    from_email = str(getattr(settings, 'CUSTOMERIO_FROM_EMAIL', '') or '').strip()
    if from_email:
        request_body['from'] = from_email

    response = APIClient(api_key).send_email(request_body)
    logger.info(
        'Victor:AI registration confirmation sent to %s using template %s',
        application.email,
        template_id,
    )
    return response
