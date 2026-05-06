# Jobs Daily Setup

This is the recommended production setup for the daily jobs scraper.

## Architecture

- `mlai-backend` owns the 7am Melbourne schedule
- `mlai-backend` owns scraping, ranking, run state, retries, and Slack posting
- Roo does not own the daily clock
- Roo is optional for future manual trigger UX, but it should stay out of the critical path

## Production Env

### Backend

Set these in `/root/mlai-backend/.env`:

```env
JOBS_SCHEDULER_ENABLED=true
JOBS_SCHEDULER_POST_TO_SLACK=true
JOBS_SCHEDULER_POST_TO_NOTION=false

JOBS_SLACK_CHANNEL=#jobs
SLACK_BOT_TOKEN=xoxb-...

JOBS_PUBLIC_BASE_URL=https://api.mlai.au

JOBS_SCHEDULE_TIMEZONE=Australia/Melbourne
JOBS_SCHEDULE_HOUR=7
JOBS_SCHEDULE_MINUTE=0

JOBS_SCHEDULER_MAX_PAGES=2
JOBS_SCHEDULER_PER_KEYWORD_LIMIT=7
JOBS_SCRAPE_HEADLESS=true
JOBS_FRESHNESS_HOURS=72
JOBS_TOP_PICK_LIMIT=7

JOBS_RETRY_ATTEMPTS=3
JOBS_RETRY_DELAY_SECONDS=300
JOBS_FAILURE_STOP_AFTER_DAYS=3
```

### Roo

Set this in `/root/roo/roo-standalone/.env`:

```env
JOBS_SCHEDULER_ENABLED=false
```

Only add these in Roo if you later want Roo to trigger backend runs manually:

```env
JOBS_API_URL=https://api.mlai.au/api/v1
JOBS_TRIGGER_TOKEN=<backend ROO_API_KEY or INTERNAL_API_KEY>
```

## Triggering

### Automatic 7am run

The backend scheduler loop runs continuously and starts the daily jobs run when the local Melbourne time reaches the configured schedule window.

### Manual run

Use the backend API directly:

```bash
curl -X POST https://api.mlai.au/api/v1/jobs/daily-run \
  -H 'X-API-Key: <backend ROO_API_KEY or INTERNAL_API_KEY>' \
  -H 'Content-Type: application/json' \
  -d '{
    "collect_live": true,
    "post_to_slack": false,
    "post_to_notion": false,
    "sources": ["AI Jobs Australia"],
    "max_pages": 1,
    "per_keyword_limit": 3
  }'
```

Then inspect the run:

```bash
curl https://api.mlai.au/api/v1/jobs/runs/<run_id>
```

## Slack

- `SLACK_BOT_TOKEN` must be present on backend
- `mlai_bot` must be invited to the target channel
- if `#jobs` is not the real channel name, set `JOBS_SLACK_CHANNEL` to the actual channel
