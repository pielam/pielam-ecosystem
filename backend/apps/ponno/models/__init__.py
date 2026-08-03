# apps/ponno/models/__init__.py
#
# Every model defined in this package must be importable from here. Django
# only registers a model once the module defining it is imported, so a model
# missing from this file is silently absent from migrations and from
# ``from apps.ponno.models import X``.

from .brand import Brand, BrandManager
from .category import Category, CategoryManager
from .sub_category import SubCategory, SubCategoryManager
from .product import (
    Order,
    OrderItem,
    Product,
    ProductManager,
    ProductView,
    SearchHistory,
    Wishlist,
)
from .product_view_log import ProductViewLog, ProductViewLogManager
from .rating import ProductRating
from .discovery_visit_log import DiscoveryVisitLog

__all__ = [
    "Brand",
    "BrandManager",
    "Category",
    "CategoryManager",
    "SubCategory",
    "SubCategoryManager",
    "Product",
    "ProductManager",
    "ProductView",
    "Wishlist",
    "Order",
    "OrderItem",
    "SearchHistory",
    "ProductViewLog",
    "ProductViewLogManager",
    "ProductRating",
    "DiscoveryVisitLog",
]
