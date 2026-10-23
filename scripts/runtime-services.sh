#!/usr/bin/env bash
# Source from the repository root. Optional activation is selected by deploy.sh;
# every optional writer still participates in shutdown and rollback discovery.
required_runtime_services=(web scheduler jobs-worker memory-worker memory-scheduler password-email-worker community-email-worker)
bridge_runtime_services=(bridge-worker bridge-retention)
# Kept in pause/rollback discovery for one release while its old container retires.
retiring_runtime_services=(bridge-reconciler)
analytics_runtime_services=(analytics-sync)
native_agent_runtime_services=(native-agent-worker)
community_agent_runtime_services=(community-agent-worker)
event_discount_runtime_services=(event-discount-worker)
# The transient handoff slot is managed explicitly, never normal startup.
handoff_runtime_services=(web-candidate)
all_runtime_writer_services=(
    "${required_runtime_services[@]}"
    "${bridge_runtime_services[@]}"
    "${retiring_runtime_services[@]}"
    "${analytics_runtime_services[@]}"
    "${handoff_runtime_services[@]}"
    "${native_agent_runtime_services[@]}"
    "${community_agent_runtime_services[@]}"
    "${event_discount_runtime_services[@]}"
)
runtime_services=("${required_runtime_services[@]}")
