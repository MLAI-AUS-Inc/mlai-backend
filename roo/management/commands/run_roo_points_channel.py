"""Run the native Roo Points channel worker."""

import os
import time

from django.core.management.base import BaseCommand, CommandError
import requests

from roo.native_points_requests import RooRelay, deliver_ready, scan_requests


class Command(BaseCommand):
    help = "Process native Roo Points requests and publish queued confirmations"

    def add_arguments(self, parser):
        parser.add_argument("--once", action="store_true")

    def handle(self, *args, **options):
        raw = os.getenv("ROO_POINTS_NATIVE_ACTIVE_FROM", "")
        if not raw.isdecimal() or int(raw) <= 0:
            raise CommandError("Set ROO_POINTS_NATIVE_ACTIVE_FROM to an explicit Unix timestamp")
        relay = RooRelay(os.getenv("ROO_POINTS_NATIVE_SIGNING_KEY", ""))
        while True:
            try:
                scan_requests(relay, int(raw))
                deliver_ready(relay)
            except (requests.RequestException, ValueError) as exc:
                self.stderr.write(f"Roo Points worker scan failed ({type(exc).__name__})")
                if options["once"]:
                    raise CommandError("Roo Points worker failed") from None
            if options["once"]:
                break
            time.sleep(3)
