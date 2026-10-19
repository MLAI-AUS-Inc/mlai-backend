"""Optional B5 formatting metadata; never workflow or repository authority."""

MARKER = "workflow_order_v1"
KEY = "_mlai_workflow_order"
ADMISSION = {"version": 1}


def admitted(request):
    """Use only the original run's explicit paired formatting admission."""
    if not isinstance(request, dict):
        return False
    value = request.get(MARKER)
    return (isinstance(value, dict) and set(value) == {"version"}
        and type(value["version"]) is int and value["version"] == 1)


def validated_order(value, step_states):
    """Return a declared exact sequence or an optional-metadata miss."""
    if (not isinstance(value, dict) or set(value) != {"version", "steps"}
            or type(value.get("version")) is not int or value["version"] != 1
            or not isinstance(step_states, dict)):
        return None
    steps = value["steps"]
    if (not isinstance(steps, list) or len(steps) > 512
            or any(not isinstance(step, str) or not 1 <= len(step) <= 120 for step in steps)
            or len(set(steps)) != len(steps) or set(steps) != set(step_states)):
        return None
    return list(steps)


def reserve_marker(saved, payload, prefix, *, new_run):
    """Admit formatting only at original owned B5 reservation; absence is sticky."""
    from django.conf import settings
    original = saved.get(prefix + "_request")
    payload.pop(MARKER, None)
    if (isinstance(original, dict) and admitted(original)) or (
            new_run and original is None and
            getattr(settings, "CONTENT_FACTORY_WORKFLOW_ORDER_FORMATTING_ADMISSION_ENABLED", False) is True):
        payload[MARKER] = dict(ADMISSION)


def original_admission(run, operation, envelope):
    """Require paired original backend reservation, never worker result claims."""
    article = envelope["workflow"] in {"direct_generate", "confirmed_topic"}
    prefix = "article_delivery" if article else "setup_delivery"
    original = (operation.payload or {}).get(prefix + "_request")
    return isinstance(original, dict) and admitted(original) and admitted(run.run_request or {})


def received_order(run, operation, envelope):
    """Read optional order only for the backend's original current dispatch."""
    if not original_admission(run, operation, envelope):
        return None
    projection = envelope["body"]["state_projection"]
    setup = projection["result"].get("article_system_setup")
    if not isinstance(setup, dict):
        return None
    return validated_order(setup.get(KEY), projection["step_states"])
