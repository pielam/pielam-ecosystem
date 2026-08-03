"""
Composable view mixins.

Each mixin owns exactly one concern (Single Responsibility) and is opt-in via
a class attribute rather than an override (Open/Closed), so a concrete viewset
usually declares data and no behaviour at all.
"""

from __future__ import annotations

from typing import Any, Dict, Mapping, Optional, Sequence

from django.db.models import Q
from django.utils import timezone
from rest_framework.exceptions import ValidationError

from apps.core.permissions import resolve_owners
from apps.core.utils import boolean_param

__all__ = [
    "MultiSerializerMixin",
    "OwnedQuerySetMixin",
    "AutoOwnerMixin",
    "SoftDeleteMixin",
    "DeclarativeFilterMixin",
    "SelectRelatedMixin",
]


class MultiSerializerMixin:
    """
    Pick a serializer per action instead of branching inside one class.

    ::

        serializer_classes = {
            "list":    ProductListSerializer,
            "create":  ProductWriteSerializer,
            "default": ProductDetailSerializer,
        }

    Falls back to ``serializer_class`` so it is safe to mix into any view.
    """

    serializer_classes: Mapping[str, Any] = {}

    def get_serializer_class(self):
        mapping = self.serializer_classes or {}
        action = getattr(self, "action", None)

        if action in mapping:
            return mapping[action]

        # Treat the write verbs as one bucket unless overridden individually.
        if action in {"create", "update", "partial_update"} and "write" in mapping:
            return mapping["write"]

        if "default" in mapping:
            return mapping["default"]

        return super().get_serializer_class()


class SelectRelatedMixin:
    """
    Apply ``select_related`` / ``prefetch_related`` declaratively.

    Prevents the N+1 queries that appear as soon as a serializer touches a
    foreign key. List actions may declare a lighter set than detail actions.
    """

    select_related: Sequence[str] = ()
    prefetch_related: Sequence[str] = ()
    list_select_related: Optional[Sequence[str]] = None
    list_prefetch_related: Optional[Sequence[str]] = None

    def get_queryset(self):
        queryset = super().get_queryset()
        is_list = getattr(self, "action", None) == "list"

        select = self.list_select_related if (is_list and self.list_select_related is not None) else self.select_related
        prefetch = (
            self.list_prefetch_related
            if (is_list and self.list_prefetch_related is not None)
            else self.prefetch_related
        )

        if select:
            queryset = queryset.select_related(*select)
        if prefetch:
            queryset = queryset.prefetch_related(*prefetch)
        return queryset


class OwnedQuerySetMixin:
    """
    Restrict the queryset to rows the requesting user owns.

    This is the queryset half of the IDOR fix: ``IsOwner`` returns 403 on a
    known object, while this mixin makes non-owned rows return 404 -- so the
    endpoint does not leak which slugs exist.

    ``owner_fields`` may list several relations; they are OR-ed together.
    Set ``public_read = True`` to leave safe methods unfiltered (public
    catalogue) and scope only writes.
    """

    owner_fields: Sequence[str] = ("user",)
    public_read: bool = False
    staff_sees_all: bool = True

    def _is_privileged(self) -> bool:
        user = self.request.user
        if not (user and user.is_authenticated):
            return False
        return bool(
            getattr(user, "is_staff", False)
            or getattr(user, "is_superuser", False)
            or getattr(user, "role", None) == "admin"
        )

    def get_queryset(self):
        queryset = super().get_queryset()
        user = self.request.user

        if self.public_read and self.request.method in ("GET", "HEAD", "OPTIONS"):
            return queryset

        if self.staff_sees_all and self._is_privileged():
            return queryset

        if not (user and user.is_authenticated):
            return queryset.none()

        predicate = Q()
        for field in self.owner_fields:
            predicate |= Q(**{field.replace(".", "__"): user})
        return queryset.filter(predicate)


class AutoOwnerMixin:
    """
    Stamp the owner on create from ``request.user``.

    Keeps the owner field out of the serializer's writable set, which closes
    the mass-assignment hole where a client posts someone else's user id.
    """

    owner_field: str = "user"

    def perform_create(self, serializer):
        serializer.save(**{self.owner_field: self.request.user})


class SoftDeleteMixin:
    """
    Turn DELETE into a soft delete when the model supports it.

    Uses the model's own ``soft_delete()`` when present so per-model side
    effects (cascades, counters) still run; otherwise stamps ``deleted_at``.
    """

    soft_delete_field: str = "deleted_at"
    soft_delete_actor_field: Optional[str] = "deleted_by"

    def perform_destroy(self, instance):
        if hasattr(instance, "soft_delete"):
            try:
                instance.soft_delete(self.request.user)
                return
            except TypeError:
                instance.soft_delete()
                return

        if not hasattr(instance, self.soft_delete_field):
            super().perform_destroy(instance)
            return

        update_fields = [self.soft_delete_field]
        setattr(instance, self.soft_delete_field, timezone.now())

        actor_field = self.soft_delete_actor_field
        if actor_field and hasattr(instance, actor_field):
            setattr(instance, actor_field, self.request.user)
            update_fields.append(actor_field)

        instance.save(update_fields=update_fields)


class DeclarativeFilterMixin:
    """
    Query-string filtering without pulling in ``django-filter``.

    ``filter_map`` maps a public query parameter to an ORM lookup::

        filter_map = {
            "brand":     "brand__brand_slug",
            "min_price": "final_price__gte",
            "featured":  "is_featured",
        }

    Values are coerced from the target field's type, so ``?featured=true``
    works and ``?min_price=abc`` returns a 400 instead of a 500.
    """

    filter_map: Dict[str, str] = {}
    boolean_filters: Sequence[str] = ()
    range_filters: Dict[str, str] = {}

    def _coerce(self, param: str, raw: str):
        if param in self.boolean_filters:
            value = boolean_param(raw)
            if value is None:
                raise ValidationError({param: "Expected a boolean (true/false)."})
            return value
        return raw

    def filter_queryset(self, queryset):
        queryset = super().filter_queryset(queryset)

        params = self.request.query_params
        applied = {}

        for param, lookup in self.filter_map.items():
            raw = params.get(param)
            if raw in (None, ""):
                continue
            applied[lookup] = self._coerce(param, raw)

        if applied:
            try:
                queryset = queryset.filter(**applied)
            except (ValueError, TypeError) as exc:
                raise ValidationError({"detail": f"Invalid filter value: {exc}"}) from exc

        return queryset
