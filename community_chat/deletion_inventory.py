"""Account erasure inventory for an authorised operator's existing deletion case."""

from django.apps import apps
from django.contrib.auth import get_user_model
from django.db import models
from django.db.models import Subquery


def related_record_counts(user_id):
    """Report populated direct user references without exporting row contents."""
    user_model = get_user_model()
    counts = {}
    for model in sorted(apps.get_models(include_auto_created=True), key=lambda row: row._meta.label_lower):
        if model._meta.proxy or not model._meta.managed:
            continue
        for field in model._meta.local_fields:
            if (
                not isinstance(field, models.ForeignKey)
                or field.related_model is not user_model
            ):
                continue
            value = user_id
            if not field.target_field.primary_key:
                value = Subquery(
                    user_model._base_manager.filter(pk=user_id).values(field.target_field.attname)
                )
            count = model._base_manager.filter(**{field.attname: value}).count()
            if count:
                counts[f"{model._meta.label_lower}.{field.name}"] = count
    return counts
