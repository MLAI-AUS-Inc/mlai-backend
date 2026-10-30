"""Saved audiences: a JSON rule set, validated without the database and run through the ORM.

A definition is ``{"match": "all" | "any", "rules": [...], "exclude": [...]}``.
``validate_definition`` is pure and returns the canonical form or raises
``SegmentError`` with a JSON pointer. ``evaluate`` builds ``Q`` and ``Exists``
expressions only, never SQL text, then applies the email suppression rules and
the blast purpose's rule whatever the segment says.
"""

from __future__ import annotations

import re
import uuid
from datetime import datetime, timedelta
from functools import reduce
from operator import and_, or_
from typing import Any, Optional

from django.db.models import Count, Exists, OuterRef, Q
from django.db.models.fields.json import KeyTextTransform
from django.db.models.lookups import IContains, IExact, Regex
from django.utils import timezone

from community_chat.models import CommunityChatDevice, DeviceBindingStatus

from .luma_guests import APPROVED, WAITLIST
from .models import BlastDelivery, Channel, Contact, CrmEvent, EventRegistration
from .queries import (
    PENDING,
    REACHABLE,
    banned,
    marketing_consent_q,
    registered_q,
)

MATCH = ("all", "any")
PURPOSES = ("marketing", "event_update")
MAX_RULES = 50
MAX_LIST_ITEMS = 50
MAX_TEXT = 300
MAX_LABEL = 500
MAX_COUNT_VALUE = 10_000
MAX_DAYS = 3650
# Luma answers are a JSON list; rules check this many leading answers, which
# covers any registration form MLAI uses.
ANSWER_SCAN_LIMIT = 30
REGISTRATION_STATUSES = {"going": APPROVED, "waitlist": WAITLIST, "pending": PENDING}
COUNT_OPS = ("gte", "lte", "eq")
_COUNT_LOOKUPS = {"gte": "gte", "lte": "lte", "eq": "exact"}
TAG_OPS = ("has_any", "has_all", "has_none")
TEXT_OPS = ("contains", "equals", "is_set", "is_not_set")
VALUE_OPS = ("contains", "equals")
ENGAGEMENT_ACTIONS = ("opened", "clicked")
FIELDS = {
    "email": "Email",
    "first_name": "First name",
    "last_name": "Last name",
    "display_name": "Name",
    "company": "Company",
    "job_title": "Job title",
    "city": "City",
    "linkedin_url": "LinkedIn",
    "phone_e164": "Mobile number",
}
ATTRIBUTE_KEY = re.compile(r"^[a-z0-9][a-z0-9_]{0,254}$")
_NON_BLANK = r"\S"

RULE_TYPES = {
    "event_registration": {
        "label": "Registered for an event",
        "keys": {"event_ids", "statuses", "checked_in"},
        "available": True,
    },
    "events_attended_count": {
        "label": "Number of events attended",
        "keys": {"op", "value"},
        "available": True,
    },
    "last_attended_within_days": {
        "label": "Attended an event in the last N days",
        "keys": {"value"},
        "available": True,
    },
    "tag": {"label": "Tags", "keys": {"op", "values"}, "available": True},
    "field": {"label": "Profile field", "keys": {"field", "op", "value"}, "available": True},
    "attribute": {"label": "Imported attribute", "keys": {"key", "op", "value"}, "available": True},
    "registration_answer": {
        "label": "Answer to a registration question",
        "keys": {"label", "op", "value"},
        "available": True,
    },
    "chat_member": {"label": "Has an MLAI Chat account", "keys": {"value"}, "available": True},
    "source": {"label": "Where the contact came from", "keys": {"values"}, "available": True},
    "reachable": {"label": "Can be reached on a channel", "keys": {"channel"}, "available": True},
    # Opens and clicks come from the listmonk feedback sync (crm/listmonk_sync.py).
    "blast_engagement": {
        "label": "Opened or clicked a blast",
        "keys": {"blast_id", "action"},
        "available": True,
    },
}
EXCLUDED_REASONS = ("unsubscribed", "bounced", "banned", "no_consent", "not_registered")


class SegmentError(ValueError):
    """A definition or preview request that cannot be evaluated."""

    def __init__(self, code: str, pointer: str = "", reason: str = ""):
        self.code, self.pointer, self.reason = code, pointer, reason
        super().__init__(code)


def _invalid(pointer: str, reason: str) -> SegmentError:
    return SegmentError("invalid_definition", pointer or "/", reason)


def _pointer(*parts) -> str:
    return "".join("/" + str(part).replace("~", "~0").replace("/", "~1") for part in parts)


def validate_definition(definition: Any) -> dict:
    """Return the canonical definition, or raise ``SegmentError`` naming the bad part."""

    if not isinstance(definition, dict):
        raise _invalid("", "expected_object")
    for key in definition:
        if key not in ("match", "rules", "exclude"):
            raise _invalid(_pointer(key), "unknown_key")
    match = definition.get("match", "all")
    if match not in MATCH:
        raise _invalid(_pointer("match"), "expected_one_of")
    result = {"match": match}
    total = 0
    for section in ("rules", "exclude"):
        rules = definition.get(section, [])
        if not isinstance(rules, list):
            raise _invalid(_pointer(section), "expected_array")
        total += len(rules)
        if total > MAX_RULES:
            raise _invalid(_pointer(section), "too_many_rules")
        result[section] = [
            _validate_rule(rule, (section, index)) for index, rule in enumerate(rules)
        ]
    return result


def _validate_rule(rule: Any, at: tuple) -> dict:
    if not isinstance(rule, dict):
        raise _invalid(_pointer(*at), "expected_object")
    rule_type = rule.get("type")
    spec = RULE_TYPES.get(rule_type) if isinstance(rule_type, str) else None
    if spec is None:
        raise _invalid(_pointer(*at, "type"), "unknown_rule_type")
    for key in rule:
        if key != "type" and key not in spec["keys"]:
            raise _invalid(_pointer(*at, key), "unknown_key")
    return {"type": rule_type, **_RULE_VALIDATORS[rule_type](rule, at)}


def _event_registration(rule: dict, at: tuple) -> dict:
    event_ids = rule.get("event_ids") or []
    statuses = rule.get("statuses") or []
    checked_in = rule.get("checked_in")
    return {
        "event_ids": _list(event_ids, at + ("event_ids",), _uuid, allow_empty=True),
        "statuses": _list(
            statuses, at + ("statuses",), _choice(REGISTRATION_STATUSES), allow_empty=True
        ),
        "checked_in": _optional_bool(checked_in, at + ("checked_in",)),
    }


def _events_attended_count(rule: dict, at: tuple) -> dict:
    return {
        "op": _choice(COUNT_OPS)(rule.get("op"), at + ("op",)),
        "value": _integer(rule.get("value"), at + ("value",), 0, MAX_COUNT_VALUE),
    }


def _last_attended_within_days(rule: dict, at: tuple) -> dict:
    return {"value": _integer(rule.get("value"), at + ("value",), 1, MAX_DAYS)}


def _tag(rule: dict, at: tuple) -> dict:
    return {
        "op": _choice(TAG_OPS)(rule.get("op"), at + ("op",)),
        "values": _list(rule.get("values"), at + ("values",), _text(64)),
    }


def _field(rule: dict, at: tuple) -> dict:
    return {
        "field": _choice(FIELDS)(rule.get("field"), at + ("field",)),
        **_text_condition(rule, at),
    }


def _attribute(rule: dict, at: tuple) -> dict:
    key = rule.get("key")
    if not isinstance(key, str) or not ATTRIBUTE_KEY.match(key):
        raise _invalid(_pointer(*at, "key"), "invalid_attribute_key")
    return {"key": key, **_text_condition(rule, at)}


def _registration_answer(rule: dict, at: tuple) -> dict:
    return {
        "label": _text(MAX_LABEL)(rule.get("label"), at + ("label",)),
        **_text_condition(rule, at),
    }


def _chat_member(rule: dict, at: tuple) -> dict:
    value = rule.get("value")
    if not isinstance(value, bool):
        raise _invalid(_pointer(*at, "value"), "expected_boolean")
    return {"value": value}


def _source(rule: dict, at: tuple) -> dict:
    return {"values": _list(rule.get("values"), at + ("values",), _choice(Contact.Source.values))}


def _reachable(rule: dict, at: tuple) -> dict:
    return {"channel": _choice(Channel.values)(rule.get("channel"), at + ("channel",))}


def _blast_engagement(rule: dict, at: tuple) -> dict:
    return {
        "blast_id": _uuid(rule.get("blast_id"), at + ("blast_id",)),
        "action": _choice(ENGAGEMENT_ACTIONS)(rule.get("action"), at + ("action",)),
    }


_RULE_VALIDATORS = {
    "event_registration": _event_registration,
    "events_attended_count": _events_attended_count,
    "last_attended_within_days": _last_attended_within_days,
    "tag": _tag,
    "field": _field,
    "attribute": _attribute,
    "registration_answer": _registration_answer,
    "chat_member": _chat_member,
    "source": _source,
    "reachable": _reachable,
    "blast_engagement": _blast_engagement,
}


def _text_condition(rule: dict, at: tuple) -> dict:
    op = _choice(TEXT_OPS)(rule.get("op"), at + ("op",))
    if op in VALUE_OPS:
        return {"op": op, "value": _text(MAX_TEXT)(rule.get("value"), at + ("value",))}
    value = rule.get("value")
    if value is not None and not isinstance(value, str):
        raise _invalid(_pointer(*at, "value"), "expected_string")
    return {"op": op}


def _choice(allowed):
    def check(value, at):
        if not isinstance(value, str) or value not in allowed:
            raise _invalid(_pointer(*at), "expected_one_of")
        return value

    return check


def _text(limit: int):
    def check(value, at):
        if not isinstance(value, str):
            raise _invalid(_pointer(*at), "expected_string")
        text = " ".join(value.split())
        if not text:
            raise _invalid(_pointer(*at), "required")
        if len(text) > limit:
            raise _invalid(_pointer(*at), "too_long")
        return text

    return check


def _uuid(value, at) -> str:
    if isinstance(value, str):
        try:
            return str(uuid.UUID(value))
        except ValueError:
            pass
    raise _invalid(_pointer(*at), "expected_uuid")


def _integer(value, at, minimum: int, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise _invalid(_pointer(*at), "expected_integer")
    if not minimum <= value <= maximum:
        raise _invalid(_pointer(*at), "out_of_range")
    return value


def _optional_bool(value, at) -> Optional[bool]:
    if value is not None and not isinstance(value, bool):
        raise _invalid(_pointer(*at), "expected_boolean")
    return value


def _list(values, at, check, *, allow_empty: bool = False) -> list:
    if not isinstance(values, list):
        raise _invalid(_pointer(*at), "expected_array")
    if not values and not allow_empty:
        raise _invalid(_pointer(*at), "required")
    if len(values) > MAX_LIST_ITEMS:
        raise _invalid(_pointer(*at), "too_many_items")
    cleaned = []
    for index, value in enumerate(values):
        item = check(value, at + (index,))
        if item not in cleaned:
            cleaned.append(item)
    return cleaned


def definition_q(definition: dict, *, now: Optional[datetime] = None) -> Q:
    """The ``Contact`` filter for a validated definition, before any suppression."""

    now = now or timezone.now()
    rules = [_rule_q(rule, ("rules", index), now) for index, rule in enumerate(definition["rules"])]
    excludes = [
        _rule_q(rule, ("exclude", index), now) for index, rule in enumerate(definition["exclude"])
    ]
    combined = Q()
    if rules:
        combined = reduce(and_ if definition["match"] == "all" else or_, rules)
    if excludes:
        combined &= ~reduce(or_, excludes)
    return combined


def segment_contacts(definition: Any, *, now: Optional[datetime] = None):
    """Everyone a definition matches, ignoring channel and purpose rules."""

    return Contact.objects.filter(definition_q(validate_definition(definition), now=now))


def evaluate(
    definition: Any,
    purpose: Optional[str] = None,
    event: Optional[CrmEvent] = None,
    *,
    now: Optional[datetime] = None,
):
    """Return ``(queryset, excluded_counts)`` for an email audience.

    Suppression runs after the rules: banned, unsubscribed (or marked as spam)
    and bounced people are removed, then the purpose rule: ``marketing`` needs
    the consent rule and ``event_update`` a registration for ``event``. Each
    excluded person is counted once, under the first reason that applies.
    """

    now = now or timezone.now()
    if purpose not in (None, *PURPOSES):
        raise SegmentError("invalid_purpose")
    if purpose == "event_update" and event is None:
        raise SegmentError("event_required")
    matched = Contact.objects.filter(
        definition_q(validate_definition(definition), now=now)
    ).annotate(is_banned=banned())

    sendable = Q(is_banned=False, email_status=Contact.EmailStatus.ACTIVE)
    purpose_q = Q()
    reasons = {
        "banned": Q(is_banned=True),
        "unsubscribed": Q(
            is_banned=False,
            email_status__in=(Contact.EmailStatus.UNSUBSCRIBED, Contact.EmailStatus.COMPLAINED),
        ),
        "bounced": Q(is_banned=False, email_status=Contact.EmailStatus.BOUNCED),
    }
    if purpose == "marketing":
        purpose_q = marketing_consent_q(now)
        reasons["no_consent"] = sendable & ~purpose_q
    elif purpose == "event_update":
        # Annotated, like is_banned, so the wrapped aggregate below can read it
        # as a column instead of a correlated subquery.
        matched = matched.annotate(is_registered=Exists(_registrations(event)))
        purpose_q = Q(is_registered=True)
        reasons["not_registered"] = sendable & Q(is_registered=False)
    counted = matched.aggregate(
        **{reason: Count("pk", filter=condition) for reason, condition in reasons.items()}
    )
    excluded = {reason: counted.get(reason, 0) for reason in EXCLUDED_REASONS}
    return matched.filter(sendable & purpose_q), excluded


def _registrations(event: CrmEvent):
    return EventRegistration.objects.filter(registered_q(), event=event, contact=OuterRef("pk"))


def _rule_q(rule: dict, at: tuple, now: datetime) -> Q:
    rule_type = rule["type"]
    if rule_type == "event_registration":
        # Removed and declined guests are not registered, whatever the statuses say.
        registrations = EventRegistration.objects.filter(registered_q(), contact=OuterRef("pk"))
        if rule["event_ids"]:
            registrations = registrations.filter(event_id__in=rule["event_ids"])
        if rule["statuses"]:
            registrations = registrations.filter(
                approval_status__in=[REGISTRATION_STATUSES[status] for status in rule["statuses"]]
            )
        if rule["checked_in"] is not None:
            registrations = registrations.filter(checked_in_at__isnull=not rule["checked_in"])
        return Q(Exists(registrations))
    if rule_type == "events_attended_count":
        return Q(**{f"events_attended_count__{_COUNT_LOOKUPS[rule['op']]}": rule["value"]})
    if rule_type == "last_attended_within_days":
        return Q(last_attended_at__gte=now - timedelta(days=rule["value"]))
    if rule_type == "tag":
        has_any = reduce(or_, [Q(tags__contains=[tag]) for tag in rule["values"]])
        if rule["op"] == "has_all":
            return Q(tags__contains=rule["values"])
        return ~has_any if rule["op"] == "has_none" else has_any
    if rule_type == "field":
        return _field_q(rule["field"], rule["op"], rule.get("value"))
    if rule_type == "attribute":
        return _attribute_q(rule["key"], rule["op"], rule.get("value"))
    if rule_type == "registration_answer":
        return _answer_q(rule["label"], rule["op"], rule.get("value"))
    if rule_type == "chat_member":
        member = Q(
            Exists(
                CommunityChatDevice.objects.filter(
                    user_id=OuterRef("user_id"),
                    user__is_active=True,
                    status=DeviceBindingStatus.VERIFIED,
                    revoked_at__isnull=True,
                )
            )
        )
        return member if rule["value"] else ~member
    if rule_type == "source":
        return Q(source__in=rule["values"])
    if rule_type == "reachable":
        return REACHABLE[rule["channel"]]()
    if rule_type == "blast_engagement":
        deliveries = BlastDelivery.objects.filter(blast_id=rule["blast_id"], contact=OuterRef("pk"))
        if rule["action"] == "clicked":
            deliveries = deliveries.filter(first_clicked_at__isnull=False)
        else:
            # A click means the email was opened, even if its tracking pixel was blocked.
            deliveries = deliveries.filter(
                Q(first_opened_at__isnull=False) | Q(first_clicked_at__isnull=False)
            )
        return Q(Exists(deliveries))
    raise SegmentError("rule_not_available", _pointer(*at), rule_type)


def _field_q(name: str, op: str, value: Optional[str]) -> Q:
    if op == "contains":
        return Q(**{f"{name}__icontains": value})
    if op == "equals":
        return Q(**{f"{name}__iexact": value})
    blank = Q(**{name: ""})
    return ~blank if op == "is_set" else blank


def _attribute_q(key: str, op: str, value: Optional[str]) -> Q:
    text = KeyTextTransform(key, "attributes")
    if op == "contains":
        return Q(IContains(text, value))
    if op == "equals":
        return Q(IExact(text, value))
    has_value = Q(attributes__has_key=key) & Q(Regex(text, _NON_BLANK))
    return has_value if op == "is_set" else ~has_value


def _answer_q(label: str, op: str, value: Optional[str]) -> Q:
    answered = []
    for index in range(ANSWER_SCAN_LIMIT):
        same_label = Q(**{f"registration_answers__{index}__label": label})
        if op == "contains":
            condition = Q(**{f"registration_answers__{index}__value__icontains": value})
        elif op == "equals":
            condition = Q(**{f"registration_answers__{index}__value__iexact": value})
        else:
            condition = Q(**{f"registration_answers__{index}__value__regex": _NON_BLANK})
        answered.append(same_label & condition)
    registrations = EventRegistration.objects.filter(
        reduce(or_, answered), contact=OuterRef("pk"), removed_at__isnull=True
    )
    match = Q(Exists(registrations))
    return ~match if op == "is_not_set" else match


def quick_segment_definition(key: str, event: CrmEvent) -> dict:
    """The saved-segment form of an event's Going, Checked in, No-shows or Waitlist tab."""

    event_ids = [str(event.pk)]
    rules = {
        "going": {"statuses": ["going"], "checked_in": None},
        "checked_in": {"statuses": [], "checked_in": True},
        "no_show": {"statuses": ["going"], "checked_in": False},
        "waitlist": {"statuses": ["waitlist"], "checked_in": None},
    }[key]
    return {
        "match": "all",
        "rules": [{"type": "event_registration", "event_ids": event_ids, **rules}],
        "exclude": [],
    }


def catalog(*, tags: list, attributes: list, questions: list) -> dict:
    """What the segment builder's dropdowns offer."""

    return {
        "rule_types": [
            {"type": rule_type, "label": spec["label"], "available": spec["available"]}
            for rule_type, spec in RULE_TYPES.items()
        ],
        "match": list(MATCH),
        "registration_statuses": list(REGISTRATION_STATUSES),
        "count_ops": list(COUNT_OPS),
        "tag_ops": list(TAG_OPS),
        "text_ops": list(TEXT_OPS),
        "fields": [{"field": name, "label": label} for name, label in FIELDS.items()],
        "sources": [{"value": value, "label": label} for value, label in Contact.Source.choices],
        "channels": [{"value": value, "label": label} for value, label in Channel.choices],
        "engagement_actions": list(ENGAGEMENT_ACTIONS),
        "attributes": attributes,
        "registration_questions": questions,
        "tags": tags,
        "limits": {
            "rules": MAX_RULES,
            "list_items": MAX_LIST_ITEMS,
            "text": MAX_TEXT,
            "days": MAX_DAYS,
            "answers_scanned": ANSWER_SCAN_LIMIT,
        },
    }
