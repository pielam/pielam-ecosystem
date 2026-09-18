# apps/customer/decorators/role_required.py

from functools import wraps

from django.conf import settings
from django.contrib.auth.views import redirect_to_login
from django.core.exceptions import PermissionDenied
from django.urls import reverse_lazy

DEFAULT_LOGIN_URL = getattr(settings, "LOGIN_URL", None) or reverse_lazy("customer:signin")


def role_required(*allowed_roles, allow_admin_bypass=True, login_url=None, redirect_field_name="next"):
    """
    Restrict a view to specific User.Role values.

        @role_required(User.Role.BUSINESS)
        def dealer_dashboard(request):
            ...

        @role_required(User.Role.BUSINESS, User.Role.STAFF)
        def shared_view(request):
            ...

    Behavior:
    - Unauthenticated users are redirected to login (same as
      login_required), since role checks are meaningless without an
      authenticated user first.
    - Authenticated users whose role isn't in `allowed_roles` raise
      PermissionDenied, which Django renders as a 403 (respecting a
      custom 403.html template if you have one). A redirect would be
      misleading here since the user IS logged in — they're just not
      allowed on this specific page.
    - By default, User.Role.ADMIN and is_superuser accounts always
      pass, on the assumption admins can reach anything role-gated
      views can. Set allow_admin_bypass=False to require the exact
      role(s) listed even for admins.

    Stack this UNDER an explicit login_required/jwt_login_required if
    you want to be certain which auth mechanism populates request.user
    (this decorator doesn't care which one did, as long as
    request.user ends up authenticated by the time it runs):

        @login_required
        @role_required(User.Role.BUSINESS)
        def my_view(request):
            ...
    """
    if not allowed_roles:
        raise TypeError(
            "role_required() requires at least one role, "
            "e.g. @role_required(User.Role.BUSINESS)"
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

            if allow_admin_bypass and (user.is_superuser or getattr(user, "is_admin", False)):
                return view_func(request, *args, **kwargs)

            if user.role not in allowed_roles:
                raise PermissionDenied(
                    "You don't have permission to access this page."
                )

            return view_func(request, *args, **kwargs)

        return _wrapped_view

    return decorator