"""Safe worker diagnostics: identifiers and error types, never work content."""

import json


def report_failure(logger, stage, error, *, stdout=None, subscription_id=None):
    """Capture a safe failure without serializing work content or stack locals."""
    record = {"stage": stage, "error_code": "agent_stage_failed", "error_type": type(error).__name__}
    if subscription_id is not None:
        record["subscription_id"] = str(subscription_id)
    line = json.dumps(record, sort_keys=True)
    if stdout is None:
        print(line, flush=True)
    else:
        stdout.write(line)
    sanitized = RuntimeError("agent_stage_failed")
    logger.exception("Agent worker stage failed", extra=record,
                     exc_info=(RuntimeError, sanitized, None))
