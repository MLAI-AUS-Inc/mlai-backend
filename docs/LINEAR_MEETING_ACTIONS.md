# Roo Linear Meeting Actions

Roo calls `mlai-backend` for Linear meeting-action context and issue creation. Keep the Linear personal API key only in the backend environment.

Backend `.env` example:

```bash
LINEAR_API_KEY=
ROO_API_KEY=
INTERNAL_API_KEY=
```

Create the Linear API key in Linear under **Settings > Account > Security & Access**. Use a dedicated service or bot user when possible, and do not commit the populated value.
