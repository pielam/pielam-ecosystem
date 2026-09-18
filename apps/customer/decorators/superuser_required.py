# apps/customer/decorators/superuser_required.py

from functools import wraps

from django.conf import settings
from django.contrib.auth.views import redirect_to_login
from django.core.exceptions import PermissionDenied
from django.urls import reverse_lazy

DEFAULT_LOGIN_URL = getattr(settings, "LOGIN_URL", None) or reverse_lazy("customer:signin")


def superuser_required(function=None, login_url=None, redirect_field_name="next"):
    """
    Simple is_superuser gate — distinct from role_required(User.Role.ADMIN):
    role is a self-reportable field on the model (an admin-role account
    isn't necessarily is_superuser=True, and vice versa — e.g. a
    createsuperuser account defaults to role=USER unless set otherwise).
    Use this specifically when a view must be superuser-only regardless
    of the `role` field.

        @superuser_required
        def dangerous_admin_tool(request):
            ...

    Not authenticated -> redirect to login.
    Authenticated, not a superuser -> PermissionDenied (403).
    """

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

            if not user.is_superuser:
                raise PermissionDenied("Superuser access required.")

            return view_func(request, *args, **kwargs)

        return _wrapped_view

    if function:
        return decorator(function)
    return decorator