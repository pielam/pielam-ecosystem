# apps/customer/decorators/owner_required.py

from functools import wraps

from django.core.exceptions import PermissionDenied
from django.shortcuts import get_object_or_404


def owner_required(model=None, owner_field="user", pk_url_kwarg="pk",
                    get_owner=None, object_kwarg_name=None, allow_staff=True):
    """
    Ensures request.user owns the object being accessed, before the
    view runs. Two ways to use it:

    1. Automatic fetch — give it a model + the field that points at
       the owner + which URL kwarg holds the pk. It fetches the
       object for you (404 if missing) and, if ownership checks out,
       hands it to the view so you don't fetch it twice:

        @owner_required(model=Product, owner_field="owner", pk_url_kwarg="product_id")
        def edit_product(request, product_id, product):
            # `product` was already fetched and ownership-checked
            product.name = ...

    2. Fully custom — pass get_owner(request, *args, **kwargs) and
       return whatever User instance should be treated as the owner
       (e.g. derived from a slug, a related object, etc):

        @owner_required(get_owner=lambda request, slug, **kw: Campaign.objects.get(slug=slug).owner)
        def campaign_edit(request, slug):
            ...

    Behavior:
    - Not authenticated -> PermissionDenied (403). This decorator
      assumes it's stacked under login_required/jwt_login_required;
      it does NOT redirect to login itself, since "you don't own
      this" and "you're not logged in" should usually be handled by
      an explicit auth decorator higher up, not silently conflated
      here.
    - Owner mismatch -> PermissionDenied (403).
    - allow_staff=True (default): request.user.is_staff or
      is_superuser bypasses the ownership check entirely.
    """
    if model is None and get_owner is None:
        raise TypeError("owner_required() needs either `model` or `get_owner`.")

    def decorator(view_func):
        @wraps(view_func)
        def _wrapped_view(request, *args, **kwargs):
            user = getattr(request, "user", None)
            if user is None or not user.is_authenticated:
                raise PermissionDenied("Authentication required.")

            if allow_staff and (user.is_staff or user.is_superuser):
                if model is not None and object_kwarg_name:
                    obj = get_object_or_404(model, pk=kwargs.get(pk_url_kwarg))
                    kwargs[object_kwarg_name] = obj
                return view_func(request, *args, **kwargs)

            if get_owner is not None:
                owner = get_owner(request, *args, **kwargs)
                obj = None
            else:
                obj = get_object_or_404(model, pk=kwargs.get(pk_url_kwarg))
                owner = getattr(obj, owner_field, None)

            owner_pk = getattr(owner, "pk", owner)  # allow returning either a User or a raw pk
            if owner_pk is None or owner_pk != user.pk:
                raise PermissionDenied("You don't have permission to access this resource.")

            if model is not None:
                kwargs[object_kwarg_name or model._meta.model_name] = obj

            return view_func(request, *args, **kwargs)

        return _wrapped_view

    return decorator