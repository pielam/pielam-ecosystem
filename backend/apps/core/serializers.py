"""
Serializer base classes.

The goal is that concrete serializers only declare *fields*; anything about
request context, timestamps, or owner stamping lives here once.
"""

from __future__ import annotations

from typing import Optional, Sequence

from rest_framework import serializers

__all__ = [
    "BaseModelSerializer",
    "TimestampedSerializerMixin",
    "ReadOnlyOwnerMixin",
]


class BaseModelSerializer(serializers.ModelSerializer):
    """
    Adds a couple of context helpers every serializer in the project needs.

    ``current_user`` returns ``None`` for anonymous requests rather than an
    ``AnonymousUser``, so ``if self.current_user:`` behaves as expected.
    """

    @property
    def request(self):
        return self.context.get("request")

    @property
    def current_user(self):
        request = self.request
        user = getattr(request, "user", None)
        if user is not None and getattr(user, "is_authenticated", False):
            return user
        return None

    def build_absolute_uri(self, value: Optional[str]) -> Optional[str]:
        """Turn a media path into a full URL when a request is available."""
        if not value:
            return None
        request = self.request
        return request.build_absolute_uri(value) if request else value


class TimestampedSerializerMixin(serializers.Serializer):
    """Marks the usual audit columns read-only in one place."""

    TIMESTAMP_FIELDS: Sequence[str] = ("created_at", "updated_at", "deleted_at")

    def get_fields(self):
        fields = super().get_fields()
        for name in self.TIMESTAMP_FIELDS:
            if name in fields:
                fields[name].read_only = True
        return fields


class ReadOnlyOwnerMixin(serializers.Serializer):
    """
    Force owner-ish columns read-only.

    Without this, ``fields = "__all__"`` on an owned model happily accepts a
    ``user`` id from the request body and lets a client create rows on behalf
    of somebody else.
    """

    OWNER_FIELDS: Sequence[str] = ("user", "dealer", "created_by", "owner", "recipient")

    def get_fields(self):
        fields = super().get_fields()
        for name in self.OWNER_FIELDS:
            if name in fields:
                fields[name].read_only = True
        return fields
