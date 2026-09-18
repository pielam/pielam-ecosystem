# apps/customer/decorators/login_required.py

from django.conf import settings
from django.contrib.auth.decorators import login_required as django_login_required
from django.urls import reverse_lazy

# Falls back to reverse_lazy("customer:signin") directly in case
# settings.LOGIN_URL is ever unset/removed — but as long as
# LOGIN_URL = reverse_lazy("customer:signin") stays in
# pielam/settings/base.py, this just mirrors that.
DEFAULT_LOGIN_URL = getattr(settings, "LOGIN_URL", None) or reverse_lazy("customer:signin")


def login_required(function=None, redirect_field_name="next", login_url=None):
    """
    Drop-in replacement for django.contrib.auth.decorators.login_required
    that defaults to this project's sign-in route instead of Django's
    '/accounts/login/' default, so call sites never need to hardcode a
    literal path like '/user/signin/'.

    Usage is identical to Django's own decorator:

        @login_required
        def my_view(request):
            ...

        @login_required(login_url='customer:forgot-password')
        def some_other_view(request):
            ...

    For class-based views, wrap with method_decorator as usual:

        @method_decorator(login_required, name='dispatch')
        class MyView(View):
            ...
    """
    actual_decorator = django_login_required(
        redirect_field_name=redirect_field_name,
        login_url=login_url or DEFAULT_LOGIN_URL,
    )
    if function:
        return actual_decorator(function)
    return actual_decorator