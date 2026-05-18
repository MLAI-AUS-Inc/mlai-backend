param(
    [string[]]$Sources = @("SEEK"),
    [int]$MaxPages = 1,
    [int]$PerKeywordLimit = 1,
    [switch]$CheckSchedulerWindow
)

$ErrorActionPreference = "Stop"

$repoRoot = Split-Path -Parent $PSScriptRoot
Set-Location $repoRoot

$env:JOBS_SCRAPE_HEADLESS = if ($env:JOBS_SCRAPE_HEADLESS) { $env:JOBS_SCRAPE_HEADLESS } else { "false" }
$env:JOBS_SCHEDULER_ENABLED = if ($env:JOBS_SCHEDULER_ENABLED) { $env:JOBS_SCHEDULER_ENABLED } else { "true" }

Write-Host "Running Django checks..."
python manage.py check

$joinedSources = ($Sources | ForEach-Object { "'$_'" }) -join ", "

Write-Host ""
Write-Host "Running queued manual jobs flow..."
$runIdOutput = python manage.py shell -c @"
from jobs.services.job_pipeline import enqueue_run_from_request
run = enqueue_run_from_request(
    {
        'collect_live': True,
        'post_to_slack': False,
        'post_to_notion': False,
        'sources': [$joinedSources],
        'max_pages': $MaxPages,
        'per_keyword_limit': $PerKeywordLimit,
    },
    trigger_source='manual_api',
)
print(run.run_id)
"@
$runId = ($runIdOutput | Select-Object -Last 1).Trim()
Write-Host "Queued run: $runId"

python manage.py run_scheduled_discovery

python manage.py shell -c @"
from jobs.models import JobRun, SourceRunLog, JobListing
run = JobRun.objects.get(run_id='$runId')
print('STATUS', run.status)
print('COUNTS', run.fetched_count, run.matched_count, run.deduped_count, run.ranked_count)
print('ERROR', run.error_message)
print('CLAIMED', bool(run.claimed_at), bool(run.started_at), bool(run.completed_at))
print('LOGS', list(SourceRunLog.objects.filter(run=run).values_list('source_name', 'status', 'fetched_count', 'error_message')))
print('JOBS', list(JobListing.objects.filter(run=run).values_list('title', 'company_name', 'job_url')[:5]))
"@

if ($CheckSchedulerWindow) {
    Write-Host ""
    Write-Host "Checking scheduler behavior for May 2, 2026 at 7:00 AM Australia/Melbourne..."
    python manage.py shell -c @"
from datetime import datetime
from zoneinfo import ZoneInfo
from django.utils import timezone
from jobs.models import JobRun
from jobs.services import job_pipeline

JobRun.objects.filter(run_date='2026-05-02', trigger_source='daily_scheduler').delete()

original = job_pipeline.run_daily_jobs
def fake_run_daily_jobs(run_id, *args, **kwargs):
    run = JobRun.objects.get(run_id=run_id)
    run.status = 'completed'
    run.started_at = run.started_at or timezone.now()
    run.completed_at = timezone.now()
    run.save(update_fields=['status', 'started_at', 'completed_at', 'updated_at'])

job_pipeline.run_daily_jobs = fake_run_daily_jobs
try:
    result_before = job_pipeline.run_daily_jobs_scheduler(
        datetime(2026, 5, 2, 6, 59, tzinfo=ZoneInfo('Australia/Melbourne'))
    )
    JobRun.objects.filter(run_date='2026-05-02', trigger_source='daily_scheduler').delete()
    result_at = job_pipeline.run_daily_jobs_scheduler(
        datetime(2026, 5, 2, 7, 0, tzinfo=ZoneInfo('Australia/Melbourne'))
    )
    print({'before_7am': result_before, 'at_7am': result_at})
finally:
    job_pipeline.run_daily_jobs = original
"@
}
