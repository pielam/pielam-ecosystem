# apps/customer/decorators/mfa_required.py
#
# IMPORTANT — read before using this one.
#
# Your User model has the STORAGE for MFA (mfa_enabled, mfa_method,
# mfa_secret) but there is no MFA challenge view, no TOTP/SMS-code
# verification endpoint, and no session flag anywhere in the code
# we've written so far that records "this session actually completed
# an MFA challenge." That infrastructure doesn't exist yet.
#
# This decorator can only meaningfully do half the job right now:
# check whether MFA is enabled on the account and send the user to
# set it up if not. The second half — "has THIS session actually
# passed the MFA challenge" — is stubbed against a session key
# (`mfa_verified`) that nothing currently sets, because nothing
# currently challenges the user for a code. Until an MFA
# setup/challenge view exists, that branch will always redirect to
# the (also nonexistent) challenge view. Treat this as scaffolding to
# build against, not a working security control yet.

from functools import wraps

from django.conf import settings
from django.contrib import messages
from django.shortcuts import redirect
from django.urls import reverse_lazy

# ASSUMPTION: these views/URL names don't exist yet.
DEFAULT_MFA_SETUP_URL = getattr(settings, "MFA_SETUP_URL", None) or reverse_lazy("customer:mfa-setup")
DEFAULT_MFA_CHALLENGE_URL = getattr(settings, "MFA_CHALLENGE_URL", None) or reverse_lazy("customer:mfa-challenge")

SESSION_MFA_VERIFIED_KEY = "mfa_verified"


def mfa_required(setup_url=None, challenge_url=None):
    """
    Requires MFA to be both (a) enabled on the account and (b) passed
    for the current session, before allowing access.

        @mfa_required()
        def sensitive_admin_action(request):
            ...

    See the module docstring above — (b) is not enforceable until an
    MFA challenge view sets request.session['mfa_verified'] = True
    after a successful code check. Want that view built next?
    """
    resolved_setup_url = setup_url or DEFAULT_MFA_SETUP_URL
    resolved_challenge_url = challenge_url or DEFAULT_MFA_CHALLENGE_URL

    def decorator(view_func):
        @wraps(view_func)
        def _wrapped_view(request, *args, **kwargs):
            user = getattr(request, "user", None)
            if user is None or not user.is_authenticated:
                messages.error(request, "Please sign in to continue.")
                return redirect("customer:signin")

            if not user.mfa_enabled:
                messages.warning(
                    request,
                    "This action requires two-factor authentication. "
                    "Please set it up to continue."
                )
                return redirect(resolved_setup_url)

            if not request.session.get(SESSION_MFA_VERIFIED_KEY):
                return redirect(resolved_challenge_url)

            return view_func(request, *args, **kwargs)

        return _wrapped_view

    return decorator