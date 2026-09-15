#!/usr/bin/env bash
# Source from the repository root. Optional activation is selected by deploy.sh;
# every optional writer still participates in shutdown and rollback discovery.
required_runtime_services=(web scheduler jobs-worker memory-worker memory-scheduler password-email-worker community-email-worker)
bridge_runtime_services=(bridge-worker bridge-reconciler bridge-retention)
analytics_runtime_services=(analytics-sync)
committee_runtime_services=(committee-remuneration)
all_runtime_writer_services=(
    "${required_runtime_services[@]}"
    "${bridge_runtime_services[@]}"
    "${analytics_runtime_services[@]}"
    "${committee_runtime_services[@]}"
)
runtime_services=("${required_runtime_services[@]}")
