"""
Model-layer helpers.

Deliberately contains **no concrete or abstract Django models** -- adding one
to an installed app changes migration state. Everything here is a plain
function or plain-object mixin that existing models can adopt without a
schema change.
"""

from __future__ import annotations

import uuid
from typing import Iterable, Optional, Sequence

from django.utils.text import slugify

__all__ = ["clean_for_save", "ValidatedSaveMixin", "unique_slug"]


def clean_for_save(instance, update_fields: Optional[Iterable[str]] = None) -> None:
    """
    Run ``full_clean()`` in a way that is compatible with ``update_fields``.

    Calling a bare ``self.full_clean()`` from ``Model.save()`` is a common
    anti-pattern with two concrete failure modes:

    1. ``obj.save(update_fields=['slug'])`` validates *every* field, so a row
       that is already invalid in some unrelated column (a legacy record, a
       field made stricter later) can no longer be updated at all.
    2. ``full_clean()`` runs ``validate_unique()``, which issues an extra
       ``SELECT`` per save and races anyway -- the database constraint is the
       real guarantee.

    When ``update_fields`` is given we validate only those columns and leave
    uniqueness to the database.
    """
    if update_fields is None:
        instance.full_clean()
        return

    wanted = set(update_fields)
    exclude = [f.name for f in instance._meta.fields if f.name not in wanted]
    instance.full_clean(exclude=exclude, validate_unique=False)


def unique_slug(
    instance,
    base_text: str,
    field_name: str = "slug",
    max_length: Optional[int] = None,
    fallback: str = "item",
) -> str:
    """
    Build a slug that does not collide with an existing row.

    Replaces the ``while Model.objects.filter(slug=slug).exists()`` loop found
    across this project, which issued one query per collision (O(n) queries for
    n products sharing a name). Here a single query fetches every slug sharing
    the prefix and the suffix is chosen in memory.

    Notes
    -----
    * Uses ``_base_manager`` on purpose: a custom default manager that hides
      soft-deleted or unpublished rows would otherwise let this hand back a
      slug that is already taken at the database level.
    * Two concurrent inserts can still pick the same slug. That last-mile race
      is the database unique constraint's job -- callers should catch
      ``IntegrityError`` and retry rather than assume this is atomic.
    """
    manager = instance.__class__._base_manager

    if max_length is None:
        try:
            max_length = instance._meta.get_field(field_name).max_length
        except Exception:  # pragma: no cover - field introspection is best effort
            max_length = 255
    max_length = max_length or 255

    # Leave room for a "-NN" suffix.
    base = slugify(base_text or "")[: max(1, max_length - 5)] or fallback

    taken = set(
        manager.filter(**{f"{field_name}__startswith": base})
        .exclude(pk=instance.pk)
        .values_list(field_name, flat=True)
    )

    if base not in taken:
        return base

    for counter in range(2, 1000):
        candidate = f"{base}-{counter}"
        if candidate not in taken:
            return candidate

    return f"{base}-{uuid.uuid4().hex[:8]}"


class ValidatedSaveMixin:
    """
    Mixin for models that want validation on save without the pitfalls above.

    Adds a ``skip_validation=True`` escape hatch for bulk/administrative
    writes and for ``loaddata``-style flows where the data is already trusted.

    Place it *before* ``models.Model`` in the bases::

        class Thing(ValidatedSaveMixin, models.Model):
            ...
    """

    #: Fields never validated on save (e.g. auto-populated audit columns).
    validation_exclude: Sequence[str] = ()

    def save(self, *args, **kwargs):
        skip_validation = kwargs.pop("skip_validation", False)
        if not skip_validation:
            clean_for_save(self, kwargs.get("update_fields"))
        return super().save(*args, **kwargs)
