"""
Reusable object-level permissions.

Design notes
------------
The project's recurring authorisation bug is checking a *role* ("is this user
a dealer?") and then fetching the object by slug with no ownership filter --
which lets any dealer edit any other dealer's records. These classes separate
the two questions:

* ``IsDealer`` / ``IsStaff``    -> "may this user act at all?"  (role)
* ``IsOwnerOrReadOnly``         -> "may this user act on *this* row?" (object)

Both must be listed for a write endpoint. Ownership is resolved through a
declarative ``owner_fields`` attribute on the view, so a single implementation
covers ``dealer``, ``created_by``, ``user``, ``recipient`` and friends
(Open/Closed: extend by declaring a field name, not by subclassing).
"""

from __future__ import annotations

from typing import Any, Iterable, Optional, Sequence

from rest_framework.permissions import SAFE_METHODS, BasePermission

__all__ = [
    "ReadOnly",
    "IsAuthenticatedAndActive",
    "IsSelf",
    "IsOwner",
    "IsOwnerOrReadOnly",
    "IsOwnerOrStaff",
    "IsDealer",
    "IsDealerOrReadOnly",
    "IsStaffOrReadOnly",
    "resolve_owners",
]


# --------------------------------------------------------------------------
# Ownership resolution
# --------------------------------------------------------------------------

#: Field names tried when a view does not declare ``owner_fields``.
DEFAULT_OWNER_FIELDS: Sequence[str] = ("user", "dealer", "created_by", "recipient", "owner")


def _owner_fields_for(view) -> Sequence[str]:
    return getattr(view, "owner_fields", None) or DEFAULT_OWNER_FIELDS


def resolve_owners(obj: Any, owner_fields: Iterable[str]) -> list:
    """
    Collect the user objects that own ``obj``.

    Supports dotted paths (``profile.user``) so a nested owner can be reached
    without writing a bespoke permission class. Missing attributes are skipped
    rather than raising -- a model simply may not have that relation.
    """
    owners = []
    for path in owner_fields:
        current = obj
        for part in path.split("."):
            current = getattr(current, part, None)
            if current is None:
                break
        if current is not None:
            owners.append(current)
    return owners


def _is_owner(user, obj, view) -> bool:
    if not user or not user.is_authenticated:
        return False
    return any(owner == user for owner in resolve_owners(obj, _owner_fields_for(view)))


def _is_privileged(user) -> bool:
    """Staff/superusers and the platform ``admin`` role bypass ownership."""
    if not user or not user.is_authenticated:
        return False
    return bool(
        getattr(user, "is_staff", False)
        or getattr(user, "is_superuser", False)
        or getattr(user, "role", None) == "admin"
    )


# --------------------------------------------------------------------------
# Role-level permissions
# --------------------------------------------------------------------------

class ReadOnly(BasePermission):
    """Allows GET/HEAD/OPTIONS only. Compose with ``|`` for public reads."""

    def has_permission(self, request, view) -> bool:
        return request.method in SAFE_METHODS


class IsAuthenticatedAndActive(BasePermission):
    """
    Authenticated *and* usable: not soft-deleted, not locked, not suspended.

    ``IsAuthenticated`` alone lets a suspended account keep using a still-valid
    JWT until it expires.
    """

    message = "This account is not permitted to perform this action."

    def has_permission(self, request, view) -> bool:
        user = request.user
        if not (user and user.is_authenticated and user.is_active):
            return False
        if getattr(user, "deleted_at", None) is not None:
            return False
        if getattr(user, "is_locked", False):
            return False
        if getattr(user, "account_status", "active") in {"suspended", "banned"}:
            return False
        return True


class IsDealer(BasePermission):
    """Role gate for catalogue-management endpoints."""

    message = "Only dealer accounts may perform this action."

    def has_permission(self, request, view) -> bool:
        user = request.user
        if not (user and user.is_authenticated):
            return False
        return bool(getattr(user, "is_dealer", False)) or _is_privileged(user)


class IsDealerOrReadOnly(IsDealer):
    """Anyone may read; only dealers (or staff) may write."""

    def has_permission(self, request, view) -> bool:
        if request.method in SAFE_METHODS:
            return True
        return super().has_permission(request, view)


class IsStaffOrReadOnly(BasePermission):
    message = "Only staff may modify this resource."

    def has_permission(self, request, view) -> bool:
        if request.method in SAFE_METHODS:
            return True
        return _is_privileged(request.user)


# --------------------------------------------------------------------------
# Object-level permissions
# --------------------------------------------------------------------------

class IsOwner(BasePermission):
    """Object must belong to the requesting user (staff bypass)."""

    message = "You do not have access to this object."

    def has_object_permission(self, request, view, obj) -> bool:
        return _is_privileged(request.user) or _is_owner(request.user, obj, view)


class IsOwnerOrReadOnly(IsOwner):
    """Reads open to anyone the queryset exposed; writes restricted to owners."""

    def has_object_permission(self, request, view, obj) -> bool:
        if request.method in SAFE_METHODS:
            return True
        return super().has_object_permission(request, view, obj)


class IsOwnerOrStaff(IsOwner):
    """Alias kept for readability at call sites; staff bypass is built in."""


class IsSelf(BasePermission):
    """For endpoints acting on a ``User`` row directly (``/users/{id}/``)."""

    message = "You may only act on your own account."

    def has_object_permission(self, request, view, obj) -> bool:
        if _is_privileged(request.user):
            return True
        return obj == request.user
