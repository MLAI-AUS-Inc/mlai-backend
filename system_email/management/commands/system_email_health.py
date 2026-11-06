import time

from django.core.management.base import BaseCommand

from system_email import health


class Command(BaseCommand):
    help = "Check transactional email failure rate and delivery latency; alert Slack on problems."

    def add_arguments(self, parser):
        parser.add_argument("--loop-seconds", type=int, default=0,
                            help="Repeat every N seconds (0 runs once).")

    def handle(self, *args, **options):
        while True:
            items = health.snapshot()
            problems = [problem for item in items for problem in item.problems()]
            for item in items:
                self.stdout.write(f"{item.category}: sends={item.sends} failures={item.failures} "
                                  f"bounced={item.hard_bounces} complaints={item.complaints} "
                                  f"p95={item.p95_delivery_seconds}")
            if problems:
                self.stdout.write("problems: " + "; ".join(problems))
                health.alert(problems)
            if options["loop_seconds"] <= 0:
                return
            time.sleep(options["loop_seconds"])
