"""
Catalogue serializers.

Three shapes per resource, wired up through ``MultiSerializerMixin``:

``*MiniSerializer``
    Embedded inside other payloads. Never triggers a query of its own -- the
    viewset ``select_related``s the relation.
``*ListSerializer``
    What a grid or feed needs, and nothing more.
``*DetailSerializer`` / ``*WriteSerializer``
    Full read payload / the writable subset.

Nothing here declares ``fields = "__all__"``. Explicit field lists are the
only thing that stops a later model migration from silently publishing a new
column -- and several of these models carry ``buying_price``, ``metadata``
and soft-delete audit columns that must not reach the public API.
"""

from __future__ import annotations

from rest_framework import serializers

from apps.core.serializers import BaseModelSerializer
from apps.ponno.models import (
    Brand,
    Category,
    Order,
    OrderItem,
    Product,
    ProductRating,
    SearchHistory,
    SubCategory,
    Wishlist,
)


# ==========================================================================
# Shared helpers
# ==========================================================================

class _ImageURLMixin:
    """
    Serialise an image field as an absolute URL.

    Models expose ``logo_url``/``image_url`` properties that return a media
    path; a bare path is useless to a client on another origin, so it is
    resolved against the incoming request.
    """

    def _absolute(self, obj, attr):
        try:
            value = getattr(obj, attr, None)
        except ValueError:
            # FileField raises when no file is associated.
            return None
        return self.build_absolute_uri(value)


# ==========================================================================
# Brand
# ==========================================================================

class BrandMiniSerializer(BaseModelSerializer, _ImageURLMixin):
    logo = serializers.SerializerMethodField()

    class Meta:
        model = Brand
        fields = ("id", "uuid", "brand_name", "brand_slug", "logo", "is_verified")
        read_only_fields = fields

    def get_logo(self, obj):
        return self._absolute(obj, "logo_url")


class BrandListSerializer(BrandMiniSerializer):
    class Meta(BrandMiniSerializer.Meta):
        fields = BrandMiniSerializer.Meta.fields + (
            "brand_type",
            "brand_tagline",
            "country_of_origin",
            "is_official",
            "is_featured",
            "is_trending",
            "product_count",
            "display_order",
        )
        read_only_fields = fields


class BrandDetailSerializer(BrandListSerializer):
    banner = serializers.SerializerMethodField()
    icon = serializers.SerializerMethodField()

    class Meta(BrandListSerializer.Meta):
        fields = BrandListSerializer.Meta.fields + (
            "banner",
            "icon",
            "brand_description",
            "brand_story",
            "company_name",
            "founded_year",
            "headquarters",
            "brand_website",
            "brand_email",
            "brand_phone",
            "support_email",
            "support_phone",
            "social_facebook",
            "social_instagram",
            "social_twitter",
            "social_linkedin",
            "social_youtube",
            "verification_status",
            "is_trusted",
            "is_exclusive",
            "is_active",
            "meta_title",
            "meta_description",
            "view_count",
            "popularity_score",
            "brand_created_at",
        )
        read_only_fields = fields

    def get_banner(self, obj):
        return self._absolute(obj, "banner_url")

    def get_icon(self, obj):
        return self._absolute(obj, "icon_url")


class BrandWriteSerializer(BaseModelSerializer):
    """
    Writable subset.

    Excluded on purpose: ``uuid`` and ``brand_slug`` (derived), the
    verification columns (``is_verified``, ``verified_by``, ``verified_at``,
    ``is_official``, ``is_trusted``) which are a staff decision and were
    previously settable by whoever created the brand, every counter
    (``view_count``, ``product_count``, ``popularity_score``), the soft-delete
    audit columns, ``created_by``/``managed_by`` (stamped server-side), and
    ``metadata``, which is a free-form JSON blob with no schema.
    """

    class Meta:
        model = Brand
        fields = (
            "id",
            "brand_name",
            "brand_type",
            "brand_description",
            "brand_tagline",
            "brand_story",
            "brand_logo",
            "brand_banner",
            "brand_icon",
            "company_name",
            "founded_year",
            "country_of_origin",
            "headquarters",
            "brand_website",
            "brand_email",
            "brand_phone",
            "support_email",
            "support_phone",
            "social_facebook",
            "social_instagram",
            "social_twitter",
            "social_linkedin",
            "social_youtube",
            "is_active",
            "meta_title",
            "meta_description",
            "meta_keywords",
            "display_order",
        )
        read_only_fields = ("id",)


# ==========================================================================
# Category / SubCategory
# ==========================================================================

class CategoryMiniSerializer(BaseModelSerializer, _ImageURLMixin):
    image = serializers.SerializerMethodField()

    class Meta:
        model = Category
        fields = ("id", "uuid", "category_name", "category_slug", "image", "level")
        read_only_fields = fields

    def get_image(self, obj):
        return self._absolute(obj, "image_url")


class CategoryListSerializer(CategoryMiniSerializer):
    parent = CategoryMiniSerializer(read_only=True)

    class Meta(CategoryMiniSerializer.Meta):
        fields = CategoryMiniSerializer.Meta.fields + (
            "parent",
            "category_type",
            "category_short_description",
            "icon_class",
            "color_code",
            "is_featured",
            "is_trending",
            "is_visible_in_menu",
            "display_order",
            "product_count",
        )
        read_only_fields = fields


class CategoryDetailSerializer(CategoryListSerializer):
    breadcrumb = serializers.SerializerMethodField()

    class Meta(CategoryListSerializer.Meta):
        fields = CategoryListSerializer.Meta.fields + (
            "breadcrumb",
            "category_description",
            "display_style",
            "products_per_page",
            "show_subcategories",
            "is_visible_on_homepage",
            "is_active",
            "meta_title",
            "meta_description",
            "canonical_url",
            "view_count",
            "popularity_score",
            "min_price",
            "max_price",
            "category_created_at",
        )
        read_only_fields = fields

    def get_breadcrumb(self, obj):
        """``breadcrumb`` is a model property returning ancestor rows."""
        crumbs = getattr(obj, "breadcrumb", None) or []
        return [
            {
                "id": node.pk,
                "category_name": node.category_name,
                "category_slug": node.category_slug,
            }
            for node in crumbs
        ]


class CategoryTreeSerializer(CategoryMiniSerializer):
    """Recursive menu payload. Depth is bounded by the model's level<=10 check."""

    children = serializers.SerializerMethodField()

    class Meta(CategoryMiniSerializer.Meta):
        fields = CategoryMiniSerializer.Meta.fields + (
            "icon_class", "color_code", "display_order", "product_count", "children",
        )
        read_only_fields = fields

    def get_children(self, obj):
        children = [
            child for child in obj.children.all()
            if child.is_active and child.deleted_at is None
        ]
        return CategoryTreeSerializer(children, many=True, context=self.context).data


class CategoryWriteSerializer(BaseModelSerializer):
    """
    Writable subset. ``level``/``path`` are derived in ``Category.save()`` and
    counters are maintained by the model, so neither is accepted here.
    """

    class Meta:
        model = Category
        fields = (
            "id",
            "brand",
            "category_name",
            "category_type",
            "parent",
            "category_description",
            "category_short_description",
            "category_image",
            "category_icon",
            "category_thumbnail",
            "icon_class",
            "color_code",
            "is_active",
            "is_visible_in_menu",
            "is_visible_on_homepage",
            "display_style",
            "display_order",
            "products_per_page",
            "show_subcategories",
            "meta_title",
            "meta_description",
            "meta_keywords",
            "canonical_url",
            "commission_rate",
            "min_price",
            "max_price",
        )
        read_only_fields = ("id",)

    def validate_parent(self, value):
        """
        Reject a cycle.

        ``move_to()`` guards this on the model, but a plain PATCH of ``parent``
        does not go through it -- and a cycle makes ``update_level()`` recurse
        until the request dies.
        """
        if value is None:
            return value
        if self.instance is not None:
            if value.pk == self.instance.pk:
                raise serializers.ValidationError("A category cannot be its own parent.")
            node = value
            seen = set()
            while node is not None and node.pk not in seen:
                seen.add(node.pk)
                if node.pk == self.instance.pk:
                    raise serializers.ValidationError(
                        "That parent is a descendant of this category."
                    )
                node = node.parent
        return value


class SubCategoryMiniSerializer(BaseModelSerializer, _ImageURLMixin):
    image = serializers.SerializerMethodField()

    class Meta:
        model = SubCategory
        fields = ("id", "uuid", "sub_category_name", "sub_category_slug", "image")
        read_only_fields = fields

    def get_image(self, obj):
        return self._absolute(obj, "image_url")


class SubCategoryListSerializer(SubCategoryMiniSerializer):
    category = CategoryMiniSerializer(read_only=True)

    class Meta(SubCategoryMiniSerializer.Meta):
        fields = SubCategoryMiniSerializer.Meta.fields + (
            "category",
            "sub_category_type",
            "sub_category_short_description",
            "icon_class",
            "color_code",
            "is_featured",
            "is_trending",
            "is_visible_in_menu",
            "display_order",
            "product_count",
        )
        read_only_fields = fields


class SubCategoryDetailSerializer(SubCategoryListSerializer):
    brand = BrandMiniSerializer(read_only=True)

    class Meta(SubCategoryListSerializer.Meta):
        fields = SubCategoryListSerializer.Meta.fields + (
            "brand",
            "sub_category_description",
            "display_style",
            "products_per_page",
            "is_visible_on_homepage",
            "is_active",
            "meta_title",
            "meta_description",
            "canonical_url",
            "view_count",
            "popularity_score",
            "min_price",
            "max_price",
            "sub_category_created_at",
        )
        read_only_fields = fields


class SubCategoryWriteSerializer(BaseModelSerializer):
    class Meta:
        model = SubCategory
        fields = (
            "id",
            "category",
            "brand",
            "sub_category_name",
            "sub_category_type",
            "sub_category_description",
            "sub_category_short_description",
            "sub_category_image",
            "sub_category_icon",
            "sub_category_thumbnail",
            "icon_class",
            "color_code",
            "is_active",
            "is_visible_in_menu",
            "is_visible_on_homepage",
            "display_style",
            "display_order",
            "products_per_page",
            "meta_title",
            "meta_description",
            "meta_keywords",
            "canonical_url",
            "commission_rate",
            "min_price",
            "max_price",
        )
        read_only_fields = ("id",)


# ==========================================================================
# Product
# ==========================================================================

class ProductListSerializer(BaseModelSerializer, _ImageURLMixin):
    brand = BrandMiniSerializer(read_only=True)
    category = CategoryMiniSerializer(read_only=True)
    sub_category = SubCategoryMiniSerializer(read_only=True)
    image = serializers.SerializerMethodField()
    is_in_stock = serializers.BooleanField(read_only=True)
    is_on_sale = serializers.BooleanField(read_only=True)
    selling_price = serializers.SerializerMethodField()
    brand_price = serializers.SerializerMethodField()

    class Meta:
        model = Product
        fields = (
            "id",
            "product_id",
            "product_name",
            "product_title",
            "slug",
            "brand",
            "category",
            "sub_category",
            "image",
            "short_description",
            "product_condition",
            "selling_price",
            "brand_price",
            "final_price",
            "discount_percentage",
            "currency",
            "stock_status",
            "is_in_stock",
            "is_on_sale",
            "rating_average",
            "review_count",
            "is_featured",
            "is_trending",
            "is_verified",
            "created_at",
        )
        read_only_fields = fields

    def get_image(self, obj):
        return self._absolute(obj, "image_url")

    def get_selling_price(self, obj):
        # The model carries per-field visibility switches; honour them instead
        # of shipping the number and hoping the front end hides it.
        return obj.selling_price if obj.is_selling_price_visible else None

    def get_brand_price(self, obj):
        return obj.brand_price if obj.is_brand_price_visible else None


class ProductDetailSerializer(ProductListSerializer):
    dealer = serializers.SerializerMethodField()
    buying_price = serializers.SerializerMethodField()
    discount_amount = serializers.DecimalField(max_digits=12, decimal_places=2, read_only=True)
    is_wishlisted = serializers.SerializerMethodField()
    my_rating = serializers.SerializerMethodField()

    class Meta(ProductListSerializer.Meta):
        fields = ProductListSerializer.Meta.fields + (
            "dealer",
            "sku",
            "barcode",
            "description",
            "warrenty_info",
            "delivery_info",
            "video_url",
            "buying_price",
            "discount_amount",
            "tax_rate",
            "stock",
            "low_stock_threshold",
            "min_order_quantity",
            "max_order_quantity",
            "allow_backorder",
            "weight",
            "length",
            "width",
            "height",
            "free_shipping",
            "shipping_cost",
            "meta_title",
            "meta_description",
            "view_count",
            "wishlist_count",
            "total_sales",
            "share_count",
            "is_active",
            "updated_at",
            "is_wishlisted",
            "my_rating",
        )
        read_only_fields = fields

    def get_dealer(self, obj):
        dealer = obj.dealer
        if dealer is None:
            return None
        return {
            "id": dealer.pk,
            "display_name": getattr(dealer, "get_display_name", lambda: None)()
            or getattr(dealer, "email_or_phone", None),
        }

    def get_buying_price(self, obj):
        """
        Cost price is commercially sensitive.

        Even with ``is_buying_price_visible`` set, it is only returned to the
        dealer who owns the product (or to staff) -- a public shopper must
        never see a seller's margin.
        """
        user = self.current_user
        if user is None:
            return None
        privileged = bool(
            getattr(user, "is_staff", False)
            or getattr(user, "is_superuser", False)
            or getattr(user, "role", None) == "admin"
        )
        if not (privileged or obj.dealer_id == user.pk):
            return None
        return obj.buying_price

    def get_is_wishlisted(self, obj):
        user = self.current_user
        if user is None:
            return False
        return Wishlist.objects.filter(user=user, product=obj).exists()

    def get_my_rating(self, obj):
        user = self.current_user
        if user is None:
            return None
        rating = ProductRating.objects.filter(user=user, product=obj).first()
        return rating.rating if rating else None


class ProductWriteSerializer(BaseModelSerializer):
    """
    Dealer-writable subset.

    ``dealer`` is stamped from ``request.user`` by the viewset, not accepted
    from the body -- otherwise any dealer could file a product under a
    competitor's account. ``slug``, ``final_price`` and ``stock_status`` are
    derived in ``Product.save()``; every counter, the verification flag and
    the soft-delete columns are server-owned.
    """

    class Meta:
        model = Product
        fields = (
            "id",
            "product_name",
            "product_title",
            "sku",
            "barcode",
            "brand",
            "category",
            "sub_category",
            "description",
            "short_description",
            "warrenty_info",
            "delivery_info",
            "product_condition",
            "image",
            "video_url",
            "brand_price",
            "is_brand_price_visible",
            "buying_price",
            "is_buying_price_visible",
            "selling_price",
            "is_selling_price_visible",
            "discount_percentage",
            "tax_rate",
            "currency",
            "stock",
            "low_stock_threshold",
            "track_inventory",
            "allow_backorder",
            "min_order_quantity",
            "max_order_quantity",
            "weight",
            "length",
            "width",
            "height",
            "free_shipping",
            "shipping_cost",
            "meta_title",
            "meta_description",
            "meta_keywords",
            "is_active",
        )
        read_only_fields = ("id",)

    def validate(self, attrs):
        def current(name):
            if name in attrs:
                return attrs[name]
            return getattr(self.instance, name, None)

        sub_category = current("sub_category")
        category = current("category")
        if sub_category is not None and category is not None:
            if sub_category.category_id != category.pk:
                raise serializers.ValidationError({
                    "sub_category": "That sub-category does not belong to the selected category.",
                })

        minimum = current("min_order_quantity")
        maximum = current("max_order_quantity")
        if minimum and maximum and minimum > maximum:
            raise serializers.ValidationError({
                "max_order_quantity": "Maximum order quantity cannot be below the minimum.",
            })

        return attrs


# ==========================================================================
# Engagement: wishlist, ratings, search history
# ==========================================================================

class WishlistSerializer(BaseModelSerializer):
    product = ProductListSerializer(read_only=True)
    product_id = serializers.PrimaryKeyRelatedField(
        source="product",
        queryset=Product.objects.filter(deleted_at__isnull=True, is_active=True),
        write_only=True,
    )

    class Meta:
        model = Wishlist
        # ``user`` is absent from the writable set on purpose: the viewset
        # stamps it from the session.
        fields = ("id", "product", "product_id", "note", "added_at")
        read_only_fields = ("id", "added_at")


class ProductRatingSerializer(BaseModelSerializer):
    product = serializers.PrimaryKeyRelatedField(
        queryset=Product.objects.filter(deleted_at__isnull=True, is_active=True),
    )
    user_display = serializers.SerializerMethodField()

    class Meta:
        model = ProductRating
        fields = ("id", "product", "rating", "rated_at", "user_display")
        read_only_fields = ("id", "rated_at", "user_display")

    def get_user_display(self, obj):
        return getattr(obj.user, "email_or_phone", None)

    def validate_rating(self, value):
        if not 1 <= value <= 5:
            raise serializers.ValidationError("Rating must be between 1 and 5.")
        return value


class SearchHistorySerializer(BaseModelSerializer):
    class Meta:
        model = SearchHistory
        fields = ("id", "query", "result_count", "searched_at", "search_count")
        read_only_fields = fields


# ==========================================================================
# Orders
# ==========================================================================

class OrderItemSerializer(BaseModelSerializer):
    class Meta:
        model = OrderItem
        fields = (
            "id",
            "product",
            "product_name",
            "product_sku",
            "product_image",
            "quantity",
            "unit_price",
            "discount_pct",
            "line_total",
        )
        read_only_fields = fields


class OrderSerializer(BaseModelSerializer):
    items = OrderItemSerializer(many=True, read_only=True)

    class Meta:
        model = Order
        fields = (
            "id",
            "uuid",
            "order_number",
            "status",
            "payment_status",
            "subtotal",
            "discount_total",
            "shipping_total",
            "grand_total",
            "currency",
            "shipping_name",
            "shipping_phone",
            "shipping_address",
            "shipping_city",
            "shipping_country",
            "placed_at",
            "updated_at",
            "delivered_at",
            "notes",
            "items",
        )
        read_only_fields = fields


class OrderItemInputSerializer(serializers.Serializer):
    """One line of a checkout request. Prices are never taken from the client."""

    product = serializers.PrimaryKeyRelatedField(
        queryset=Product.objects.filter(deleted_at__isnull=True, is_active=True),
    )
    quantity = serializers.IntegerField(min_value=1, max_value=1000)


class OrderCreateSerializer(serializers.Serializer):
    """
    Checkout input.

    Totals are deliberately absent: the previous web flow trusted the posted
    price, which lets a client buy anything for zero. Every monetary value is
    recomputed from the product rows inside the viewset.
    """

    items = OrderItemInputSerializer(many=True, allow_empty=False)
    shipping_name = serializers.CharField(max_length=150)
    shipping_phone = serializers.CharField(max_length=20)
    shipping_address = serializers.CharField()
    shipping_city = serializers.CharField(max_length=100, required=False, allow_blank=True)
    shipping_country = serializers.CharField(max_length=2, required=False, allow_blank=True)
    notes = serializers.CharField(required=False, allow_blank=True)

    def validate_items(self, value):
        seen = set()
        for line in value:
            product = line["product"]
            if product.pk in seen:
                raise serializers.ValidationError(
                    f"Product {product.pk} appears more than once; combine the quantities."
                )
            seen.add(product.pk)
        return value
