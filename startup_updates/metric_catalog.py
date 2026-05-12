from __future__ import annotations

from typing import Optional


STARTUP_UPDATE_METRIC_LABELS: dict[str, str] = {
    "revenue": "Revenue",
    "activeUsers": "Active Users",
    "mrr": "MRR",
    "burnRate": "Burn Rate",
    "runway": "Runway",
    "monthlyCosts": "Monthly Costs",
    "invoiceRevenue": "Invoice Revenue",
    "cashCollected": "Cash Collected",
    "revenueGrowthRate": "Revenue Growth Rate",
    "customerCount": "Customer Count",
    "churn": "Churn",
    "invoiceCount": "Invoice Count",
    "recurringInvoiceCount": "Recurring Invoice Count",
    "websiteVisitors": "Website Visitors",
    "waitlistSignups": "Waitlist Signups",
    "demoRequests": "Demo Requests",
    "customerInterviews": "Customer Interviews",
    "experimentsRun": "Experiments Run",
    "pilotCount": "Pilots",
    "qualifiedPipeline": "Qualified Pipeline",
}

STARTUP_UPDATE_METRIC_KEYS: tuple[str, ...] = tuple(STARTUP_UPDATE_METRIC_LABELS)
STARTUP_UPDATE_METRIC_KEY_SET = frozenset(STARTUP_UPDATE_METRIC_KEYS)

_METRIC_ALIASES = {
    "revenue": "revenue",
    "monthly revenue": "revenue",
    "active users": "activeUsers",
    "users": "activeUsers",
    "monthly active users": "activeUsers",
    "mau": "activeUsers",
    "mrr": "mrr",
    "monthly recurring revenue": "mrr",
    "burn rate": "burnRate",
    "burn": "burnRate",
    "runway": "runway",
    "monthly costs": "monthlyCosts",
    "monthly cost": "monthlyCosts",
    "costs": "monthlyCosts",
    "costs per month": "monthlyCosts",
    "monthly expenses": "monthlyCosts",
    "expenses": "monthlyCosts",
    "invoice revenue": "invoiceRevenue",
    "sales invoice revenue": "invoiceRevenue",
    "cash collected": "cashCollected",
    "cash received": "cashCollected",
    "revenue growth": "revenueGrowthRate",
    "revenue growth rate": "revenueGrowthRate",
    "mrr growth": "revenueGrowthRate",
    "mrr growth rate": "revenueGrowthRate",
    "customer count": "customerCount",
    "customers": "customerCount",
    "churn": "churn",
    "invoice count": "invoiceCount",
    "invoices": "invoiceCount",
    "invoices sent": "invoiceCount",
    "invoices sent out": "invoiceCount",
    "recurring invoice count": "recurringInvoiceCount",
    "repeating invoice count": "recurringInvoiceCount",
    "website visitors": "websiteVisitors",
    "website traffic": "websiteVisitors",
    "site visitors": "websiteVisitors",
    "unique visitors": "websiteVisitors",
    "waitlist signups": "waitlistSignups",
    "waitlist": "waitlistSignups",
    "waitlist users": "waitlistSignups",
    "demo requests": "demoRequests",
    "demos requested": "demoRequests",
    "customer interviews": "customerInterviews",
    "potential customers interviewed": "customerInterviews",
    "potential customer interviews": "customerInterviews",
    "experiments run": "experimentsRun",
    "experiments": "experimentsRun",
    "pilot count": "pilotCount",
    "pilots": "pilotCount",
    "qualified pipeline": "qualifiedPipeline",
    "pipeline": "qualifiedPipeline",
    "pipeline usd": "qualifiedPipeline",
    "pipeline_usd": "qualifiedPipeline",
}


def startup_update_metric_key(value: object) -> Optional[str]:
    text = str(value or "").strip()
    if not text:
        return None
    if text in STARTUP_UPDATE_METRIC_KEY_SET:
        return text
    normalized = text.replace("_", " ").replace("-", " ").strip().lower()
    return _METRIC_ALIASES.get(normalized)


def startup_update_metric_label(metric_key: str) -> str:
    return STARTUP_UPDATE_METRIC_LABELS.get(metric_key, metric_key)
