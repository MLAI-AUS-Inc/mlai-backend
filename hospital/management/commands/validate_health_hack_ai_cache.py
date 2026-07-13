"""Fail deployment unless Health Hack guards use a working shared Redis cache."""

from __future__ import annotations

import secrets

from django.conf import settings
from django.core.cache import cache
from django.core.management.base import BaseCommand, CommandError


REDIS_BACKEND = "django.core.cache.backends.redis.RedisCache"


class Command(BaseCommand):
    help = "Validate that the default cache is shared Redis and supports atomic counters."

    def handle(self, *args, **options):
        config = (getattr(settings, "CACHES", {}) or {}).get("default", {})
        if config.get("BACKEND") != REDIS_BACKEND:
            raise CommandError(
                "default cache must use django.core.cache.backends.redis.RedisCache"
            )
        if not str(config.get("LOCATION") or "").startswith(("redis://", "rediss://")):
            raise CommandError("default Redis cache location is missing or invalid")

        key = f"health-hack:deployment-cache-check:{secrets.token_urlsafe(12)}"
        try:
            if not cache.add(key, 1, timeout=30):
                raise CommandError("shared cache add operation unexpectedly failed")
            if int(cache.incr(key)) != 2:
                raise CommandError("shared cache atomic increment returned an invalid value")
            if int(cache.get(key)) != 2:
                raise CommandError("shared cache read-after-write returned an invalid value")
        except CommandError:
            raise
        except Exception as exc:
            raise CommandError("shared Redis cache is unavailable") from exc
        finally:
            try:
                cache.delete(key)
            except Exception:
                pass

        self.stdout.write(self.style.SUCCESS("Health Hack shared Redis cache is ready."))
