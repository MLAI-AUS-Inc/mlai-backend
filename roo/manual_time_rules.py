"""Shared catalogue and exact calculation for manually logged volunteer time."""

import json
from pathlib import Path
from uuid import UUID

CATALOGUE = json.loads(Path(__file__).with_name("manual_time_catalogue.json").read_text())
ACTIONS = {action["id"]: action for group in CATALOGUE["groups"] for action in group["actions"]}
# Keep the result exactly representable in the clients' JSON numbers.
MAX_MINUTES = (2**53 - 1) // 100_000


class ManualTimeError(ValueError):
    def __init__(self, message, status=400):
        super().__init__(message)
        self.status = status


def normalize(data):
    if data.get("catalog_version") != CATALOGUE["version"]:
        raise ManualTimeError("Refresh the form to load the current activity list.", 409)
    minutes = data.get("volunteer_minutes")
    if type(minutes) is not int or not 0 < minutes <= MAX_MINUTES:
        raise ManualTimeError("Enter a positive total of whole minutes.")
    actions = data.get("action_ids")
    if not isinstance(actions, list) or not actions or any(
        not isinstance(action, str) or action not in ACTIONS for action in actions
    ):
        raise ManualTimeError("Choose at least one listed activity.")
    actions = sorted(set(actions))
    note = data.get("note", "")
    if not isinstance(note, str) or len(note) > 500:
        raise ManualTimeError("Keep the note to 500 characters.")
    note = note.strip()
    if "other" in actions and not note:
        raise ManualTimeError("Briefly describe the unlisted work.")
    try:
        key = str(UUID(data.get("submission_key", "")))
    except (ValueError, AttributeError, TypeError):
        raise ManualTimeError("Invalid submission key.") from None
    return key, {
        "catalog_version": CATALOGUE["version"],
        "action_ids": actions,
        "volunteer_minutes": minutes,
        "amount_microroo": minutes * 100_000,
        "note": note,
    }


def roo_text(minutes):
    whole, fraction = divmod(minutes, 10)
    return f"{whole}.{fraction}" if fraction else str(whole)
