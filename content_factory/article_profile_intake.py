"""Fill missing article facts under the existing founder owner and catalogue rules."""
from copy import deepcopy

from django.db import transaction
from django.utils import timezone

from . import content_first_article as card
from .editorial_catalog import CATALOG_KEY, review_payload, update_catalog
from .editorial_first_audience import _default_name, _reader_task

KEY = "founder_article_profile_intake_v1"
LIMITS = {"oneLiner": (3, card.ONE_LINER_MAX), "targetAudience": (card.AUDIENCE_MIN, card.AUDIENCE_MAX), "readerTask": (3, 280)}


def _state(config, facts):
    strategy = getattr(config, "pillar_strategy", {})
    if not isinstance(strategy, dict):
        return None
    try:
        catalog = review_payload(strategy)
    except (TypeError, ValueError):
        return None
    raw = strategy.get(CATALOG_KEY, {})
    stored = strategy.get(KEY, {"answers": {}})
    answers = stored.get("answers") if isinstance(stored, dict) else None
    if not isinstance(answers, dict) or any(not isinstance(answer, dict) or not isinstance(answer.get("value"), str)
                                            for answer in answers.values()):
        return None
    audiences = catalog["audience_options"]
    approved = [row for row in audiences if row["status"] == "approved"]
    if approved:
        audience = next((row for row in approved if row.get("allow_no_offer")), approved[0])
    else:
        # Keep retired, withdrawn, invalid and independently configured offer choices
        # on the catalogue's own review path; intake cannot reset an earlier policy.
        if (raw.get("approval_history") or any((raw.get("approval_receipts") or {}).values()) or catalog["cta_options"]
                or len(audiences) > 1 or any(row["status"] != "draft" for row in audiences)):
            return None
        audience = audiences[0] if audiences else {}
    description = audience.get("description") or facts.get("targetAudience") or ""
    reader_task = audience.get("reader_task", "")
    if (not approved and reader_task == _reader_task(description)
            and (answers.get("readerTask") or {}).get("value") != reader_task):
        reader_task = ""
    values = {"oneLiner": facts.get("oneLiner"), "targetAudience": description, "readerTask": reader_task}
    # An incomplete intake can record the reader's objective before an audience exists.
    if not values["readerTask"] and not approved:
        values["readerTask"] = (answers.get("readerTask") or {}).get("value")
    return catalog, audience, values


def intake_hint(config, facts):
    state = _state(config, facts)
    if state is None:
        return {}
    missing = [name for name in LIMITS if not state[2].get(name)]
    return {"profileIntake": {"missingFields": missing}} if missing else {}


def _validate(data):
    if not isinstance(data, dict) or not data or data.keys() - LIMITS.keys():
        raise card.FirstArticleRefusal(400, "Send the missing article details shown on your card.", "invalid_profile")
    normalised = {}
    for key, value in data.items():
        low, high = LIMITS[key]
        text = " ".join(value.split()) if isinstance(value, str) else ""
        if not low <= len(text) <= high:
            raise card.FirstArticleRefusal(400, f"Each answer must be between {low} and {high} characters.", "invalid_profile")
        normalised[key] = text
    return normalised


@transaction.atomic
def save(context, user, data):
    from .startup_first_profile import _locked_config
    from startup_updates.models import StartupProfile

    organization = context.organization
    card.require_owner(user, organization)
    supplied = _validate(data)
    config = _locked_config(organization)
    card.require_owner(user, organization)
    profile = StartupProfile.objects.select_for_update().filter(organization=organization).first()
    facts = card.profile_facts(context.company, organization, config)
    state = _state(config, facts)
    if state is None:
        raise card.FirstArticleRefusal(409, "Review the existing audience and offers before changing these details.", "not_available")
    catalog, audience, values = state
    strategy = deepcopy(config.pillar_strategy)
    record = strategy.get(KEY, {"answers": {}})
    answers = record["answers"]
    changed = {}
    for key, text in supplied.items():
        if values[key]:
            if values[key] != text:
                raise card.FirstArticleRefusal(409, "Your saved article details changed. Refresh the card.", "not_available")
            continue
        changed[key] = text
        answers[key] = {"value": text, "source": "founder", "userId": str(user.pk), "suppliedAt": timezone.now().isoformat()}
    if not changed:
        return card.card_payload(context, user, config=config, include_profile_intake=True)
    if "oneLiner" in changed or "targetAudience" in changed:
        if profile is None:
            profile, _ = StartupProfile.objects.select_for_update().get_or_create(organization=organization)
        updates = []
        for key, column in (("oneLiner", "short_description"), ("targetAudience", "target_audience")):
            if key not in changed:
                continue
            current = getattr(profile, column)
            if current and current != changed[key]:
                raise card.FirstArticleRefusal(409, "Your startup profile changed. Refresh the card.", "not_available")
            setattr(profile, column, changed[key])
            updates.append(column)
        profile.save(update_fields=[*updates, "updated_at"])
    values.update(changed)
    if all(values.values()) and audience.get("status") != "approved":
        description = values["targetAudience"]
        if audience:
            option = {**audience, "reader_task": values["readerTask"]}
            if option != audience:
                option["version"] += 1
        else:
            option = card._new_audience_option({"name": _default_name(description), "description": description}, set())
            option["reader_task"] = values["readerTask"]
        record["draftAudienceId"] = option["id"]
        if option != audience:
            strategy = update_catalog(strategy, {"expected_editorial_catalog_version": catalog["editorial_catalog_version"],
                                                "audience_options": [option]})
    strategy[KEY] = record
    config.pillar_strategy = strategy
    config.save(update_fields=["pillar_strategy", "updated_at"])
    return card.card_payload(context, user, config=config, include_profile_intake=True)
