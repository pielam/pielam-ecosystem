# apps/ponno/views/sub_categories_for_category.py

"""
Sub-Categories AJAX Endpoint
-----------------------------
GET /products/sub-categories/?category=<pk>

Returns a JSON array of active sub-categories belonging to the given
category pk. Used by the product edit form to dynamically populate
the SubCategory <select> when the user changes the Category field.

Response shape:
    [
        {"pk": 1, "sub_category_name": "Laptops"},
        {"pk": 2, "sub_category_name": "Desktops"},
        ...
    ]

Empty array is returned (not an error) when:
    - `category` param is missing or blank
    - the category has no active sub-categories
    - the category pk does not exist
"""

from django.http import JsonResponse
from django.contrib.auth.decorators import login_required
from django.views.decorators.http import require_http_methods

from apps.ponno.models.sub_category import SubCategory


@login_required
@require_http_methods(["GET"])
def sub_categories_for_category(request):
    """
    AJAX helper consumed by the product edit / create forms.

    Query params
    ------------
    category : int  — primary key of the parent Category
    """
    category_pk = request.GET.get("category", "").strip()

    if not category_pk:
        return JsonResponse([], safe=False)

    try:
        category_pk = int(category_pk)
    except ValueError:
        return JsonResponse([], safe=False)

    sub_categories = (
        SubCategory.objects
        .filter(
            category_id=category_pk,
            is_active=True,
            deleted_at__isnull=True,
        )
        .order_by("display_order", "sub_category_name")
        .values("pk", "sub_category_name")
    )

    return JsonResponse(list(sub_categories), safe=False)