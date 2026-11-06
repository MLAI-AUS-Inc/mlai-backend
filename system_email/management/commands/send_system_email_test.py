"""Render one message with sample data and send it to an allow-listed address."""

import uuid

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from system_email import api
from system_email.fixtures import SAMPLE_CONTEXT
from system_email.registry import MESSAGES
from system_email.render import RenderedEmail


class Command(BaseCommand):
    help = "Send a sample of a system email to an address in SYSTEM_EMAIL_TEST_RECIPIENTS."

    def add_arguments(self, parser):
        parser.add_argument("--message", required=True, choices=sorted(MESSAGES))
        parser.add_argument("--to", required=True)

    def handle(self, *args, **options):
        allowed = {value.strip().lower() for value in getattr(settings, "SYSTEM_EMAIL_TEST_RECIPIENTS", ()) if value}
        to = options["to"].strip().lower()
        if to not in allowed:
            raise CommandError("--to must be listed in SYSTEM_EMAIL_TEST_RECIPIENTS.")
        key = options["message"]
        spec = MESSAGES[key]
        prerendered = None
        if spec.prerendered:
            prerendered = RenderedEmail(subject=f"[Test] {key}", html=f"<p>Test of {key}.</p>", text=f"Test of {key}.\n")
        try:
            result = api.send(
                key,
                to=to,
                idempotency_key=f"test/{key}/{uuid.uuid4()}",
                context=SAMPLE_CONTEXT.get(key, {}),
                prerendered=prerendered,
                unsubscribe_url="https://mlai.au/unsubscribe-test" if spec.unsubscribe else "",
            )
        except api.SystemEmailRejected as exc:
            raise CommandError(f"Rejected: {exc.code}") from exc
        self.stdout.write(f"Sent {key}: resend id {result.provider_message_id}")
