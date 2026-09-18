# apps/customer/decorators/_http.py
#
# Small shared helpers used by the other decorators in this package.
# Not a decorator itself — just avoids re-implementing the same two
# checks in every file.

def is_ajax_or_json_request(request):
    requested_with = request.headers.get("X-Requested-With", "")
    accept = request.headers.get("Accept", "")
    content_type = request.headers.get("Content-Type", "")
    return (
        requested_with.lower() == "xmlhttprequest"
        or "application/json" in accept.lower()
        or "application/json" in content_type.lower()
    )


def get_client_ip(request):
    forwarded_for = request.META.get("HTTP_X_FORWARDED_FOR")
    if forwarded_for:
        return forwarded_for.split(",")[0].strip()
    return request.META.get("REMOTE_ADDR")