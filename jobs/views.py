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


def _render_job_card(job: JobListing) -> str:
    rank = f"#{job.rank} top pick" if job.is_top_pick and job.rank else "Matched role"
    bucket = job.bucket or "matched"
    link = escape(job.apply_url or job.job_url)
    logo = (
        f'<img src="{escape(job.company_logo_url)}" alt="{escape(job.company_name or "Company")} logo" '
        'style="height:32px;max-width:120px;object-fit:contain;margin-bottom:8px;">'
        if job.company_logo_url
        else ""
    )
    return f"""
    <article class="job" id="{escape(bucket)}">
      {logo}
      <div><span class="tag">{escape(rank)}</span><span class="tag">{escape(bucket.replace("_", " ").title())}</span></div>
      <h3>{escape(job.title)}</h3>
      <p class="meta">{escape(job.company_name or "Unknown company")} - {escape(job.location or "Location not listed")} - {escape(job.source_name)}</p>
      <p>{escape(job.summary or job.why_selected or "Good match for today.")}</p>
      <p class="meta">{escape(job.company_stage or "")} {escape(job.company_size or "")} {escape((job.remote_eligibility or "").replace("_", " ").title())}</p>
      <p class="score">Score: {job.ranking_score:.2f}</p>
      <p><a href="{link}" target="_blank" rel="noopener noreferrer">Read more</a></p>
    </article>
    """
@method_decorator(csrf_exempt, name="dispatch")
class DailyRunTriggerView(APIView):
    permission_classes = [HasRooApiKey]

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


class DailyJobsJsonView(APIView):
    permission_classes = [AllowAny]

    def get(self, request, run_date: str):
        run = latest_run_for_date(run_date)
        if not run:
            return Response([])
        rows = JobListing.objects.filter(run=run).order_by("-is_top_pick", "rank", "-ranking_score")
        return Response(JobListingSerializer(rows, many=True).data)


class DailyJobsHtmlView(APIView):
    permission_classes = [AllowAny]

    def get(self, request, run_date: str):
        run = latest_run_for_date(run_date)
        if not run:
            return HttpResponse(f"<h1>Roo Jobs Daily</h1><p>No run found for {escape(run_date)} yet.</p>")

        rows = list(JobListing.objects.filter(run=run).order_by("-is_top_pick", "rank", "-ranking_score"))
        if not rows:
            return HttpResponse(f"<h1>Roo Jobs Daily</h1><p>No matched jobs found for {escape(run_date)} yet.</p>")

        top_jobs = [job for job in rows if job.is_top_pick]
        full_list_url = f"{settings.public_base_url}/api/v1/jobs/daily/{run_date}"
        slack_preview = format_slack_message(run_date, top_jobs, full_list_url)["text"] if top_jobs else ""
        job_cards = "\n".join(_render_job_card(job) for job in rows)
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
            .filters {{ display: flex; gap: 8px; flex-wrap: wrap; margin: 16px 0 24px; }}
            .filters a {{ color: #12343b; background: white; border: 1px solid #d8dee4; border-radius: 8px; padding: 8px 10px; text-decoration: none; }}
            .job {{ background: white; border: 1px solid #d8dee4; border-radius: 8px; padding: 18px; margin-bottom: 14px; }}
            .meta {{ color: #52616b; font-size: 14px; margin: 6px 0; }}
            .score {{ color: #0f766e; font-weight: 700; }}
            .tag {{ display: inline-block; background: #e6f4f1; color: #0f5f59; border-radius: 8px; padding: 4px 8px; margin-right: 6px; font-size: 12px; }}
            pre {{ white-space: pre-wrap; background: #101820; color: white; border-radius: 8px; padding: 16px; }}
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
              <div class="filters">
                <a href="#australian_ai">Australian AI</a>
                <a href="#australian_startup">Australian Startup</a>
                <a href="#remote_ai">Remote AI</a>
                <a href="#remote_startup">Remote Startup</a>
                <a href="/api/v1/jobs/daily/{escape(run_date)}/json">JSON feed</a>
              </div>
            </section>
            <section>
              <h2>Slack preview</h2>
              <pre>{escape(slack_preview)}</pre>
            </section>
            <section>
              <h2>All matched jobs</h2>
              {job_cards}
            </section>
          </main>
        </body>
        </html>
        """
        return HttpResponse(html)
