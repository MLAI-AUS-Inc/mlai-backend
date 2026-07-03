from __future__ import annotations

from html import escape

from django.http import Http404, HttpResponse
from django.utils.decorators import method_decorator
from django.views.decorators.csrf import csrf_exempt
from rest_framework import status
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.views import APIView

from core.permissions import HasRooApiKey

from .conf import settings
from .models import JobListing, JobRun
from .serializers import DailyRunRequestSerializer, JobListingSerializer
from .services.job_pipeline import enqueue_run_from_request, latest_run_for_date
from .services.slack import format_slack_message

VALID_BUCKETS = {"australian_ai", "australian_startup", "remote_ai", "remote_startup"}


class HasJobsTriggerToken(HasRooApiKey):
    """
    Allows Roo's daily scheduler to trigger jobs with JOBS_TRIGGER_TOKEN.

    Roo sends Authorization: Bearer <token>. We also keep the existing Roo API
    key permission path so older/internal callers using X-API-Key still work.
    """

    def has_permission(self, request, view):
        trigger_token = settings.jobs_trigger_token
        auth_header = request.META.get("HTTP_AUTHORIZATION", "")
        if trigger_token and auth_header.startswith("Bearer "):
            candidate = auth_header.split("Bearer ", 1)[1].strip()
            if candidate == trigger_token:
                return True
        return super().has_permission(request, view)


def _bucket_filter(request) -> str | None:
    bucket = str(request.query_params.get("bucket") or request.GET.get("bucket") or "").strip()
    return bucket if bucket in VALID_BUCKETS else None


def _jobs_for_run(run: JobRun, bucket: str | None = None):
    rows = JobListing.objects.filter(run=run)
    if bucket:
        rows = rows.filter(bucket=bucket)
    return rows.order_by("-is_top_pick", "rank", "-ranking_score")


def _render_job_card(job: JobListing) -> str:
    rank = f"#{job.rank} top pick" if job.is_top_pick and job.rank else "Matched role"
    bucket = job.bucket or "matched"
    link = escape(job.apply_url or job.job_url)
    title = escape(job.title)
    logo = (
        f'<img src="{escape(job.company_logo_url)}" alt="{escape(job.company_name or "Company")} logo" '
        'style="height:32px;max-width:120px;object-fit:contain;margin-bottom:8px;">'
        if job.company_logo_url
        else ""
    )
    return f"""
    <article class="job" id="{escape(bucket)}" data-role-title="{title.lower()}">
      {logo}
      <div><span class="tag">{escape(rank)}</span><span class="tag">{escape(bucket.replace("_", " ").title())}</span></div>
      <h3>{title}</h3>
      <p class="meta">{escape(job.company_name or "Unknown company")} - {escape(job.location or "Location not listed")} - {escape(job.source_name)}</p>
      <p>{escape(job.summary or job.why_selected or "Good match for today.")}</p>
      <p class="meta">{escape(job.company_stage or "")} {escape(job.company_size or "")} {escape((job.remote_eligibility or "").replace("_", " ").title())}</p>
      <p class="score">Score: {job.ranking_score:.2f}</p>
      <p><a href="{link}" target="_blank" rel="noopener noreferrer">Read more</a></p>
    </article>
    """
@method_decorator(csrf_exempt, name="dispatch")
class DailyRunTriggerView(APIView):
    permission_classes = [HasJobsTriggerToken]

    def post(self, request):
        serializer = DailyRunRequestSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        run = enqueue_run_from_request(serializer.validated_data, trigger_source="manual_api")
        return Response(
            {
                "run_id": run.run_id,
                "status": run.status,
                "status_url": f"/api/v1/jobs/runs/{run.run_id}",
                "full_list_url": run.full_list_url or f"/api/v1/jobs/daily/{run.run_date}",
            },
            status=status.HTTP_202_ACCEPTED,
        )


class JobRunDetailView(APIView):
    permission_classes = [AllowAny]

    def get(self, request, run_id: str):
        run = JobRun.objects.filter(run_id=run_id).first()
        if not run:
            raise Http404("Run not found")
        top_jobs = JobListing.objects.filter(run=run, is_top_pick=True).order_by("rank")
        return Response(
            {
                "run_id": run.run_id,
                "run_date": run.run_date,
                "status": run.status,
                "started_at": run.started_at,
                "completed_at": run.completed_at,
                "error_message": run.error_message,
                "full_list_url": run.full_list_url,
                "slack_posted_at": run.slack_posted_at,
                "counts": {
                    "fetched": run.fetched_count,
                    "matched": run.matched_count,
                    "deduped": run.deduped_count,
                    "ranked": run.ranked_count,
                },
                "source_logs": [
                    {
                        "source_name": log.source_name,
                        "status": log.status,
                        "fetched_count": log.fetched_count,
                        "error_message": log.error_message,
                    }
                    for log in run.source_logs.all()
                ],
                "top_jobs": JobListingSerializer(top_jobs, many=True).data,
            }
        )


class JobRunSlackPayloadView(APIView):
    permission_classes = [AllowAny]

    def get(self, request, run_id: str):
        run = JobRun.objects.filter(run_id=run_id).first()
        if not run:
            raise Http404("Run not found")
        top_jobs = list(JobListing.objects.filter(run=run, is_top_pick=True).order_by("rank"))
        if not top_jobs:
            raise Http404("Run has no top jobs yet")
        return Response(format_slack_message(run.run_date, top_jobs, run.full_list_url or ""))


class JobsHistoryView(APIView):
    permission_classes = [AllowAny]

    def get(self, request):
        try:
            limit = min(max(int(request.query_params.get("limit", 30)), 1), 100)
        except (TypeError, ValueError):
            limit = 30
        runs = JobRun.objects.filter(run_date__regex=r"^\d{4}-\d{2}-\d{2}$").order_by("-run_date", "-created_at")[:limit]
        return Response(
            [
                {
                    "run_id": run.run_id,
                    "run_date": run.run_date,
                    "status": run.status,
                    "trigger_source": run.trigger_source,
                    "started_at": run.started_at,
                    "completed_at": run.completed_at,
                    "slack_posted_at": run.slack_posted_at,
                    "full_list_url": run.full_list_url or f"/api/v1/jobs/daily/{run.run_date}",
                    "status_url": f"/api/v1/jobs/runs/{run.run_id}",
                    "counts": {
                        "fetched": run.fetched_count,
                        "matched": run.matched_count,
                        "deduped": run.deduped_count,
                        "ranked": run.ranked_count,
                    },
                    "source_errors": [
                        {
                            "source_name": log.source_name,
                            "error_message": log.error_message,
                        }
                        for log in run.source_logs.filter(status="error")
                    ],
                    "top_jobs": JobListingSerializer(
                        JobListing.objects.filter(run=run, is_top_pick=True).order_by("rank"),
                        many=True,
                    ).data,
                }
                for run in runs
            ]
        )


class JobsHistoryHtmlView(APIView):
    permission_classes = [AllowAny]

    def get(self, request):
        runs = JobRun.objects.filter(run_date__regex=r"^\d{4}-\d{2}-\d{2}$").order_by("-run_date", "-created_at")[:30]
        run_cards = []
        for run in runs:
            top_jobs = list(JobListing.objects.filter(run=run, is_top_pick=True).order_by("rank"))
            top_jobs_html = "".join(
                f"<li>#{job.rank or '-'} {escape(job.title)} - {escape(job.company_name or 'Unknown company')}</li>"
                for job in top_jobs
            )
            run_cards.append(
                f"""
                <article class="run">
                  <h2>{escape(run.run_date)} <span>{escape(run.status)}</span></h2>
                  <p>Fetched: {run.fetched_count} | Matched: {run.matched_count} | Deduplicated: {run.deduped_count} | Ranked: {run.ranked_count}</p>
                  <p><a href="/api/v1/jobs/daily/{escape(run.run_date)}">View full list</a> | <a href="/api/v1/jobs/runs/{escape(run.run_id)}">View run JSON</a></p>
                  <ul>{top_jobs_html or "<li>No top picks recorded.</li>"}</ul>
                </article>
                """
            )
        return HttpResponse(
            f"""
            <!doctype html>
            <html lang="en">
            <head>
              <meta charset="utf-8">
              <meta name="viewport" content="width=device-width, initial-scale=1">
              <title>Roo Jobs Daily History</title>
              <style>
                body {{ font-family: Arial, sans-serif; margin: 0; color: #1f2933; background: #f7f8fa; }}
                header {{ background: #12343b; color: white; padding: 28px 24px; }}
                main {{ max-width: 980px; margin: 0 auto; padding: 24px; }}
                .run {{ background: white; border: 1px solid #d8dee4; border-radius: 8px; padding: 18px; margin-bottom: 14px; }}
                h2 {{ margin-top: 0; }}
                h2 span {{ color: #0f766e; font-size: 14px; margin-left: 8px; }}
                a {{ color: #0f5f59; }}
              </style>
            </head>
            <body>
              <header><h1>Roo Jobs Daily History</h1><p>Recent scrape runs and published shortlists.</p></header>
              <main>{''.join(run_cards) or '<p>No valid job runs found.</p>'}</main>
            </body>
            </html>
            """
        )


class DailyJobsJsonView(APIView):
    permission_classes = [AllowAny]

    def get(self, request, run_date: str):
        run = latest_run_for_date(run_date)
        if not run:
            return Response([])
        rows = _jobs_for_run(run, _bucket_filter(request))
        return Response(JobListingSerializer(rows, many=True).data)


class DailyJobsHtmlView(APIView):
    permission_classes = [AllowAny]

    def get(self, request, run_date: str):
        run = latest_run_for_date(run_date)
        if not run:
            return HttpResponse(f"<h1>Roo Jobs Daily</h1><p>No run found for {escape(run_date)} yet.</p>")

        selected_bucket = _bucket_filter(request)
        rows = list(_jobs_for_run(run, selected_bucket))
        if not rows:
            return HttpResponse(f"<h1>Roo Jobs Daily</h1><p>No matched jobs found for {escape(run_date)} yet.</p>")

        top_jobs = [job for job in rows if job.is_top_pick]
        job_cards = "\n".join(_render_job_card(job) for job in rows)
        active_filter = selected_bucket or "all"
        filter_links = [
            ("all", "All", f"/api/v1/jobs/daily/{escape(run_date)}"),
            ("australian_ai", "Australian AI", f"/api/v1/jobs/daily/{escape(run_date)}?bucket=australian_ai"),
            ("australian_startup", "Australian Startup", f"/api/v1/jobs/daily/{escape(run_date)}?bucket=australian_startup"),
            ("remote_ai", "Remote AI", f"/api/v1/jobs/daily/{escape(run_date)}?bucket=remote_ai"),
            ("remote_startup", "Remote Startup", f"/api/v1/jobs/daily/{escape(run_date)}?bucket=remote_startup"),
        ]
        filter_html = "\n".join(
            f'<a class="filter-link{" active" if key == active_filter else ""}" href="{href}" aria-current="page">{label}</a>'
            if key == active_filter
            else f'<a class="filter-link" href="{href}">{label}</a>'
            for key, label, href in filter_links
        )

        html = f"""
        <!doctype html>
        <html lang="en">
        <head>
          <meta charset="utf-8">
          <meta name="viewport" content="width=device-width, initial-scale=1">
          <title>Roo Jobs Daily - {escape(run_date)}</title>
          <style>
            body {{ font-family: Arial, sans-serif; margin: 0; color: #1f2933; background: #f7f8fa; }}
            header {{ background: #12343b; color: white; padding: 32px 24px; }}
            main {{ max-width: 1080px; margin: 0 auto; padding: 24px; }}
            .toolbar {{ display: grid; gap: 12px; margin: 16px 0 24px; }}
            .search-input {{ box-sizing: border-box; width: 100%; border: 1px solid #cbd5df; border-radius: 8px; padding: 11px 12px; font-size: 16px; background: white; color: #1f2933; }}
            .search-input:focus {{ border-color: #0f766e; box-shadow: 0 0 0 3px rgba(15, 118, 110, 0.16); outline: none; }}
            .filters {{ display: flex; gap: 8px; flex-wrap: wrap; }}
            .filter-link {{ color: #12343b; background: white; border: 1px solid #cbd5df; border-radius: 8px; padding: 8px 10px; text-decoration: none; font-weight: 700; }}
            .filter-link:hover {{ border-color: #0f766e; color: #0f5f59; }}
            .filter-link.active {{ background: #0f766e; border-color: #0f766e; color: white; }}
            .job {{ background: white; border: 1px solid #d8dee4; border-radius: 8px; padding: 18px; margin-bottom: 14px; }}
            .job[hidden] {{ display: none; }}
            .meta {{ color: #52616b; font-size: 14px; margin: 6px 0; }}
            .score {{ color: #0f766e; font-weight: 700; }}
            .tag {{ display: inline-block; background: #e6f4f1; color: #0f5f59; border-radius: 8px; padding: 4px 8px; margin-right: 6px; font-size: 12px; }}
            .empty-state {{ display: none; background: white; border: 1px solid #d8dee4; border-radius: 8px; padding: 18px; color: #52616b; }}
            .empty-state.visible {{ display: block; }}
            a {{ color: #0f5f59; }}
          </style>
        </head>
        <body>
          <header>
            <h1>Roo Jobs Daily</h1>
            <p>{escape(run_date)} matched AI and startup jobs for Australia and remote-friendly candidates.</p>
          </header>
          <main>
            <section>
              <h2>Filters</h2>
              <div class="toolbar">
                <input id="role-search" class="search-input" type="search" placeholder="Search role" aria-label="Search role">
                <div class="filters">
                  {filter_html}
                  <a class="filter-link" href="/api/v1/jobs/daily/{escape(run_date)}/json">JSON feed</a>
                </div>
              </div>
              <p class="meta">Showing: {escape(selected_bucket.replace("_", " ").title() if selected_bucket else "All matched jobs")}</p>
            </section>
            <section>
              <h2>All matched jobs</h2>
              <p id="empty-state" class="empty-state">No matching roles found.</p>
              {job_cards}
            </section>
          </main>
          <script>
            const roleSearch = document.getElementById('role-search');
            const emptyState = document.getElementById('empty-state');
            const jobs = Array.from(document.querySelectorAll('.job'));

            function applyRoleSearch() {{
              const query = roleSearch.value.trim().toLowerCase();
              let visibleCount = 0;

              jobs.forEach((job) => {{
                const title = job.dataset.roleTitle || '';
                const matches = !query || title.includes(query);
                job.hidden = !matches;
                if (matches) {{
                  visibleCount += 1;
                }}
              }});

              emptyState.classList.toggle('visible', visibleCount === 0);
            }}

            roleSearch.addEventListener('input', applyRoleSearch);
          </script>
        </body>
        </html>
        """
        return HttpResponse(html)
