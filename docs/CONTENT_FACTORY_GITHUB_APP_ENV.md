# Content Factory GitHub App Env

`mlai-backend` is the source of truth for customer repository access. It stores the selected repository and `github_installation_id`, then mints short-lived MLAI Tools GitHub App installation tokens for Content Factory.

Required backend env:

```bash
GITHUB_APP_ID=
GITHUB_APP_PRIVATE_KEY=
CONTENT_FACTORY_URL=
CONTENT_FACTORY_API_KEY=
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
