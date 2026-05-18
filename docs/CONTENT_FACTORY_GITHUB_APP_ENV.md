# Content Factory GitHub App Env

`mlai-backend` is the source of truth for customer repository access. It stores the selected repository and `github_installation_id`, then mints short-lived MLAI Tools GitHub App installation tokens for Content Factory.

Required backend env:

```bash
GITHUB_APP_ID=
GITHUB_APP_PRIVATE_KEY=
CONTENT_FACTORY_URL=
CONTENT_FACTORY_API_KEY=
```

`GITHUB_APP_PRIVATE_KEY` must be stored as a single line with escaped newlines:

```bash
GITHUB_APP_PRIVATE_KEY=-----BEGIN PRIVATE KEY-----\n...\n-----END PRIVATE KEY-----
```

Do not paste the key as a multiline value in `/root/mlai-backend/.env`; Django receives the raw env value and `integrations.services.github_app` converts literal `\n` sequences back to PEM newlines before signing GitHub App JWTs.

On the production droplet, validate the config without printing secret values:

```bash
cd /root/mlai-backend
docker compose run -T --rm --no-deps web python manage.py check_github_app_credentials
```

To also verify the key against GitHub:

```bash
docker compose run -T --rm --no-deps web python manage.py check_github_app_credentials --validate-github
```

Keep GitHub OAuth env for connect/reconnect and repo listing. Do not configure customer repository PATs in `mlai-backend`.

Content Factory calls `/api/content-factory/token?domain=<domain>&github_repo=<owner/repo>`. When the organization has a `github_installation_id`, the response should include:

```json
{
  "token_source": "github_app_installation",
  "github_installation_id": "...",
  "github_repo": "owner/repo"
}
```

If this endpoint returns an OAuth token source, check that the MLAI Tools app is installed on the target repo and that `GITHUB_APP_PRIVATE_KEY` is configured.
