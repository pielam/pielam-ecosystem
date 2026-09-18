# apps/customer/decorators/verified_required.py

from functools import wraps

from django.conf import settings
from django.contrib import messages
from django.contrib.auth.views import redirect_to_login
from django.shortcuts import redirect
from django.urls import reverse_lazy

DEFAULT_LOGIN_URL = getattr(settings, "LOGIN_URL", None) or reverse_lazy("customer:signin")

# Where an authenticated-but-unverified user gets sent. ASSUMPTION:
# your profile page is the right landing spot to show verification
# status/prompts — point this elsewhere if you build a dedicated
# "please verify your account" screen instead.
DEFAULT_UNVERIFIED_REDIRECT = reverse_lazy("customer:profile")


def verified_required(redirect_to=None, message=None, login_url=None, redirect_field_name="next"):
    """
    Blocks access to a view unless the account has verified email or
    phone (User.is_verified). Accounts can log in while
    account_status=PENDING_VERIFICATION (see SignInView — verification
    status doesn't currently gate login itself), so use this decorator
    on any view where you specifically want to require verification
    before letting someone proceed (posting a listing, checkout, etc).

        @verified_required()
        def create_listing(request):
            ...

        @verified_required(redirect_to='customer:some-other-page')
        def another_view(request):
            ...

    Behavior:
    - Unauthenticated users are redirected to login first (a role/
      verification check is meaningless without an authenticated user).
    - Authenticated-but-unverified users get a warning message and are
      redirected to `redirect_to` (default: customer:profile) rather
      than a hard 403, since "go verify your account" is an actionable
      next step, not a permission wall.

    Stack this alongside login_required/jwt_login_required if you want
    an explicit, unambiguous authentication check first:

        @login_required
        @verified_required()
        def my_view(request):
            ...
    """
    resolved_redirect = redirect_to or DEFAULT_UNVERIFIED_REDIRECT
    resolved_message = message or (
        "Please verify your email or phone number before continuing."
    )

    def decorator(view_func):
        @wraps(view_func)
        def _wrapped_view(request, *args, **kwargs):
            user = getattr(request, "user", None)

            if user is None or not user.is_authenticated:
                resolved_login_url = login_url or DEFAULT_LOGIN_URL
                return redirect_to_login(
                    request.build_absolute_uri(),
                    str(resolved_login_url),
                    redirect_field_name,
                )

            if not user.is_verified:
                messages.warning(request, resolved_message)
                return redirect(resolved_redirect)

            return view_func(request, *args, **kwargs)

        return _wrapped_view

    return decorator