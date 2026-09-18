# apps/customer/decorators/guest_only.py

from functools import wraps

from django.conf import settings
from django.shortcuts import redirect
from django.urls import reverse_lazy

# ASSUMPTION: mirrors settings.LOGIN_REDIRECT_URL
# (pielam/settings/base.py already sets this to customer:profile).
DEFAULT_AUTHENTICATED_REDIRECT = (
    getattr(settings, "LOGIN_REDIRECT_URL", None) or reverse_lazy("customer:profile")
)


def guest_only(redirect_to=None):
    """
    Inverse of login_required: redirects an ALREADY-authenticated user
    away from a page meant only for anonymous visitors (signin, signup,
    forgot-password) instead of showing them the form again.

        @guest_only()
        def SignInView(request):
            ...

        @guest_only(redirect_to='customer:dashboard')
        def SignUpView(request):
            ...

    Note: this only checks request.user.is_authenticated via whatever
    already populated it (Django's session middleware, or an earlier
    jwt_login_required-style decorator higher in the stack). It doesn't
    perform authentication itself.
    """
    resolved_redirect = redirect_to or DEFAULT_AUTHENTICATED_REDIRECT

    def decorator(view_func):
        @wraps(view_func)
        def _wrapped_view(request, *args, **kwargs):
            user = getattr(request, "user", None)
            if user is not None and user.is_authenticated:
                return redirect(resolved_redirect)
            return view_func(request, *args, **kwargs)

        return _wrapped_view

    return decorator