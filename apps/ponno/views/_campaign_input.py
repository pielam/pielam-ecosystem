# apps/ponno/views/_campaign_input.py

"""
Manual request-data parsing for the campaign create/edit views, used
instead of Django forms.py. Each helper returns (value, error_message)
so a view can collect all field errors in one pass rather than
exception-per-field. Model-level validation (Campaign.clean() via
full_clean() inside Campaign.save()) is still the actual source of
truth for cross-field rules — these helpers only handle "did the raw
POST value parse into the right type at all".
"""

from decimal import Decimal, InvalidOperation
from typing import Optional, Tuple

from django.utils.dateparse import parse_datetime
from django.utils import timezone


def parse_decimal(raw: Optional[str], *, required: bool = False, field_label: str = "value") -> Tuple[Optional[Decimal], Optional[str]]:
    if raw is None or raw.strip() == '':
        if required:
            return None, f"{field_label} is required."
        return None, None
    try:
        return Decimal(raw.strip()), None
    except InvalidOperation:
        return None, f"{field_label} must be a valid number."


def parse_int(raw: Optional[str], *, default: int = 0, field_label: str = "value") -> Tuple[int, Optional[str]]:
    if raw is None or raw.strip() == '':
        return default, None
    try:
        return int(raw.strip()), None
    except ValueError:
        return default, f"{field_label} must be a whole number."


def parse_datetime_local(raw: Optional[str], *, required: bool = False, field_label: str = "value") -> Tuple[Optional[object], Optional[str]]:
    """
    Parses the string produced by an <input type="datetime-local">
    (e.g. '2026-08-20T18:30') or a full ISO datetime. Makes the result
    timezone-aware using the current timezone if USE_TZ is on and the
    parsed value came back naive.
    """
    if raw is None or raw.strip() == '':
        if required:
            return None, f"{field_label} is required."
        return None, None

    dt = parse_datetime(raw.strip())
    if dt is None:
        return None, f"{field_label} must be a valid date/time."

    if timezone.is_naive(dt) and getattr(timezone, 'get_current_timezone', None):
        dt = timezone.make_aware(dt, timezone.get_current_timezone())

    return dt, None


def parse_bool(raw: Optional[str]) -> bool:
    """HTML checkboxes only send a value when checked; absence = False."""
    return raw is not None


def parse_id_list(post_data, field_name: str) -> list:
    """QueryDict.getlist for a <select multiple> or repeated checkboxes."""
    return [v for v in post_data.getlist(field_name) if v]