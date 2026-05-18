from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from integrations import http_client as http_requests
from integrations.services import github_app


class Command(BaseCommand):
    help = "Validate MLAI Tools GitHub App server credentials without printing secret values."

    def add_arguments(self, parser):
        parser.add_argument(
            "--validate-github",
            action="store_true",
            help="Also call GitHub /app with the generated JWT.",
        )

    def handle(self, *args, **options):
        app_id = str(getattr(settings, "GITHUB_APP_ID", "") or "").strip()
        private_key = github_app._github_app_private_key()
        missing = []
        if not app_id:
            missing.append("GITHUB_APP_ID")
        if not private_key:
            missing.append("GITHUB_APP_PRIVATE_KEY")
        if missing:
            raise CommandError(
                "Missing required GitHub App env value(s): "
                + ", ".join(missing)
                + ". Configure the MLAI Tools GitHub App id and private key."
            )

        if "BEGIN" not in private_key or "PRIVATE KEY" not in private_key:
            raise CommandError(
                "GITHUB_APP_PRIVATE_KEY is present but does not look like a PEM private key. "
                "Store it as one line with escaped newlines, for example "
                "-----BEGIN PRIVATE KEY-----\\n...\\n-----END PRIVATE KEY-----."
            )

        try:
            jwt_token = github_app._github_app_jwt()
        except Exception as exc:
            raise CommandError(
                "GITHUB_APP_PRIVATE_KEY is present but could not sign a GitHub App JWT. "
                "Check that the key belongs to the MLAI Tools GitHub App and uses escaped newlines."
            ) from exc

        self.stdout.write(self.style.SUCCESS("GitHub App credentials can sign a JWT."))

        if not options.get("validate_github"):
            return

        response = http_requests.get(
            "https://api.github.com/app",
            headers={
                "Authorization": f"Bearer {jwt_token}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
            },
            timeout=(3, 15),
        )
        if response.status_code != 200:
            raise CommandError(f"GitHub /app validation failed with status {response.status_code}.")

        payload = response.json()
        slug = str(payload.get("slug") or payload.get("name") or "unknown")
        self.stdout.write(self.style.SUCCESS(f"GitHub App API validation succeeded for {slug}."))
