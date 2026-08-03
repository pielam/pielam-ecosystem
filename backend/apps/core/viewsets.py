"""
Base viewsets that pre-compose the core mixins.

Concrete APIs subclass one of these and declare data (queryset, serializers,
filters, owner fields) rather than overriding behaviour.
"""

from __future__ import annotations

from rest_framework import viewsets
from rest_framework.filters import OrderingFilter, SearchFilter

from apps.core.mixins import (
    AutoOwnerMixin,
    DeclarativeFilterMixin,
    MultiSerializerMixin,
    OwnedQuerySetMixin,
    SelectRelatedMixin,
    SoftDeleteMixin,
)
from apps.core.pagination import StandardPagination

__all__ = [
    "BaseReadOnlyViewSet",
    "BaseModelViewSet",
    "OwnedModelViewSet",
]


class _CommonConfig:
    """Shared defaults so every endpoint paginates, searches and sorts alike."""

    pagination_class = StandardPagination
    filter_backends = [SearchFilter, OrderingFilter]
    search_fields: list = []
    ordering_fields: list = []
    ordering: list = []


class BaseReadOnlyViewSet(
    MultiSerializerMixin,
    SelectRelatedMixin,
    DeclarativeFilterMixin,
    _CommonConfig,
    viewsets.ReadOnlyModelViewSet,
):
    """List + retrieve only. Use for public catalogue reads."""


class BaseModelViewSet(
    MultiSerializerMixin,
    SelectRelatedMixin,
    DeclarativeFilterMixin,
    SoftDeleteMixin,
    _CommonConfig,
    viewsets.ModelViewSet,
):
    """
    Full CRUD with soft delete.

    Ownership is *not* enforced here -- add ``OwnedModelViewSet`` or an
    explicit ``IsOwner`` permission. Keeping them separate means a public
    catalogue viewset does not silently inherit an owner filter.
    """


class OwnedModelViewSet(
    OwnedQuerySetMixin,
    AutoOwnerMixin,
    BaseModelViewSet,
):
    """
    CRUD over rows belonging to ``request.user``.

    Non-owned rows are invisible (404, not 403) and the owner column is
    stamped server-side on create.
    """
