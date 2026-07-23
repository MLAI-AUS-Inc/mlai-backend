from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any

import requests

from jobs.conf import settings


@dataclass(frozen=True)
class LocationEligibility:
    status: str
    region: str | None = None
    reason: str | None = None

    @property
    def is_restricted(self) -> bool:
        return self.status == "restricted_remote"


@dataclass(frozen=True)
class DisqualificationSignal:
    category: str
    severity: str
    reason: str
    penalty: float = 0.0


RESTRICTED_PATTERNS: tuple[tuple[re.Pattern[str], str | None, str], ...] = (
    (re.compile(r"\bfrom any european union country\b", re.I), "European Union", "Restricted to European Union countries"),
    (re.compile(r"\beuropean union\b", re.I), "European Union", "Mentions European Union as the hiring region"),
    (re.compile(r"\bany eu country\b", re.I), "European Union", "Restricted to EU countries"),
    (re.compile(r"\beu\s+only\b", re.I), "European Union", "Restricted to EU only"),
    (re.compile(r"\bnot (available|open) (in|to|for) australia(ns)?\b", re.I), "Australia", "Explicitly excludes Australian candidates"),
    (re.compile(r"\baustralia(ns)? (are|is) not eligible\b", re.I), "Australia", "Explicitly excludes Australian candidates"),
    (re.compile(r"\beurope\s+only\b", re.I), "Europe", "Restricted to Europe only"),
    (re.compile(r"\bmust be based in (the )?(eu|europe)\b", re.I), "Europe", "Must be based in Europe"),
    (re.compile(r"\b(us|u\.s\.|united states)\s+only\b", re.I), "United States", "Restricted to United States only"),
    (re.compile(r"\bremote\s*[,(-]\s*(usa|us|u\.s\.|united states)\b", re.I), "United States", "Remote role restricted to the United States"),
    (re.compile(r"\b(usa|us|u\.s\.|united states)\s*[,)-]\s*remote\b", re.I), "United States", "Remote role restricted to the United States"),
    (re.compile(r"\bremote\s*[,(-]\s*(canada|uk|united kingdom|europe)\b", re.I), None, "Remote role restricted outside Australia/APAC"),
    (re.compile(r"\b(canada|uk|united kingdom|europe)\s*[,)-]\s*remote\b", re.I), None, "Remote role restricted outside Australia/APAC"),
    (re.compile(r"\bremote\s*[,(-]\s*oceania\b", re.I), "Oceania", "Remote role only mentions Oceania, not Australia/APAC explicitly"),
    (re.compile(r"\boceania\s*[,)-]\s*remote\b", re.I), "Oceania", "Remote role only mentions Oceania, not Australia/APAC explicitly"),
    (re.compile(r"\buk\s+only\b", re.I), "United Kingdom", "Restricted to UK only"),
    (re.compile(r"\bcanada\s+only\b", re.I), "Canada", "Restricted to Canada only"),
    (re.compile(r"\b(us|u\.s\.|united states)\s+or\s+canada\b", re.I), "United States/Canada", "Restricted to United States or Canada"),
    (re.compile(r"\bmust be based in (the )?(us|u\.s\.|united states|uk|canada)\b", re.I), None, "Must be based outside Australia"),
    (re.compile(r"\b(us|u\.s\.|united states|north american?)\s+time\s*zones?\b", re.I), "United States/North America", "Restricted to US/North American time zones"),
    (re.compile(r"\b(pacific|eastern|central|mountain)\s+time\s*zones?\b", re.I), "United States/North America", "Restricted to US time zones"),
)

VISA_RESTRICTED_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"\b(us|u\.s\.|united states)\s+work authorization\b", re.I), "Requires United States work authorisation"),
    (re.compile(r"\bauthorized to work in (the )?(us|u\.s\.|united states)\b", re.I), "Requires United States work authorisation"),
    (re.compile(r"\bmust have (existing )?work authorization in (the )?(us|u\.s\.|united states|uk|canada|eu|europe)\b", re.I), "Requires existing work authorisation outside Australia"),
    (re.compile(r"\bwe (do not|don't|cannot|can't) sponsor visas?\b", re.I), "No visa sponsorship"),
    (re.compile(r"\bno visa sponsorship\b", re.I), "No visa sponsorship"),
)

NON_STARTUP_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"\bgovernment department\b|\bpublic sector\b|\bfederal government\b|\blocal government\b", re.I), "Public-sector role"),
    (re.compile(r"\bfortune 500\b|\bglobal enterprise\b|\blarge enterprise\b", re.I), "Large-enterprise signal"),
    (re.compile(r"\bbig four\b|\bconsulting firm\b|\bmanagement consulting\b", re.I), "Consulting/non-startup signal"),
    (re.compile(r"\bmajor bank\b|\bbig bank\b|\binvestment bank\b", re.I), "Large-bank signal"),
)

SENIORITY_HARD_FLOOR_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"\b1[5-9]\+?\s+years\b|\b2\d\+?\s+years\b", re.I), "Very high years-of-experience requirement"),
    (re.compile(r"\bvp[-\s]?level\b|\bexecutive[-\s]?only\b|\bc-suite\b", re.I), "Executive-level hard floor"),
    (re.compile(r"\bgraduate program\b|\binternship\b|\bstudent only\b", re.I), "Student/graduate-only role"),
)

NON_JOB_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"\bphd scholarship\b|\bscholarship opportunity\b|\bstudentship\b|\bdoctoral scholarship\b", re.I), "Scholarship or studentship, not a standard job"),
    (re.compile(r"\bvolunteer role\b|\bunpaid internship\b|\bunpaid role\b", re.I), "Unpaid or volunteer listing"),
)

# Matched against the whole (trimmed) company_name field, not free text, so a real
# company would have to be named exactly "Confidential" etc. to trip these.
GENERIC_COMPANY_NAME_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"^our (other )?(client|company|business)s?$", re.I),
    re.compile(r"^(a |the )?confidential( client| company| employer)?$", re.I),
    re.compile(r"^(undisclosed|anonymous|private) (client|company|employer)$", re.I),
    re.compile(r"^(leading|major|large|reputable) (company|employer|business|organisation|organization)$", re.I),
    re.compile(r"^(my |a )?client( company)?$", re.I),
    re.compile(r"^unknown( company| employer)?$", re.I),
    re.compile(r"^(name withheld|not disclosed|n/a|tba|tbc|na)$", re.I),
)


def is_generic_company_name(company_name: str | None) -> bool:
    normalized = re.sub(r"\s+", " ", (company_name or "").strip())
    if not normalized:
        return False
    return any(pattern.match(normalized) for pattern in GENERIC_COMPANY_NAME_PATTERNS)

AUSTRALIA_ELIGIBLE_PATTERNS: tuple[tuple[re.Pattern[str], str, str], ...] = (
    (re.compile(r"\baustralia\b", re.I), "Australia", "Mentions Australia eligibility"),
    (re.compile(r"\bapac\b|\basia pacific\b", re.I), "APAC", "Mentions APAC eligibility"),
    (re.compile(r"\banz\b|\baustralia/new zealand\b", re.I), "Australia/ANZ", "Mentions ANZ eligibility"),
)

AUSTRALIA_EXCLUSION_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (
        re.compile(
            r"\b(?:cannot|can'?t|unable to|won'?t|do not|don'?t)\s+"
            r"(?:hire|employ|accept|consider|support)\b[^.]{0,50}\b"
            r"(?:candidates?\s+)?(?:in|from)\s+australia\b",
            re.I,
        ),
        "Description explicitly excludes Australia",
    ),
    (re.compile(r"\bexcept\s+australia\b|\bexcluding\s+australia\b", re.I), "Excludes Australia"),
    (
        re.compile(
            r"\bnot\s+(?:available|open|eligible)\s+(?:to|for|in)\s+(?:(?!\boutside\b)[^.]){0,20}\baustralia\b",
            re.I,
        ),
        "Not open to Australia",
    ),
)


def classify_australia_exclusion(text: str) -> "LocationEligibility | None":
    for pattern, reason in AUSTRALIA_EXCLUSION_PATTERNS:
        if pattern.search(text):
            return LocationEligibility("restricted_remote", "Australia excluded", reason)
    return None


def apply_disqualification_scan(job: dict[str, Any]) -> dict[str, Any]:
    eligibility = classify_location_eligibility(job)
    if eligibility.region and not _has_australia_signal(eligibility.region):
        job["remote_region"] = eligibility.region
        if eligibility.is_restricted:
            job["location"] = f"Remote - {eligibility.region}"
    if eligibility.status != "unknown_remote":
        job["remote_eligibility"] = eligibility.status
        if eligibility.status == "australia_eligible":
            job["remote_eligibility_score"] = 0.9
        elif eligibility.status == "restricted_remote":
            job["remote_eligibility_score"] = 0.15
        else:
            job["remote_eligibility_score"] = 0.0
    if eligibility.reason:
        job["location_eligibility_reason"] = eligibility.reason

    signals = scan_disqualifying_signals(job)
    if signals:
        job["disqualification_signals"] = [
            {"category": signal.category, "severity": signal.severity, "reason": signal.reason}
            for signal in signals
        ]
        job["screening_reasons"] = "; ".join(signal.reason for signal in signals[:3])
        job["screening_status"] = "suppressed" if any(signal.severity == "suppress" for signal in signals) else "penalized"
        job["ranking_penalty"] = min(0.35, sum(signal.penalty for signal in signals))
    return job


def should_skip_disqualified_job(job: dict[str, Any]) -> bool:
    if str(job.get("remote_eligibility") or "") == "restricted_remote":
        return True
    return str(job.get("screening_status") or "") == "suppressed"


def apply_location_eligibility(job: dict[str, Any]) -> dict[str, Any]:
    return apply_disqualification_scan(job)


def should_skip_location_restricted_job(job: dict[str, Any]) -> bool:
    return should_skip_disqualified_job(job)


def apply_himalayas_location_eligibility(job: dict[str, Any]) -> dict[str, Any]:
    return apply_location_eligibility(job)


def should_skip_himalayas_job(job: dict[str, Any]) -> bool:
    return should_skip_location_restricted_job(job)


def scan_disqualifying_signals(job: dict[str, Any]) -> list[DisqualificationSignal]:
    text = searchable_text(job)
    signals: list[DisqualificationSignal] = []

    if not _has_australia_signal(text):
        for pattern, reason in VISA_RESTRICTED_PATTERNS:
            if pattern.search(text):
                signals.append(DisqualificationSignal("visa", "suppress", reason))
                break

    if not _has_positive_startup_signal(text):
        for pattern, reason in NON_STARTUP_PATTERNS:
            if pattern.search(text):
                signals.append(DisqualificationSignal("company_stage", "penalize", reason, 0.12))
                break

    for pattern, reason in SENIORITY_HARD_FLOOR_PATTERNS:
        if pattern.search(text):
            signals.append(DisqualificationSignal("seniority", "penalize", reason, 0.1))
            break

    for pattern, reason in NON_JOB_PATTERNS:
        if pattern.search(text):
            signals.append(DisqualificationSignal("non_job", "suppress", reason))
            break

    if is_generic_company_name(job.get("company_name")):
        signals.append(
            DisqualificationSignal(
                "company_identity", "penalize", "Employer name is a placeholder, not a real company", 0.15
            )
        )

    return signals


def classify_location_eligibility(job: dict[str, Any]) -> LocationEligibility:
    text = searchable_text(job)

    exclusion = classify_australia_exclusion(text)
    if exclusion:
        return exclusion

    restricted = classify_with_rules(text)
    if restricted:
        return restricted

    llm_result = classify_with_openai(job) if should_ask_openai(job, text) else None
    if llm_result:
        return llm_result

    for pattern, region, reason in AUSTRALIA_ELIGIBLE_PATTERNS:
        if pattern.search(text):
            return LocationEligibility("australia_eligible", region, reason)

    if re.search(r"\b(worldwide|global|anywhere|open to candidates from all countries)\b", text, re.I):
        return LocationEligibility("australia_eligible", "Global", "Worldwide/global remote signal")

    if re.search(r"\b(remote|work from home|work-from-home)\b", text, re.I):
        return LocationEligibility("unknown_remote", "Remote", "Remote role without clear Australia eligibility")

    return LocationEligibility("not_remote", None, "No remote signal")


def classify_with_rules(text: str) -> LocationEligibility | None:
    for pattern, region, reason in RESTRICTED_PATTERNS:
        if pattern.search(text):
            return LocationEligibility("restricted_remote", region, reason)
    return None


def classify_with_openai(job: dict[str, Any]) -> LocationEligibility | None:
    api_key = settings.llm_judge_api_key
    if not settings.llm_location_check_enabled or not api_key:
        return None

    try:
        response = requests.post(
            f"{settings.llm_judge_base_url.rstrip('/')}/chat/completions",
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            json=build_location_request(job),
            timeout=20,
        )
        response.raise_for_status()
        content = response.json()["choices"][0]["message"]["content"]
        return parse_location_response(content)
    except Exception:
        return None


def should_ask_openai(job: dict[str, Any], text: str) -> bool:
    if any(pattern.search(text) for pattern, _region, _reason in AUSTRALIA_ELIGIBLE_PATTERNS):
        return False
    source_type = str(job.get("source_type") or "").lower()
    if source_type == "remote_board":
        return True
    return bool(re.search(r"\b(remote|work from home|work-from-home|worldwide|global|anywhere)\b", text, re.I))


def build_location_request(job: dict[str, Any]) -> dict[str, Any]:
    return {
        "model": settings.llm_judge_model,
        "temperature": 0,
        "messages": [
            {
                "role": "system",
                "content": (
                    "You classify whether a remote job is open to a person living in Australia. "
                    "Prefer exact evidence from the job description over generic source headings. "
                    "Return only valid JSON."
                ),
            },
            {
                "role": "user",
                "content": json.dumps(
                    {
                        "task": "Classify location eligibility for an Australia-based candidate.",
                        "allowed_statuses": ["australia_eligible", "restricted_remote", "unknown_remote", "not_remote"],
                        "output_schema": {
                            "status": "one allowed status",
                            "region": "specific allowed region or null",
                            "reason": "short evidence phrase",
                        },
                        "job": {
                            "title": job.get("title"),
                            "company": job.get("company_name"),
                            "location": job.get("location"),
                            "remote_region": job.get("remote_region"),
                            "description": (job.get("description") or "")[:3500],
                        },
                    },
                    ensure_ascii=True,
                ),
            },
        ],
    }


def parse_location_response(content: str) -> LocationEligibility | None:
    data = json.loads(strip_code_fence(content))
    status = data.get("status")
    if status not in {"australia_eligible", "restricted_remote", "unknown_remote", "not_remote"}:
        return None
    region = data.get("region")
    reason = data.get("reason")
    return LocationEligibility(
        status=status,
        region=region if isinstance(region, str) and region.strip() else None,
        reason=reason[:180] if isinstance(reason, str) else None,
    )


def strip_code_fence(content: str) -> str:
    text = content.strip()
    if text.startswith("```"):
        lines = text.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].startswith("```"):
            lines = lines[:-1]
        text = "\n".join(lines).strip()
    return text


def searchable_text(job: dict[str, Any]) -> str:
    return " ".join(
        str(value or "")
        for value in (
            job.get("title"),
            job.get("company_name"),
            job.get("location"),
            job.get("remote_region"),
            job.get("description"),
        )
    ).lower()


def _has_australia_signal(value: str) -> bool:
    return bool(re.search(r"\baustralia\b|\bapac\b|\banz\b|\basia pacific\b", value, re.I))


def _has_positive_startup_signal(value: str) -> bool:
    return bool(
        re.search(
            r"\bstartup\b|\bscaleup\b|\bscale-up\b|\bventure-backed\b|\bvc-backed\b|\bseed\b|\bseries [abc]\b|\bfounder-led\b|\bearly-stage\b",
            value,
            re.I,
        )
    )
