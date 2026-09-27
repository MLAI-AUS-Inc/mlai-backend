"""Recent Slack timestamps for mirror tests with a rolling consent window."""

from datetime import datetime, timezone


_RECENT_EPOCH = int(datetime.now(tz=timezone.utc).timestamp()) - 2 * 86_400


def recent_slack_ts(value: str) -> str:
    """Keep fixture ordering while placing messages inside the 30-day window."""

    seconds, fraction = value.split(".", 1)
    original = int(seconds)
    if 1787900000 <= original < 1788000000:
        base = 1787900000
    elif 1788800000 <= original < 1788900000:
        base = 1788800000
    else:
        raise ValueError("Unknown recent Slack fixture timestamp")
    return f"{_RECENT_EPOCH + original - base}.{fraction}"
