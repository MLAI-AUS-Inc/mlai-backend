"""Main 6bbe177f persistence oracle for synthetic Pulse PUT replay tests."""

def _sync_content_factory_run_snapshot(*, run_id: str, data: dict, step_states: dict):
    data = sanitize_json_for_postgres(data if isinstance(data, dict) else {})
    step_states = sanitize_json_for_postgres(step_states if isinstance(step_states, dict) else {})
    with transaction.atomic():
        locked_runs = (
            ContentFactoryRun.objects.select_for_update()
            .prefetch_related("steps", "steps__attempt_history")
        )
        existing_run = locked_runs.filter(run_id=run_id).first()
        created = False
        if existing_run is None:
            # A missing-row lock does not reserve a run id. Validate before
            # insertion, then merge again with the row returned by get_or_create
            # in case another callback created it first. The locking queryset
            # also locks that existing/race-winning row before reconciliation.
            data = merge_editorial_run_snapshot(None, data)
            data["run_request"] = _merge_django_owned_run_request({}, data.get("run_request"))
            existing_run, created = locked_runs.get_or_create(
                run_id=run_id,
                defaults={
                    "workflow": data["workflow"], "domain": data.get("domain") or "",
                    "status": data["status"], "run_request": data.get("run_request") or {},
                },
            )
        original_request = existing_run.run_request if isinstance(existing_run.run_request, dict) else {}
        from .portable_drafts import portable_run_update_allowed
        from .website_contract import connection_contract
        if original_request.get("delivery_mode") == "content_only" and not connection_contract(original_request):
            if not portable_run_update_allowed(existing_run, data):
                raise EditorialRunConflict("A portable draft snapshot cannot acquire repository authority")
            # Only a previously confirmed original reaches this branch. Sparse
            # worker snapshots cannot erase that consent and strand later
            # callbacks or revision resumes; worker claims never create it.
            data["run_request"] = {**(data.get("run_request") or {}),
                                   "delivery_mode": "content_only", "delivery_mode_confirmed": True}
            data["domain"] = existing_run.domain
        # Worker observations may omit request fields. The known reader/offer
        # decision is immutable history, not a field a sparse callback can clear.
        data = merge_editorial_run_snapshot(
            {
                "workflow": existing_run.workflow,
                "domain": existing_run.domain,
                "run_request": existing_run.run_request,
            } if existing_run is not None else None,
            data,
        )
        data["run_request"] = _merge_django_owned_run_request(original_request, data.get("run_request"))
        if existing_run and stale_execution_event(existing_run.result, data, saved_status=existing_run.status):
            existing_run._content_factory_sync_unchanged = True
            return existing_run, False
        active_snapshot = str(data.get("status") or "").strip().lower() in DURABLE_ACTIVE_RUN_STATUSES
        if active_snapshot:
            data["error"] = ""
        data["result"] = _merge_django_owned_run_result(
            existing_run.result if existing_run is not None else {},
            data.get("result"),
        )
        if existing_run is not None and existing_run.workflow == "startup_monthly_update":
            # Worker checkpoints cannot replace source receipts, approval inputs,
            # connector authority or founder choices with a stale local copy.
            data["run_request"] = existing_run.run_request
            result = dict(data.get("result") or {})
            for key in ("source_evidence", "update_candidates", "_cancel_backups"):
                if key in (existing_run.result or {}):
                    result[key] = existing_run.result[key]
            data["result"] = result
        if active_snapshot:
            # Clean after the Django-owned merge as a final invariant: no local
            # augmentation may reintroduce a blocker into an active snapshot.
            data["result"] = clear_obsolete_active_run_blockers(
                data.get("result"),
                active_status=data["status"],
                current_step=data.get("current_step") or "",
            )
        data["result"] = merge_reliability_fields(data.get("result"), data)
        if not created and _content_factory_run_snapshot_unchanged(existing_run, data=data, step_states=step_states):
            existing_run._content_factory_sync_unchanged = True
            from .island_research import refund_empty_or_failed_research
            refund_empty_or_failed_research(existing_run)
            return existing_run, False

        run, _ = ContentFactoryRun.objects.update_or_create(
            run_id=run_id,
            defaults={
                "workflow": data["workflow"],
                "domain": data.get("domain") or "",
                "github_repo": data.get("github_repo") or "",
                "slack_user_id": data.get("slack_user_id") or "",
                "status": data["status"],
                "current_step": data.get("current_step") or "",
                "approval_state": data.get("approval_state") or ContentFactoryApprovalState.NOT_REQUIRED,
                "artifact_root": data.get("artifact_root") or "",
                "step_order": data.get("step_order") or [],
                "acceptance_summary": data.get("acceptance_summary") or {},
                "verification_summary": data.get("verification_summary") or {},
                "run_request": data.get("run_request") or {},
                "result": data.get("result") or {},
                "error": data.get("error") or "",
                "resume_available": bool(data.get("resume_available")),
            },
        )

        seen_steps = set()
        ordered_steps = data.get("step_order") or list(step_states.keys())
        for index, step_key in enumerate(ordered_steps):
            state_payload = dict(step_states.get(step_key) or {"name": step_key})
            step, _ = ContentFactoryRunStep.objects.update_or_create(
                run=run,
                step_key=step_key,
                defaults={
                    "display_order": index,
                    "required": bool(state_payload.get("required", True)),
                    "status": state_payload.get("status", ContentFactoryStepStatus.PENDING),
                    "attempts": int(state_payload.get("attempts", 0)),
                    "message": state_payload.get("message") or "",
                    "started_at": _parse_optional_datetime(state_payload.get("started_at")),
                    "completed_at": _parse_optional_datetime(state_payload.get("completed_at")),
                    "error": state_payload.get("error") or "",
                    "latest_attempt_path": state_payload.get("latest_attempt_path") or "",
                    "artifacts": state_payload.get("artifacts") or [],
                },
            )
            seen_steps.add(step_key)

            for attempt_payload in state_payload.get("attempt_history", []):
                ContentFactoryRunStepAttempt.objects.update_or_create(
                    step=step,
                    attempt=int(attempt_payload.get("attempt", 0)),
                    defaults={
                        "status": attempt_payload.get("status", ContentFactoryStepStatus.PENDING),
                        "message": attempt_payload.get("message") or "",
                        "started_at": _parse_optional_datetime(attempt_payload.get("started_at")),
                        "completed_at": _parse_optional_datetime(attempt_payload.get("completed_at")),
                        "artifacts": attempt_payload.get("artifacts") or [],
                        "error": attempt_payload.get("error") or "",
                        "input_path": attempt_payload.get("input_path") or "",
                        "output_path": attempt_payload.get("output_path") or "",
                        "notes_path": attempt_payload.get("notes_path") or "",
                        "status_path": attempt_payload.get("status_path") or "",
                    },
                )

        if seen_steps:
            ContentFactoryRunStep.objects.filter(run=run).exclude(step_key__in=seen_steps).delete()

    from .island_research import refund_empty_or_failed_research
    refund_empty_or_failed_research(run)
    return run, created
