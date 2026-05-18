from django.core.management.base import BaseCommand

from integrations.services.research_automations import run_research_automation_scheduler


class Command(BaseCommand):
    help = "Create and dispatch due scheduled research automation runs."

    def add_arguments(self, parser):
        parser.add_argument("--limit", type=int, default=20)

    def handle(self, *args, **options):
        result = run_research_automation_scheduler(limit=options["limit"])
        self.stdout.write(self.style.SUCCESS(str(result)))
