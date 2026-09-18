# apps/customer/decorators/consent_required.py

import logging
from functools import wraps

from django.conf import settings
from django.contrib import messages
from django.shortcuts import redirect
from django.urls import reverse_lazy

logger = logging.getLogger(__name__)

# ASSUMPTION: no "accept terms" page exists yet anywhere in what we've
# built. Point this at wherever consent actually gets collected in
# your app, or say the word and I'll build that page.
DEFAULT_TERMS_REDIRECT = getattr(settings, "TERMS_REQUIRED_REDIRECT", None) or reverse_lazy("customer:profile")
DEFAULT_CONSENT_REDIRECT = getattr(settings, "DATA_CONSENT_REDIRECT", None) or reverse_lazy("customer:profile")


# ---------------------------------------------------------------------------
# terms_accepted — view-level gate
# ---------------------------------------------------------------------------

def terms_required(redirect_to=None, message=None):
    """
    Blocks a view until User.terms_accepted is True. Use on anything a
    user shouldn't reach before agreeing to your Terms & Conditions —
    posting a listing, checking out, opening a dealer dashboard, etc.

        @terms_required()
        def create_listing(request):
            ...

    Unauthenticated -> sent to sign in.
    Authenticated but hasn't accepted -> warning message + redirect
    (default: customer:profile — there's no dedicated "accept terms"
    page in the codebase yet to send them to instead).
    """
    resolved_redirect = redirect_to or DEFAULT_TERMS_REDIRECT
    resolved_message = message or "Please accept our Terms & Conditions to continue."

    def decorator(view_func):
        @wraps(view_func)
        def _wrapped_view(request, *args, **kwargs):
            user = getattr(request, "user", None)
            if user is None or not user.is_authenticated:
                messages.error(request, "Please sign in to continue.")
                return redirect("customer:signin")

            if not user.terms_accepted:
                messages.warning(request, resolved_message)
                return redirect(resolved_redirect)

            return view_func(request, *args, **kwargs)

        return _wrapped_view

    return decorator


# ---------------------------------------------------------------------------
# data_processing_consent — view-level gate
# ---------------------------------------------------------------------------

def data_consent_required(redirect_to=None, message=None):
    """
    Blocks a view unless User.data_processing_consent is True.

    Worth knowing: this field defaults to True for every signup, and
    your model currently has NO revoke_data_processing_consent()
    method (only give/revoke_marketing_consent exist) — so nothing in
    the app can actually flip it to False today. This decorator is
    correct but functionally inert until there's a way for a user to
    withdraw general data-processing consent. Do you actually want
    that as a separate, revocable setting, or should this field stay
    effectively fixed at True?

        @data_consent_required()
        def personalized_recommendations(request):
            ...
    """
    resolved_redirect = redirect_to or DEFAULT_CONSENT_REDIRECT
    resolved_message = message or "This feature requires data processing consent."

    def decorator(view_func):
        @wraps(view_func)
        def _wrapped_view(request, *args, **kwargs):
            user = getattr(request, "user", None)
            if user is None or not user.is_authenticated:
                messages.error(request, "Please sign in to continue.")
                return redirect("customer:signin")

            if not user.data_processing_consent:
                messages.warning(request, resolved_message)
                return redirect(resolved_redirect)

            return view_func(request, *args, **kwargs)

        return _wrapped_view

    return decorator


# ---------------------------------------------------------------------------
# marketing_consent — NOT a view gate. Wraps outbound marketing actions.
# ---------------------------------------------------------------------------

def marketing_consent_required(func):
    """
    Different shape on purpose: this doesn't protect a page a user
    visits — it wraps a function that SENDS something to a user (a
    promo email, a push notification, an SMS blast) and silently
    no-ops if that user hasn't opted in (User.marketing_consent).
    There's no "request" or page to redirect here; the user being
    checked is the target of an action, not the one making the call.

    Expects the target User as the first positional arg, or a `user`
    keyword:

        @marketing_consent_required
        def send_promo_email(user, campaign):
            ...

        send_promo_email(user, campaign)   # runs only if opted in
        send_promo_email(user=user, campaign=campaign)  # also works

    Returns None (and logs) when skipped, so a silent no-op doesn't
    disappear without a trace in production.
    """

    @wraps(func)
    def _wrapped(*args, **kwargs):
        user = kwargs.get("user") or (args[0] if args else None)

        if user is None or not getattr(user, "marketing_consent", False):
            logger.info(
                "Skipped %s: no marketing consent for user id=%s",
                func.__name__, getattr(user, "pk", None),
            )
            return None

        return func(*args, **kwargs)

    return _wrapped