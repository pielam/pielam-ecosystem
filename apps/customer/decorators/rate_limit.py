# apps/customer/decorators/rate_limit.py

from functools import wraps

from django.core.cache import cache
from django.http import HttpResponse, JsonResponse

from apps.customer.decorators._http import get_client_ip, is_ajax_or_json_request


def _default_key(request, view_func):
    return f"{view_func.__module__}.{view_func.__name__}:{get_client_ip(request)}"


def rate_limit(max_attempts=5, window_seconds=300, key_func=None, message=None):
    """
    Blunt-instrument request throttle for brute-force-prone views
    (login, forgot-password request, OTP verify) now that reCAPTCHA
    is disabled on several of them.

        @rate_limit(max_attempts=5, window_seconds=300)
        def SignInView(request):
            ...

    Defaults to keying on (view name + client IP). Pass key_func for
    something more specific, e.g. per-identifier instead of per-IP:

        @rate_limit(max_attempts=5, window_seconds=300,
                    key_func=lambda request: request.POST.get("email_or_phone", ""))
        def ForgotPasswordView(request):
            ...

    Important caveats:
    - This counts EVERY request to the view within the window,
      success or failure — it's not the same as User.record_failed_login()
      (which only counts wrong passwords). Use both together on
      SignInView if you want IP-level throttling AND the model's
      existing per-account lockout.
    - Uses Django's cache backend as the counter. On LocMemCache with
      multiple worker processes, each process has its own counter, so
      the real effective limit becomes max_attempts * worker_count.
      Use a shared backend (Redis/Memcached) in production for this
      to actually hold.
    - This is IP-based by default, so anyone behind a shared/NAT'd IP
      (office network, campus, CGNAT) shares the same bucket. Combine
      with an identifier-based key_func on sensitive endpoints if
      that's a concern.
    """
    resolved_message = message or "Too many attempts. Please try again later."

    def decorator(view_func):
        @wraps(view_func)
        def _wrapped_view(request, *args, **kwargs):
            key = key_func(request) if key_func else _default_key(request, view_func)
            cache_key = f"rate_limit:{key}"

            attempts = cache.get(cache_key, 0)
            if attempts >= max_attempts:
                if is_ajax_or_json_request(request):
                    return JsonResponse({"detail": resolved_message}, status=429)
                return HttpResponse(resolved_message, status=429)

            # incremented before the view runs, so this counts attempts,
            # not just failures — see the docstring caveat above.
            cache.set(cache_key, attempts + 1, timeout=window_seconds)
            return view_func(request, *args, **kwargs)

        return _wrapped_view

    return decorator