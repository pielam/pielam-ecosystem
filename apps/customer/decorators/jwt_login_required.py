# apps/customer/decorators/jwt_login_required.py

from functools import wraps

from django.conf import settings
from django.contrib.auth import get_user_model
from django.contrib.auth.views import redirect_to_login
from django.http import JsonResponse
from django.urls import reverse_lazy
from rest_framework_simplejwt.exceptions import InvalidToken, TokenError
from rest_framework_simplejwt.tokens import AccessToken

User = get_user_model()

DEFAULT_LOGIN_URL = getattr(settings, "LOGIN_URL", None) or reverse_lazy("customer:signin")

# ASSUMPTION: the access token is stored in a cookie under this name
# when not sent via the Authorization header. Override via
# settings.JWT_ACCESS_COOKIE_NAME if your login flow uses a different
# cookie name.
JWT_ACCESS_COOKIE_NAME = getattr(settings, "JWT_ACCESS_COOKIE_NAME", "access_token")


def _get_token_from_request(request):
    """Authorization header first, then cookie fallback."""
    auth_header = request.headers.get("Authorization", "")
    if auth_header.startswith("Bearer "):
        return auth_header[len("Bearer "):].strip()
    return request.COOKIES.get(JWT_ACCESS_COOKIE_NAME)


def _is_ajax_or_json_request(request):
    requested_with = request.headers.get("X-Requested-With", "")
    accept = request.headers.get("Accept", "")
    content_type = request.headers.get("Content-Type", "")
    return (
        requested_with.lower() == "xmlhttprequest"
        or "application/json" in accept.lower()
        or "application/json" in content_type.lower()
    )


def _authentication_failed_response(request, redirect_field_name, login_url):
    if _is_ajax_or_json_request(request):
        return JsonResponse(
            {"detail": "Authentication credentials were not provided or are invalid."},
            status=401,
        )
    resolved_login_url = login_url or DEFAULT_LOGIN_URL
    return redirect_to_login(
        request.build_absolute_uri(),
        str(resolved_login_url),
        redirect_field_name,
    )


def jwt_login_required(function=None, redirect_field_name="next", login_url=None):
    """
    JWT-based equivalent of Django's session-based login_required.

    Looks for the access token in the Authorization header first
    ("Authorization: Bearer <token>"), then falls back to a cookie
    (see JWT_ACCESS_COOKIE_NAME above). On success, sets request.user
    to the real User instance (not Django's lazy SimpleLazyObject) so
    downstream code can use request.user normally.

    On failure:
    - AJAX / fetch / JSON requests get a 401 JSON response.
    - Everything else gets redirected to login_url (or settings.LOGIN_URL)
      with a `next` param pointing back to the original page, same as
      Django's own redirect_to_login().

    Usage is identical to Django's login_required:

        @jwt_login_required
        def my_view(request):
            ...

        @jwt_login_required(login_url='customer:signin')
        def my_other_view(request):
            ...
    """

    def decorator(view_func):
        @wraps(view_func)
        def _wrapped_view(request, *args, **kwargs):
            token = _get_token_from_request(request)
            user = None

            if token:
                try:
                    access_token = AccessToken(token)  # validates signature, expiry, token_type
                    user = User.objects.filter(pk=access_token["user_id"]).first()
                except (TokenError, InvalidToken, KeyError):
                    user = None

            # Reject missing users, deactivated accounts, and accounts
            # currently locked out (see User.is_locked) — a still-valid
            # JWT shouldn't outrun an account-level lock/suspension
            # applied after the token was issued.
            if user is None or not user.is_active or user.is_locked:
                return _authentication_failed_response(request, redirect_field_name, login_url)

            request.user = user
            return view_func(request, *args, **kwargs)

        return _wrapped_view

    if function:
        return decorator(function)
    return decorator