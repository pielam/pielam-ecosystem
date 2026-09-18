# apps/customer/decorators/ajax_required.py

from functools import wraps

from django.http import HttpResponseBadRequest, JsonResponse

from apps.customer.decorators._http import is_ajax_or_json_request


def ajax_required(view_func):
    """
    Restricts a view to XHR/fetch requests only — for endpoints meant
    to be called from JS, not visited directly in a browser tab
    (e.g. follow/unfollow, wishlist_toggle, load_more_products,
    load_more_services in public_profile.py).

        @ajax_required
        def wishlist_toggle(request, product_id):
            ...

    A non-AJAX request gets a 400 rather than silently running the
    view — this is meant to catch accidental direct navigation /
    stray GETs, not to serve as an auth or CSRF boundary. It doesn't
    replace login_required, CSRF protection, or require_POST/GET;
    stack those separately as needed.
    """

    @wraps(view_func)
    def _wrapped_view(request, *args, **kwargs):
        if not is_ajax_or_json_request(request):
            if request.headers.get("Accept", "").lower().startswith("application/json"):
                return JsonResponse(
                    {"detail": "This endpoint only accepts AJAX/fetch requests."},
                    status=400,
                )
            return HttpResponseBadRequest("This endpoint only accepts AJAX/fetch requests.")
        return view_func(request, *args, **kwargs)

    return _wrapped_view