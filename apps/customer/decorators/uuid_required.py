# apps/customer/decorators/uuid_required.py

import uuid as uuid_lib
from functools import wraps

from django.http import HttpResponseBadRequest
from django.shortcuts import get_object_or_404


def uuid_required(url_kwarg="uuid", model=None, uuid_field="uuid",
                   object_kwarg_name=None, source="kwargs"):
    """
    Validates that a value is a well-formed UUID before the view runs,
    and optionally fetches a model instance by it.

    Two things this covers that Django's <uuid:...> URL path converter
    doesn't already handle on its own:

    1. UUIDs arriving somewhere other than the URL path — a query
       string or POST body (e.g. an AJAX endpoint reading
       request.GET.get('uuid')) never gets converter-level validation,
       so a malformed value would otherwise reach the view as a raw,
       unchecked string.
    2. Fetch-and-inject in one step, specifically for models whose
       `uuid` field is the intended external identifier (per your
       User.uuid field's own help_text — "for external API
       references") rather than exposing the internal, sequential pk
       in URLs at all.

    Validation only, no DB lookup:

        @uuid_required(url_kwarg="product_id")
        def some_view(request, product_id):
            # product_id is now a real uuid.UUID instance, guaranteed valid
            ...

    Validation + automatic fetch:

        @uuid_required(url_kwarg="user_uuid", model=User, object_kwarg_name="target_user")
        def public_profile_by_uuid(request, user_uuid, target_user):
            # `target_user` was already fetched via User.objects.get(uuid=user_uuid)
            ...

    `source` controls where the raw value is read from:
        "kwargs" (default) — from the URLconf capture group
        "GET"              — request.GET.get(url_kwarg)
        "POST"             — request.POST.get(url_kwarg)

    Failure modes:
        Missing value           -> 400
        Malformed UUID          -> 400
        Well-formed but no row  -> 404 (only when `model` is given)
    """

    def decorator(view_func):
        @wraps(view_func)
        def _wrapped_view(request, *args, **kwargs):
            if source == "kwargs":
                raw_value = kwargs.get(url_kwarg)
            elif source == "GET":
                raw_value = request.GET.get(url_kwarg)
            elif source == "POST":
                raw_value = request.POST.get(url_kwarg)
            else:
                raise ValueError(f"Unsupported source: {source!r}")

            if not raw_value:
                return HttpResponseBadRequest(f"Missing required UUID parameter: {url_kwarg}")

            try:
                parsed_uuid = uuid_lib.UUID(str(raw_value))
            except (ValueError, AttributeError, TypeError):
                return HttpResponseBadRequest(f"Invalid UUID: {raw_value!r}")

            # Normalize to a real uuid.UUID regardless of whether it
            # arrived pre-parsed (URL path converter) or as a raw
            # string (GET/POST), so the view always sees the same type.
            kwargs[url_kwarg] = parsed_uuid

            if model is not None:
                obj = get_object_or_404(model, **{uuid_field: parsed_uuid})
                kwargs[object_kwarg_name or model._meta.model_name] = obj

            return view_func(request, *args, **kwargs)

        return _wrapped_view

    return decorator