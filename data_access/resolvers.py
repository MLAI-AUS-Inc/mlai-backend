from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Iterable
from uuid import UUID

from django.db.models import Count, Q, QuerySet
from django.utils.dateparse import parse_date, parse_datetime


class DataAccessError(ValueError):
    pass


class DataAccessPermissionDenied(PermissionError):
    pass


@dataclass(frozen=True)
class Actor:
    slack_id: str
    user: Any = None
    roles: frozenset[str] = field(default_factory=frozenset)
    organization_ids: frozenset[int] = field(default_factory=frozenset)
    organization_domains: frozenset[str] = field(default_factory=frozenset)
    points_portfolio: str = ""

    @property
    def user_id(self) -> int | None:
        return getattr(self.user, "id", None)

    def has_any_role(self, roles: Iterable[str]) -> bool:
        return bool(self.roles.intersection(set(roles)))


@dataclass(frozen=True)
class FieldSpec:
    name: str
    source: str | None = None
    searchable: bool = False
    filterable: bool = True
    orderable: bool = True
    groupable: bool = True

    @property
    def orm_source(self) -> str:
        return self.source or self.name


@dataclass(frozen=True)
class Policy:
    roles: tuple[str, ...]
    scope: str = "all"
    field: str = ""
    operations: tuple[str, ...] = ("list", "count", "aggregate")

    def allows(self, actor: Actor, operation: str) -> bool:
        return operation in self.operations and actor.has_any_role(self.roles)

    def as_q(self, actor: Actor) -> Q | None:
        if self.scope == "all":
            return Q()
        if self.scope == "self_user":
            return Q(**{self.field: actor.user_id}) if actor.user_id else None
        if self.scope == "self_slack":
            return Q(**{self.field: actor.slack_id}) if actor.slack_id else None
        if self.scope == "founder_org":
            return Q(**{f"{self.field}__in": list(actor.organization_ids)}) if actor.organization_ids else None
        if self.scope == "founder_domain":
            domains = [domain for domain in actor.organization_domains if domain]
            return Q(**{f"{self.field}__in": domains}) if domains else None
        if self.scope == "portfolio":
            return Q(**{self.field: actor.points_portfolio}) if actor.points_portfolio else None
        return None


class Resource:
    def __init__(
        self,
        *,
        key: str,
        description: str,
        resolver: "BaseResolver",
        fields: Iterable[FieldSpec],
        policies: Iterable[Policy],
        default_limit: int = 100,
        max_limit: int = 500,
        operations: Iterable[str] = ("list", "count", "aggregate"),
    ):
        self.key = key
        self.description = description
        self.resolver = resolver
        self.fields = {field.name: field for field in fields}
        self.policies = tuple(policies)
        self.default_limit = default_limit
        self.max_limit = max_limit
        self.operations = tuple(operations)

    @property
    def filter_fields(self) -> set[str]:
        return {name for name, spec in self.fields.items() if spec.filterable}

    @property
    def order_fields(self) -> set[str]:
        return {name for name, spec in self.fields.items() if spec.orderable}

    @property
    def group_fields(self) -> set[str]:
        return {name for name, spec in self.fields.items() if spec.groupable}

    @property
    def searchable_fields(self) -> set[str]:
        return {name for name, spec in self.fields.items() if spec.searchable}

    def accessible_operations(self, actor: Actor) -> tuple[str, ...]:
        return tuple(
            operation
            for operation in self.operations
            if any(
                policy.allows(actor, operation) and policy.as_q(actor) is not None
                for policy in self.policies
            )
        )

    def catalog_entry(self, *, operations: Iterable[str] | None = None) -> dict[str, Any]:
        visible_operations = tuple(self.operations if operations is None else operations)
        return {
            "key": self.key,
            "description": self.description,
            "operations": list(visible_operations),
            "fields": sorted(self.fields),
            "filters": sorted(self.filter_fields),
            "order_by": sorted(self.order_fields),
            "group_by": sorted(self.group_fields),
            "searchable_fields": sorted(self.searchable_fields),
            "default_limit": self.default_limit,
            "max_limit": self.max_limit,
        }

    def execute(self, actor: Actor, query: dict[str, Any]) -> dict[str, Any]:
        operation = query.get("operation") or "list"
        if operation not in self.operations:
            raise DataAccessError(f"Resource `{self.key}` does not support `{operation}`.")
        return self.resolver.execute(self, actor, query)


class BaseResolver:
    def execute(self, resource: Resource, actor: Actor, query: dict[str, Any]) -> dict[str, Any]:
        raise NotImplementedError


class ModelResolver(BaseResolver):
    OPERATOR_LOOKUPS = {
        "eq": "exact",
        "gt": "gt",
        "gte": "gte",
        "lt": "lt",
        "lte": "lte",
        "in": "in",
        "icontains": "icontains",
    }

    def __init__(self, model: Any, *, default_order_by: Iterable[str] = ()):
        self.model = model
        self.default_order_by = tuple(default_order_by)

    def execute(self, resource: Resource, actor: Actor, query: dict[str, Any]) -> dict[str, Any]:
        qs = self._scoped_queryset(resource, actor, query.get("operation") or "list")
        qs = self._apply_filters(resource, qs, query.get("filters") or [])

        operation = query.get("operation") or "list"
        if operation == "count":
            rows = [{"count": qs.count()}]
            return self._response(resource, rows, limit=1, offset=0, has_more=False)
        if operation == "aggregate":
            return self._aggregate(resource, qs, query)
        return self._list(resource, qs, query)

    def _scoped_queryset(self, resource: Resource, actor: Actor, operation: str) -> QuerySet:
        qs = self.model.objects.all()
        unrestricted = False
        combined_q = Q()
        matched = False

        for policy in resource.policies:
            if not policy.allows(actor, operation):
                continue
            policy_q = policy.as_q(actor)
            if policy_q is None:
                continue
            matched = True
            if policy_q == Q():
                unrestricted = True
                break
            combined_q |= policy_q

        if not matched:
            raise DataAccessPermissionDenied("You do not have access to this resource.")
        if unrestricted:
            return qs
        return qs.filter(combined_q)

    def _apply_filters(self, resource: Resource, qs: QuerySet, filters: list[dict[str, Any]]) -> QuerySet:
        for filter_spec in filters:
            field_name = filter_spec["field"]
            operator = filter_spec["operator"]
            value = filter_spec.get("value")
            field_spec = self._field(resource, field_name)
            if field_name not in resource.filter_fields:
                raise DataAccessError(f"Field `{field_name}` cannot be filtered.")
            if operator == "icontains" and field_name not in resource.searchable_fields:
                raise DataAccessError(f"Field `{field_name}` does not allow `icontains`.")
            lookup = self.OPERATOR_LOOKUPS.get(operator)
            if lookup is None and operator != "neq":
                raise DataAccessError(f"Unsupported operator `{operator}`.")
            orm_lookup = field_spec.orm_source
            if operator == "neq":
                qs = qs.exclude(**{orm_lookup: value})
                continue
            if lookup != "exact":
                orm_lookup = f"{orm_lookup}__{lookup}"
            qs = qs.filter(**{orm_lookup: self._coerce_value(value)})
        return qs

    def _aggregate(self, resource: Resource, qs: QuerySet, query: dict[str, Any]) -> dict[str, Any]:
        group_by = query.get("group_by") or []
        for field_name in group_by:
            if field_name not in resource.group_fields:
                raise DataAccessError(f"Field `{field_name}` cannot be grouped.")

        if not group_by:
            rows = [{"count": qs.count()}]
            return self._response(resource, rows, limit=1, offset=0, has_more=False)

        orm_group_by = [self._field(resource, field_name).orm_source for field_name in group_by]
        qs = qs.values(*orm_group_by).annotate(count=Count("pk"))
        qs = self._apply_ordering(resource, qs, query.get("order_by") or [])
        return self._page_values(resource, qs, query, output_fields=group_by + ["count"], source_fields=orm_group_by + ["count"])

    def _list(self, resource: Resource, qs: QuerySet, query: dict[str, Any]) -> dict[str, Any]:
        fields = query.get("fields") or sorted(resource.fields)
        for field_name in fields:
            self._field(resource, field_name)
        qs = self._apply_ordering(resource, qs, query.get("order_by") or [])
        limit, offset = self._limit_offset(resource, query)
        objects = list(qs[offset: offset + limit + 1])
        has_more = len(objects) > limit
        objects = objects[:limit]
        rows = [self._serialize_object(resource, obj, fields) for obj in objects]
        return self._response(resource, rows, limit=limit, offset=offset, has_more=has_more)

    def _page_values(
        self,
        resource: Resource,
        qs: QuerySet,
        query: dict[str, Any],
        *,
        output_fields: list[str],
        source_fields: list[str],
    ) -> dict[str, Any]:
        limit, offset = self._limit_offset(resource, query)
        values = list(qs[offset: offset + limit + 1])
        has_more = len(values) > limit
        values = values[:limit]
        rows = []
        for item in values:
            row = {}
            for output_name, source_name in zip(output_fields, source_fields):
                row[output_name] = self._json_safe(item.get(source_name))
            rows.append(row)
        return self._response(resource, rows, limit=limit, offset=offset, has_more=has_more)

    def _apply_ordering(self, resource: Resource, qs: QuerySet, order_by: list[dict[str, Any]]) -> QuerySet:
        ordering = []
        for order_spec in order_by:
            field_name = order_spec["field"]
            if field_name not in resource.order_fields:
                raise DataAccessError(f"Field `{field_name}` cannot be ordered.")
            source = self._field(resource, field_name).orm_source
            if order_spec.get("direction") == "desc":
                source = f"-{source}"
            ordering.append(source)
        if not ordering:
            ordering = list(self.default_order_by)
        return qs.order_by(*ordering) if ordering else qs

    def _limit_offset(self, resource: Resource, query: dict[str, Any]) -> tuple[int, int]:
        limit = int(query.get("limit") or resource.default_limit)
        offset = int(query.get("offset") or 0)
        if limit > resource.max_limit:
            raise DataAccessError(f"Limit {limit} exceeds max_limit {resource.max_limit} for `{resource.key}`.")
        return limit, offset

    def _field(self, resource: Resource, name: str) -> FieldSpec:
        try:
            return resource.fields[name]
        except KeyError as exc:
            raise DataAccessError(f"Unknown field `{name}` for `{resource.key}`.") from exc

    def _serialize_object(self, resource: Resource, obj: Any, fields: list[str]) -> dict[str, Any]:
        row = {}
        for field_name in fields:
            source = self._field(resource, field_name).orm_source
            row[field_name] = self._json_safe(_resolve_attr(obj, source))
        return row

    def _response(self, resource: Resource, rows: list[dict[str, Any]], *, limit: int, offset: int, has_more: bool) -> dict[str, Any]:
        return {
            "resource": resource.key,
            "rows": rows,
            "returned_count": len(rows),
            "limit": limit,
            "offset": offset,
            "has_more": has_more,
        }

    def _coerce_value(self, value: Any) -> Any:
        if isinstance(value, list):
            return [self._coerce_value(item) for item in value]
        if isinstance(value, str):
            return parse_datetime(value) or parse_date(value) or value
        return value

    def _json_safe(self, value: Any) -> Any:
        if isinstance(value, (UUID, Decimal)):
            return str(value)
        return value


class ServiceResolver(BaseResolver):
    def __init__(self, handler):
        self.handler = handler

    def execute(self, resource: Resource, actor: Actor, query: dict[str, Any]) -> dict[str, Any]:
        return self.handler(resource=resource, actor=actor, query=query)


def _resolve_attr(obj: Any, source: str) -> Any:
    current = obj
    for part in source.split("__"):
        if current is None:
            return None
        current = getattr(current, part)
    return current
