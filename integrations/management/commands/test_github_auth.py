import sys
from django.core.management.base import BaseCommand
from integrations.models import UserIntegration
from github import Github, GithubException

class Command(BaseCommand):
    help = 'Test GitHub authentication with a manual token or stored credential'

    def add_arguments(self, parser):
        parser.add_argument(
            '--token',
            type=str,
            help='Manual GitHub Personal Access Token to test'
        )
        parser.add_argument(
            '--slack-user-id',
            type=str,
            help='Slack User ID to look up stored token'
        )
        parser.add_argument(
            '--unmask',
            action='store_true',
            help='Print the full token (BE CAREFUL)'
        )

    def handle(self, *args, **options):
        token = options.get('token')
        slack_user_id = options.get('slack_user_id')
        unmask = options.get('unmask')

        if not token and not slack_user_id:
            self.stderr.write(self.style.ERROR('Please provide either --token or --slack-user-id'))
            return

        if slack_user_id:
            try:
                integration = UserIntegration.objects.get(slack_user_id=slack_user_id)
                token = integration.github_access_token
                if not token:
                    self.stderr.write(self.style.ERROR(f'No GitHub token found for user {slack_user_id}'))
                    return
                self.stdout.write(self.style.SUCCESS(f'Found stored token for user {slack_user_id}'))
                
                if integration.github_scopes:
                    self.stdout.write(f'Stored Scopes: {integration.github_scopes}')

            except UserIntegration.DoesNotExist:
                self.stderr.write(self.style.ERROR(f'No integration found for user {slack_user_id}'))
                return

        # Mask token for display unless unmask requested
        display_token = token if unmask else f"{token[:4]}...{token[-4:]}"
        self.stdout.write(f'Testing Token: {display_token}')

        try:
            g = Github(token)
            user = g.get_user()
            
            # Verify identity
            login = user.login
            self.stdout.write(self.style.SUCCESS(f'✅ Authentication Successful! Logged in as: {login}'))

            # Verify permissions (try to list repos)
            self.stdout.write('Listing first 5 accessible repositories:')
            repos = user.get_repos()
            for i, repo in enumerate(repos):
                if i >= 5:
                    break
                self.stdout.write(f'- {repo.full_name} ({repo.html_url})')
                
            # Check token scopes (if available via headers)
            # PyGithub usually handles this, but we can try to inspect
            if hasattr(g, 'oauth_scopes'):
                 self.stdout.write(f'Token Scopes from API: {g.oauth_scopes}')

        except GithubException as e:
            self.stderr.write(self.style.ERROR(f'❌ GitHub API Error: {e.status} {e.data}'))
        except Exception as e:
            self.stderr.write(self.style.ERROR(f'❌ Unexpected Error: {str(e)}'))
