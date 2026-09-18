from django.contrib import admin

# Register your models here.
from django.contrib import admin
from django.contrib.auth.admin import UserAdmin
from django import forms
from apps.customer.models.account import User
from apps.ponno.models.brand import Brand
from apps.ponno.models.category import Category
from apps.ponno.models.product import Product
# write your import here
from django.contrib import admin
 
from django.utils.html import format_html
from django.utils.text import slugify


# Register your models here.

# ------------------------------------------------------------
# BRAND ADMIN
# ------------------------------------------------------------
@admin.register(Brand)
class BrandAdmin(admin.ModelAdmin):
    list_display = (
        'brand_name',
        'brand_slug',
        'brand_created_at',
        'brand_updated_at',
    )
    search_fields = ('brand_name',)
    prepopulated_fields = {'brand_slug': ('brand_name',)}
    ordering = ('brand_name',)
    readonly_fields = ('brand_created_at', 'brand_updated_at')


# ------------------------------------------------------------
# CATEGORY ADMIN
# ------------------------------------------------------------
@admin.register(Category)
class CategoryAdmin(admin.ModelAdmin):
    list_display = (
        'category_name',
        'category_slug',
        'brand',
        'parent',
        'level',
        'is_active',
        'is_featured',
        'product_count',
        'category_created_at',
        'category_updated_at',
    )
    list_filter = (
        'is_active',
        'is_featured',
        'is_trending',
        'category_type',
        'brand',
        'level',
    )
    search_fields = ('category_name', 'category_slug', 'brand__brand_name')
    prepopulated_fields = {'category_slug': ('category_name',)}
    ordering = ('display_order', 'category_name')
    readonly_fields = ('category_created_at', 'category_updated_at', 'level', 'path')
    raw_id_fields = ('brand', 'parent', 'deleted_by')
    autocomplete_fields = ('brand',)



from apps.ponno.models.sub_category import SubCategory
# ------------------------------------------------------------
# PRODUCT ADMIN
# ------------------------------------------------------------
@admin.register(Product)
class ProductAdmin(admin.ModelAdmin):
    def image_preview(self, obj):
        if obj.image:
            return format_html(
                '<img src="{}" width="70" height="70" style="object-fit:cover;border-radius:5px;"/>',
                obj.image.url,
            )
        return "(No image)"

    image_preview.short_description = 'Preview'

    list_display = (
        'image_preview',
        'product_id',
        'product_name',
        'slug',
        'category',
        'brand',
        'sub_category',
  
        
        'buying_price',
        'brand_price',
        'selling_price',
        'stock',
        'is_active',
        'created_at',
        'updated_at',
    )
    list_filter = ('brand', 'category', 'sub_category', 'is_active')
    search_fields = ('product_id','product_name', 'brand__brand_name', 'category__category_name', 'sub_category__sub_category_name')
    list_editable = ('is_active', 'stock', 'buying_price', 'brand_price', 'selling_price')
    ordering = ('product_name',)
    readonly_fields = ('created_at', 'updated_at', 'product_id', 'slug')


# apps/ponno/admin/sub_category_admin.py

"""
SubCategory Django Admin Configuration
---------------------------------------
Features:
- Full list display with key fields
- Advanced search and filtering
- Inline product count display
- Bulk actions (activate, deactivate, feature, soft delete, restore)
- Slug auto-generation
- Read-only audit fields
- Grouped fieldsets for clean UX
- Custom admin actions with confirmation
- Export data support
"""

from django.contrib import admin
from django.utils import timezone
from django.utils.html import format_html
from django.utils.translation import gettext_lazy as _
from django.db.models import Count
from django.contrib import messages
from django.http import HttpResponse
import csv

from apps.ponno.models.sub_category import SubCategory


# ====================================================================
# INLINE (optional: use inside CategoryAdmin to show subcategories)
# ====================================================================

class SubCategoryInline(admin.TabularInline):
    """
    Inline to show subcategories inside the parent CategoryAdmin.
    Register this in your CategoryAdmin via `inlines = [SubCategoryInline]`.
    """
    model = SubCategory
    extra = 0
    fields = (
        'sub_category_name',
        'sub_category_slug',
        'is_active',
        'is_featured',
        'display_order',
        'product_count',
    )
    readonly_fields = ('sub_category_slug', 'product_count')
    show_change_link = True
    ordering = ('display_order', 'sub_category_name')


# ====================================================================
# MAIN ADMIN
# ====================================================================

@admin.register(SubCategory)
class SubCategoryAdmin(admin.ModelAdmin):
    """
    Admin interface for the SubCategory model.
    """

    # ================================================================
    # LIST VIEW
    # ================================================================

    list_display = (
        'sub_category_name',
        'parent_category_link',
        'brand',
        'sub_category_type',
        'product_count_display',
        'popularity_score',
        'status_badges',
        'display_order',
        'sub_category_created_at',
    )

    list_display_links = ('sub_category_name',)

    list_filter = (
        'is_active',
        'is_featured',
        'is_trending',
        'is_visible_in_menu',
        'is_visible_on_homepage',
        'sub_category_type',
        'display_style',
        ('brand', admin.RelatedFieldListFilter),
        ('category', admin.RelatedFieldListFilter),
        ('sub_category_created_at', admin.DateFieldListFilter),
        ('deleted_at', admin.EmptyFieldListFilter),
    )

    search_fields = (
        'sub_category_name',
        'sub_category_slug',
        'sub_category_description',
        'category__category_name',
        'brand__brand_name',
    )

    ordering = ('category__category_name', 'display_order', 'sub_category_name')

    list_per_page = 25

    date_hierarchy = 'sub_category_created_at'

    # ================================================================
    # FORM VIEW — FIELDSETS
    # ================================================================

    fieldsets = (
        # ── Identity ────────────────────────────────────────────────
        (_("Identity"), {
            'fields': (
                'uuid',
                'brand',
                'category',
                'sub_category_name',
                'sub_category_slug',
                'sub_category_type',
            )
        }),

        # ── Content ─────────────────────────────────────────────────
        (_("Content"), {
            'fields': (
                'sub_category_description',
                'sub_category_short_description',
            )
        }),

        # ── Media ───────────────────────────────────────────────────
        (_("Media"), {
            'fields': (
                'sub_category_image',
                'sub_category_icon',
                'sub_category_thumbnail',
                'icon_class',
                'color_code',
            ),
            'classes': ('collapse',),
        }),

        # ── Status & Visibility ──────────────────────────────────────
        (_("Status & Visibility"), {
            'fields': (
                'is_active',
                'is_featured',
                'is_trending',
                'is_visible_in_menu',
                'is_visible_on_homepage',
            )
        }),

        # ── Display Settings ─────────────────────────────────────────
        (_("Display Settings"), {
            'fields': (
                'display_style',
                'display_order',
                'products_per_page',
            ),
            'classes': ('collapse',),
        }),

        # ── SEO ──────────────────────────────────────────────────────
        (_("SEO"), {
            'fields': (
                'meta_title',
                'meta_description',
                'meta_keywords',
                'canonical_url',
            ),
            'classes': ('collapse',),
        }),

        # ── Marketplace / Pricing ────────────────────────────────────
        (_("Marketplace & Pricing"), {
            'fields': (
                'commission_rate',
                'effective_commission_rate_display',
                'min_price',
                'max_price',
            ),
            'classes': ('collapse',),
        }),

        # ── Analytics (read-only) ────────────────────────────────────
        (_("Analytics"), {
            'fields': (
                'view_count',
                'product_count',
                'popularity_score',
                'last_viewed_at',
            ),
            'classes': ('collapse',),
        }),

        # ── Metadata ─────────────────────────────────────────────────
        (_("Metadata"), {
            'fields': ('metadata',),
            'classes': ('collapse',),
        }),

        # ── Soft Delete ───────────────────────────────────────────────
        (_("Soft Delete"), {
            'fields': (
                'deleted_at',
                'deleted_by',
                'deletion_reason',
            ),
            'classes': ('collapse',),
        }),

        # ── Timestamps ────────────────────────────────────────────────
        (_("Timestamps"), {
            'fields': (
                'sub_category_created_at',
                'sub_category_updated_at',
            ),
            'classes': ('collapse',),
        }),
    )

    # ================================================================
    # READ-ONLY FIELDS
    # ================================================================

    readonly_fields = (
        'uuid',
        'sub_category_slug',
        'view_count',
        'product_count',
        'popularity_score',
        'last_viewed_at',
        'effective_commission_rate_display',
        'sub_category_created_at',
        'sub_category_updated_at',
    )

    # ================================================================
    # AUTOCOMPLETE
    # ================================================================

    autocomplete_fields = ('category',)

    # ================================================================
    # QUERYSET OPTIMISATION
    # ================================================================

    def get_queryset(self, request):
        # NOTE: Count('products') omitted — Product.sub_category FK not added yet.
        # Use cached `product_count` field. Once FK is wired, switch to:
        #   .annotate(_product_count=Count('products', distinct=True))
        return super().get_queryset(request).select_related(
            'brand',
            'category',
            'deleted_by',
        )

    # ================================================================
    # CUSTOM LIST DISPLAY COLUMNS
    # ================================================================

    @admin.display(description=_("Parent Category"), ordering='category__category_name')
    def parent_category_link(self, obj):
        """Clickable link to parent category admin page"""
        from django.urls import reverse
        url = reverse('admin:ponno_category_change', args=[obj.category.pk])
        return format_html(
            '<a href="{}">{}</a>',
            url,
            obj.category.category_name
        )

    @admin.display(description=_("Products"), ordering='product_count')
    def product_count_display(self, obj):
        """Display the cached product_count field (no FK to Product yet)."""
        count = obj.product_count
        if count == 0:
            return format_html('<span style="color:#999;">0</span>')
        return format_html('<strong>{}</strong> product{}', count, 's' if count != 1 else '')

    @admin.display(description=_("Status"))
    def status_badges(self, obj):
        """Render coloured badge pills for status flags"""
        badges = []

        if not obj.is_active:
            badges.append('<span style="background:#dc3545;color:#fff;padding:2px 7px;border-radius:10px;font-size:11px;">Inactive</span>')
        else:
            badges.append('<span style="background:#28a745;color:#fff;padding:2px 7px;border-radius:10px;font-size:11px;">Active</span>')

        if obj.deleted_at:
            badges.append('<span style="background:#6c757d;color:#fff;padding:2px 7px;border-radius:10px;font-size:11px;">Deleted</span>')

        if obj.is_featured:
            badges.append('<span style="background:#fd7e14;color:#fff;padding:2px 7px;border-radius:10px;font-size:11px;">Featured</span>')

        if obj.is_trending:
            badges.append('<span style="background:#6f42c1;color:#fff;padding:2px 7px;border-radius:10px;font-size:11px;">Trending</span>')

        return format_html(' '.join(badges))

    @admin.display(description=_("Effective Commission Rate"))
    def effective_commission_rate_display(self, obj):
        """Show effective commission rate with fallback info"""
        rate = obj.effective_commission_rate
        if obj.commission_rate and obj.commission_rate > 0:
            return format_html(
                '<strong>{}%</strong> <span style="color:#999;font-size:11px;">(subcategory override)</span>',
                rate
            )
        return format_html(
            '{}% <span style="color:#999;font-size:11px;">(inherited from category)</span>',
            rate
        )

    # ================================================================
    # BULK ACTIONS
    # ================================================================

    actions = [
        'action_activate',
        'action_deactivate',
        'action_feature',
        'action_unfeature',
        'action_mark_trending',
        'action_unmark_trending',
        'action_soft_delete',
        'action_restore',
        'action_recalculate_popularity',
        'action_export_csv',
    ]

    @admin.action(description=_("✅ Activate selected subcategories"))
    def action_activate(self, request, queryset):
        updated = queryset.update(is_active=True)
        self.message_user(
            request,
            _(f"{updated} subcategor{'ies' if updated != 1 else 'y'} activated."),
            messages.SUCCESS
        )

    @admin.action(description=_("🚫 Deactivate selected subcategories"))
    def action_deactivate(self, request, queryset):
        updated = queryset.update(is_active=False)
        self.message_user(
            request,
            _(f"{updated} subcategor{'ies' if updated != 1 else 'y'} deactivated."),
            messages.WARNING
        )

    @admin.action(description=_("⭐ Mark selected as Featured"))
    def action_feature(self, request, queryset):
        updated = queryset.update(is_featured=True)
        self.message_user(
            request,
            _(f"{updated} subcategor{'ies' if updated != 1 else 'y'} marked as featured."),
            messages.SUCCESS
        )

    @admin.action(description=_("✖ Remove Featured from selected"))
    def action_unfeature(self, request, queryset):
        updated = queryset.update(is_featured=False)
        self.message_user(
            request,
            _(f"Featured removed from {updated} subcategor{'ies' if updated != 1 else 'y'}."),
            messages.SUCCESS
        )

    @admin.action(description=_("🔥 Mark selected as Trending"))
    def action_mark_trending(self, request, queryset):
        updated = queryset.update(is_trending=True)
        self.message_user(
            request,
            _(f"{updated} subcategor{'ies' if updated != 1 else 'y'} marked as trending."),
            messages.SUCCESS
        )

    @admin.action(description=_("❌ Remove Trending from selected"))
    def action_unmark_trending(self, request, queryset):
        updated = queryset.update(is_trending=False)
        self.message_user(
            request,
            _(f"Trending removed from {updated} subcategor{'ies' if updated != 1 else 'y'}."),
            messages.SUCCESS
        )

    @admin.action(description=_("🗑 Soft-delete selected subcategories"))
    def action_soft_delete(self, request, queryset):
        count = 0
        for obj in queryset.filter(deleted_at__isnull=True):
            obj.soft_delete(deleted_by_user=request.user, reason="Bulk admin action")
            count += 1
        self.message_user(
            request,
            _(f"{count} subcategor{'ies' if count != 1 else 'y'} soft-deleted."),
            messages.WARNING
        )

    @admin.action(description=_("♻️ Restore selected soft-deleted subcategories"))
    def action_restore(self, request, queryset):
        count = 0
        for obj in queryset.filter(deleted_at__isnull=False):
            obj.restore()
            count += 1
        self.message_user(
            request,
            _(f"{count} subcategor{'ies' if count != 1 else 'y'} restored."),
            messages.SUCCESS
        )

    @admin.action(description=_("📊 Recalculate popularity scores"))
    def action_recalculate_popularity(self, request, queryset):
        count = 0
        for obj in queryset:
            obj.update_product_count(save=False)  # no-op until Product FK exists
            obj.calculate_popularity_score(save=True)
            count += 1
        self.message_user(
            request,
            _(f"Popularity scores recalculated for {count} subcategor{'ies' if count != 1 else 'y'}."),
            messages.SUCCESS
        )

    @admin.action(description=_("📥 Export selected to CSV"))
    def action_export_csv(self, request, queryset):
        response = HttpResponse(content_type='text/csv')
        response['Content-Disposition'] = 'attachment; filename="sub_categories.csv"'

        writer = csv.writer(response)
        writer.writerow([
            'UUID',
            'Name',
            'Slug',
            'brand',
            'Parent Category',
            'Type',
            'Active',
            'Featured',
            'Trending',
            'Product Count',
            'View Count',
            'Popularity Score',
            'Commission Rate (%)',
            'Effective Commission Rate (%)',
            'Display Order',
            'Created At',
        ])

        for obj in queryset.select_related('brand', 'category'):
            writer.writerow([
                str(obj.uuid),
                obj.brand.brand_name if obj.brand else '',
                obj.sub_category_name,
                obj.sub_category_slug or '',
                obj.brand.brand_name,
                obj.category.category_name,
                obj.get_sub_category_type_display(),
                obj.is_active,
                obj.is_featured,
                obj.is_trending,
                obj.product_count,
                obj.view_count,
                obj.popularity_score,
                obj.commission_rate,
                obj.effective_commission_rate,
                obj.display_order,
                obj.sub_category_created_at.strftime('%Y-%m-%d %H:%M:%S'),
            ])

        return response

    # ================================================================
    # SAVE HOOK — auto-update product count & popularity on save
    # ================================================================

    def save_model(self, request, obj, form, change):
        super().save_model(request, obj, form, change)
        # update_product_count is a no-op until Product.sub_category FK is added;
        # calculate_popularity_score uses the cached product_count field safely.
        obj.update_product_count(save=False)
        obj.calculate_popularity_score(save=True)

    # ================================================================
    # DELETED RECORD VISIBILITY
    # ================================================================

    def get_list_display(self, request):
        """Append a deleted indicator column when viewing deleted records"""
        base = list(super().get_list_display(request))
        if 'deleted_indicator' not in base:
            base.append('deleted_indicator')
        return base

    @admin.display(description=_("Deleted?"))
    def deleted_indicator(self, obj):
        if obj.deleted_at:
            return format_html(
                '<span style="color:#dc3545;" title="Deleted at {}">🗑 {}</span>',
                obj.deleted_at.strftime('%Y-%m-%d %H:%M'),
                obj.deleted_at.strftime('%Y-%m-%d'),
            )
        return format_html('<span style="color:#28a745;">—</span>')

# apps/ponno/admin.py (add this block; if the file already has other
# registrations, just append — don't duplicate the imports)

from django.contrib import admin

from apps.ponno.models.discovery_visit_log import DiscoveryVisitLog


@admin.register(DiscoveryVisitLog)
class DiscoveryVisitLogAdmin(admin.ModelAdmin):
    """
    Read-only admin view for the append-only DiscoveryVisitLog audit
    trail. No add/change/delete permitted — matches the model's own
    save()/delete() overrides, which already reject mutation at the
    ORM level. This just keeps the admin UI honest about that instead
    of showing forms that would fail on submit.
    """

    # ── List view ────────────────────────────────────────────────
    list_display = (
        'visited_at',
        'user',
        'role_at_visit',
        'is_authenticated',
        'ip_address',
        'device_type',
        'search_query',
        'filter_slug',
        'sort_by',
        'page_number',
    )
    list_filter = (
        'role_at_visit',
        'device_type',
        'is_authenticated',
        'sort_by',
    )
    search_fields = (
        'ip_address',
        'user__email_or_phone',
        'search_query',
        'filter_slug',
        'session_key',
        'user_agent',
        'referrer',
    )
    date_hierarchy = 'visited_at'
    ordering = ('-visited_at',)
    list_per_page = 100

    # ── Detail view ──────────────────────────────────────────────
    readonly_fields = [f.name for f in DiscoveryVisitLog._meta.fields]
    fieldsets = (
        ('Who', {
            'fields': (
                'user', 'role_at_visit', 'is_authenticated',
                'session_key', 'ip_address', 'user_agent', 'device_type',
            ),
        }),
        ('What', {
            'fields': (
                'query_string', 'search_query', 'filter_slug',
                'sort_by', 'page_number', 'referrer',
            ),
        }),
        ('When', {
            'fields': ('visited_at',),
        }),
    )

    # ── Lock it down: view-only ─────────────────────────────────
    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        # Flip to True if you want superusers able to purge individual
        # rows from the admin UI. Note the model's own delete()
        # override will still raise — you'd need to bypass via
        # queryset.delete() in a custom action instead of obj.delete().
        return False

    def has_view_permission(self, request, obj=None):
        return True



# apps/ponno/admin/campaign_admin.py

from django.contrib import admin
from django.utils.html import format_html
from django.utils.translation import gettext_lazy as _

from apps.ponno.models.campaign import Campaign, CampaignRedemption


class CampaignRedemptionInline(admin.TabularInline):
    model = CampaignRedemption
    extra = 0
    can_delete = False
    fields = (
        'user', 'product', 'order', 'order_item',
        'original_price', 'discount_amount', 'final_price', 'redeemed_at',
    )
    readonly_fields = fields
    ordering = ('-redeemed_at',)

    def has_add_permission(self, request, obj=None):
        # Redemptions are only ever created via CampaignPricingService.redeem();
        # creating one from the admin would bypass the atomic usage-limit
        # check and increment times_used incorrectly.
        return False


@admin.register(Campaign)
class CampaignAdmin(admin.ModelAdmin):
    list_display = (
        'name', 'campaign_type', 'discount_value', 'applies_to',
        'status_badge', 'priority', 'is_stackable', 'usage_display',
        'budget_display', 'start_at', 'end_at', 'is_enabled',
    )
    list_filter = (
        'campaign_type', 'applies_to', 'channel',
        'approval_status', 'is_enabled', 'is_stackable',
        'is_cancelled', 'is_archived',
    )
    search_fields = ('name', 'slug', 'campaign_code', 'description')
    prepopulated_fields = {'slug': ('name',)}

    # Every editable=False field on the model is admin-readonly too —
    # these are only ever written by the model's own methods
    # (register_redemption / approve / reject / archive / save), never
    # meant to be hand-edited from a form.
    readonly_fields = (
        'campaign_id', 'times_used', 'budget_spent',
        'first_activated_at',
        'submitted_for_approval_at', 'approved_at', 'rejected_at',
        'archived_at',
        'created_at', 'updated_at', 'status_badge',
    )
    filter_horizontal = ('products', 'categories', 'brands')
    date_hierarchy = 'start_at'
    inlines = [CampaignRedemptionInline]
    autocomplete_fields = (
        'created_by', 'updated_by', 'approved_by', 'rejected_by', 'archived_by',
    )

    fieldsets = (
        (_("Identity"), {
            'fields': (
                'campaign_id', 'campaign_code', 'name', 'slug',
                'description', 'banner_image', 'terms_conditions',
            )
        }),
        (_("Discount"), {
            'fields': (
                'campaign_type', 'discount_value', 'max_discount_amount',
                'min_order_amount', 'currency',
            )
        }),
        (_("Scope"), {
            'fields': ('applies_to', 'products', 'categories', 'brands', 'channel')
        }),
        (_("Schedule"), {
            'fields': ('start_at', 'end_at')
        }),
        (_("Usage Limits"), {
            'fields': ('usage_limit_total', 'usage_limit_per_user', 'times_used')
        }),
        (_("Budget"), {
            'fields': ('budget_limit', 'budget_spent')
        }),
        (_("Stacking"), {
            'fields': ('priority', 'is_stackable')
        }),
        (_("Approval Workflow"), {
            'fields': (
                'approval_status', 'submitted_for_approval_at',
                'approved_by', 'approved_at',
                'rejected_by', 'rejected_at', 'rejection_reason',
            )
        }),
        (_("Status"), {
            'fields': ('is_enabled', 'first_activated_at', 'is_cancelled', 'status_badge')
        }),
        (_("Archive"), {
            'fields': ('is_archived', 'archived_at', 'archived_by'),
            'classes': ('collapse',),
        }),
        (_("Ownership"), {
            'fields': ('created_by', 'updated_by', 'created_at', 'updated_at'),
            'classes': ('collapse',),
        }),
    )

    @admin.display(description=_("Status"))
    def status_badge(self, obj):
        colors = {
            Campaign.Status.ACTIVE: '#16a34a',
            Campaign.Status.SCHEDULED: '#2563eb',
            Campaign.Status.PAUSED: '#a16207',
            Campaign.Status.ENDED: '#6b7280',
            Campaign.Status.CANCELLED: '#dc2626',
            Campaign.Status.DRAFT: '#6b7280',
            Campaign.Status.PENDING_APPROVAL: '#a855f7',
            Campaign.Status.REJECTED: '#dc2626',
            Campaign.Status.ARCHIVED: '#374151',
        }
        status = obj.status
        color = colors.get(status, '#6b7280')
        return format_html(
            '<span style="color:#fff;background:{};padding:2px 8px;border-radius:10px;font-size:11px;">{}</span>',
            color, status.upper(),
        )

    @admin.display(description=_("Usage"))
    def usage_display(self, obj):
        if not obj.usage_limit_total:
            return f"{obj.times_used} / \u221e"
        return f"{obj.times_used} / {obj.usage_limit_total}"

    @admin.display(description=_("Budget"))
    def budget_display(self, obj):
        if obj.budget_limit is None:
            return "\u2014"
        return f"{obj.budget_spent} / {obj.budget_limit} {obj.currency}"

    def save_model(self, request, obj, form, change):
        if not change and not obj.created_by_id:
            obj.created_by = request.user
        # updated_by is documented as "set by callers, not inferred
        # automatically" — the admin is exactly such a caller, so it
        # should actually set it on every save, not just on create.
        obj.updated_by = request.user
        super().save_model(request, obj, form, change)

    actions = [
        'enable_campaigns', 'disable_campaigns',
        'approve_campaigns', 'cancel_campaigns', 'archive_campaigns',
    ]

    def _apply_per_object(self, request, queryset, method_name, verb):
        """
        Run a Campaign lifecycle method (enable/disable/approve/cancel/
        archive) row-by-row instead of queryset.update(). This matters:
        queryset.update() writes columns directly and skips clean()/
        full_clean() and the side effects those methods carry (e.g.
        enable() stamping first_activated_at the first time a campaign
        goes live, archive() cascading is_enabled=False, etc).
        """
        count = 0
        for obj in queryset:
            getattr(obj, method_name)()
            count += 1
        self.message_user(request, f"{count} campaign(s) {verb}.")

    @admin.action(description=_("Enable selected campaigns"))
    def enable_campaigns(self, request, queryset):
        self._apply_per_object(request, queryset, 'enable', 'enabled')

    @admin.action(description=_("Disable selected campaigns"))
    def disable_campaigns(self, request, queryset):
        self._apply_per_object(request, queryset, 'disable', 'disabled')

    @admin.action(description=_("Approve selected campaigns"))
    def approve_campaigns(self, request, queryset):
        count = 0
        for obj in queryset:
            obj.approve(approved_by_user=request.user)
            count += 1
        self.message_user(request, f"{count} campaign(s) approved.")

    @admin.action(description=_("Cancel selected campaigns"))
    def cancel_campaigns(self, request, queryset):
        self._apply_per_object(request, queryset, 'cancel', 'cancelled')

    @admin.action(description=_("Archive selected campaigns"))
    def archive_campaigns(self, request, queryset):
        count = 0
        for obj in queryset:
            obj.archive(archived_by_user=request.user)
            count += 1
        self.message_user(request, f"{count} campaign(s) archived.")


@admin.register(CampaignRedemption)
class CampaignRedemptionAdmin(admin.ModelAdmin):
    list_display = ('campaign', 'user', 'product', 'order', 'discount_amount', 'final_price', 'redeemed_at')
    list_filter = ('campaign',)
    search_fields = ('campaign__name', 'user__username', 'product__product_name', 'order__order_number')
    readonly_fields = [f.name for f in CampaignRedemption._meta.fields]
    date_hierarchy = 'redeemed_at'

    def has_add_permission(self, request):
        # Same rationale as the inline above — writes must go through
        # CampaignPricingService.redeem().
        return False

    def has_change_permission(self, request, obj=None):
        return False