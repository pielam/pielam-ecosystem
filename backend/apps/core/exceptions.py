"""
Project-wide exception handling for the API.

The handler is *additive*: it keeps DRF's original body intact (existing
front-end JavaScript reads ``detail`` and field-keyed error lists) and adds a
predictable envelope alongside it. Nothing that worked before stops working.
"""

from __future__ import annotations

import logging

from django.core.exceptions import PermissionDenied as DjangoPermissionDenied
from django.core.exceptions import ValidationError as DjangoValidationError
from django.db import IntegrityError
from django.http import Http404
from rest_framework import status
from rest_framework.exceptions import APIException, PermissionDenied, ValidationError
from rest_framework.response import Response
from rest_framework.views import exception_handler as drf_exception_handler

__all__ = ["custom_exception_handler", "ServiceUnavailable", "Conflict"]

logger = logging.getLogger(__name__)


class ServiceUnavailable(APIException):
    status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    default_detail = "The upstream service is temporarily unavailable."
    default_code = "service_unavailable"


class Conflict(APIException):
    status_code = status.HTTP_409_CONFLICT
    default_detail = "This action conflicts with the current state of the resource."
    default_code = "conflict"


def _translate(exc):
    """Map non-DRF exceptions onto DRF ones so they render as JSON, not a 500."""
    if isinstance(exc, DjangoValidationError):
        detail = getattr(exc, "message_dict", None) or list(
            getattr(exc, "messages", [str(exc)])
        )
        return ValidationError(detail)

    if isinstance(exc, DjangoPermissionDenied):
        return PermissionDenied(str(exc) or None)

    if isinstance(exc, IntegrityError):
        # Surfacing the raw database message would leak schema details.
        return Conflict()

    return exc


def custom_exception_handler(exc, context):
    """
    Normalise error payloads.

    Output shape::

        {
          "detail": "...",            # preserved from DRF where present
          "errors": {...} | [...],    # the original body, always present
          "status_code": 400
        }
    """
    exc = _translate(exc)

    response = drf_exception_handler(exc, context)

    if response is None:
        # Genuinely unexpected -- log with the view for context and let Django's
        # own 500 handling take over so DEBUG tracebacks still work locally.
        view = context.get("view")
        logger.exception("Unhandled exception in %s", view.__class__.__name__ if view else "view")
        return None

    payload = response.data

    if isinstance(payload, dict):
        normalised = dict(payload)
        normalised.setdefault("detail", _first_message(payload))
        normalised["errors"] = payload
    elif isinstance(payload, list):
        normalised = {"detail": _first_message(payload), "errors": payload}
    else:
        normalised = {"detail": str(payload), "errors": payload}

    normalised["status_code"] = response.status_code

    if isinstance(exc, Http404):
        normalised.setdefault("detail", "Not found.")

    response.data = normalised
    return response


def _first_message(payload) -> str:
    """Best-effort single-sentence summary for clients that show one message."""
    if isinstance(payload, dict):
        if "detail" in payload:
            return str(payload["detail"])
        for key, value in payload.items():
            message = _first_message(value)
            if message:
                return f"{key}: {message}" if key != "non_field_errors" else message
        return "Request failed."
    if isinstance(payload, (list, tuple)):
        return _first_message(payload[0]) if payload else "Request failed."
    return str(payload)
